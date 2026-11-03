"""Alerts outside the tool: the COSMOS notification bell and webhooks.

Events (approval waiting, run failed, activity missed ...) go to:

* COSMOS notifications: the bell in every COSMOS tool. "alert" level events
  also pop up as toasts, and COSMOS shows them again on every page load until
  someone acknowledges them, so only events that need someone to act are
  alerts: an approval still waiting after ``approval_escalate_after`` seconds,
  an unattended run (trigger, activity) failing, a missed or stuck activity,
  failed prediction. Everything else goes to the bell only. Each links back
  to the Flow Automator.
* Webhooks: an HTTP POST per event, as Slack/Teams incoming-webhook JSON
  ({"text": ...}) or a generic JSON body for anything else (PagerDuty via a
  relay, a ticketing system, a script).

Which events go where is configurable. Webhooks are delivered from a
background thread with retries, so a slow endpoint never holds up a run.
Repeats of the same event (same key) within ``DEDUP_SECONDS`` are dropped.
"""

import copy
import queue
import threading
import time
import uuid
from urllib.parse import urlparse

import requests


DEDUP_SECONDS = 60
RETRIES = 3
TIMEOUT_SECONDS = 5

# event -> (default level, description). Levels: "alert" (toast + bell), "notification" (bell)
EVENTS = {
    "approval_waiting": ("alert", "A run is waiting for operator approval"),
    "run_failed": ("alert", "A run failed"),
    "run_stopped": ("notification", "A run was stopped or aborted"),
    "activity_missed": ("alert", "A scheduled activity didn't start"),
    "activity_failed": ("alert", "A scheduled activity's run failed"),
    "activity_needs_review": ("alert", "A scheduled activity's pass moved and it needs review"),
    "activity_started": ("notification", "A scheduled activity started"),
    "activity_done": ("notification", "A scheduled activity finished"),
    "stop_all": ("notification", "Someone pressed Stop all"),
    "scheduler_paused": ("notification", "The contact scheduler was paused or resumed"),
    "tle_stale": ("notification", "A satellite's TLE is getting old"),
    "prediction_failed": ("alert", "Pass prediction failed for a satellite"),
    "flow_notify": ("notification", "A flow's Notify node"),
}
APPROVAL_ESCALATE_AFTER = 30  # seconds an approval waits before it's alerted (someone in the tool answers sooner)
DEFAULT_COSMOS_EVENTS = [
    "approval_waiting",
    "run_failed",
    "activity_missed",
    "activity_failed",
    "activity_needs_review",
    "stop_all",
    "tle_stale",
    "prediction_failed",
    "flow_notify",
]
FORMATS = ("slack", "teams", "generic")


class AlertError(Exception):
    status = 400


def _mask(url):
    parsed = urlparse(url or "")
    if not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}/…"


class Alerter:
    def __init__(self, store, notify=None, log=None, clock=time.time, post=None):
        """notify(level, message, url) posts to COSMOS notifications; post(url, json) delivers a webhook."""
        self.store = store
        self.notify = notify
        self.log = log
        self.clock = clock
        self.post = post or self._post
        self.settings = self._defaults()
        self.status = {}  # webhook id -> {"sent", "failed", "last_ok", "last_error"}
        self.recent = {}  # dedup key -> time
        self.sent = []  # the last few events, for the settings dialog
        self.lock = threading.Lock()
        self._queue = queue.Queue()
        self._thread = None
        self._stop = threading.Event()

    @staticmethod
    def _defaults():
        return {
            "cosmos": {"enabled": True, "events": list(DEFAULT_COSMOS_EVENTS)},
            "webhooks": [],
            "base_url": "",
            "approval_escalate_after": APPROVAL_ESCALATE_AFTER,
        }

    def load(self):
        saved = self.store.records("alert_settings").get("settings")
        if saved:
            self.settings = {**self._defaults(), **saved}

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._deliver_loop, name="flow-alerts", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout=5)

    # ---- settings ----------------------------------------------------------

    def settings_json(self):
        """Settings for the tool. Webhook URLs often hold secrets, so only their host is shown."""
        with self.lock:
            result = copy.deepcopy(self.settings)
            for hook in result["webhooks"]:
                hook["url_masked"] = _mask(hook.pop("url", ""))
                hook["status"] = dict(self.status.get(hook["id"], {}))
            result["events"] = {k: {"level": v[0], "description": v[1]} for k, v in EVENTS.items()}
            result["recent"] = list(self.sent[:20])
            return result

    def save_settings(self, payload):
        if not isinstance(payload, dict):
            raise AlertError("Alert settings must be a JSON object")
        with self.lock:
            current = {h["id"]: h for h in self.settings["webhooks"]}
        cosmos = payload.get("cosmos") or {}
        settings = {
            "cosmos": {
                "enabled": bool(cosmos.get("enabled", True)),
                "events": self._events(cosmos.get("events", DEFAULT_COSMOS_EVENTS)),
            },
            "base_url": str(payload.get("base_url") or "").rstrip("/"),
            "webhooks": [],
            "approval_escalate_after": self._seconds(payload.get("approval_escalate_after", APPROVAL_ESCALATE_AFTER)),
        }
        if settings["base_url"] and urlparse(settings["base_url"]).scheme not in ("http", "https"):
            raise AlertError("COSMOS address must start with http:// or https://")
        for hook in payload.get("webhooks") or []:
            hook_id = hook.get("id") or uuid.uuid4().hex[:8]
            url = str(hook.get("url") or "").strip() or current.get(hook_id, {}).get("url", "")
            if urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
                raise AlertError(f"Webhook '{hook.get('name') or hook_id}' needs an http(s) URL")
            fmt = hook.get("format", "generic")
            if fmt not in FORMATS:
                raise AlertError(f"Webhook format must be one of {', '.join(FORMATS)}")
            settings["webhooks"].append(
                {
                    "id": hook_id,
                    "name": str(hook.get("name") or "Webhook"),
                    "url": url,
                    "format": fmt,
                    "enabled": bool(hook.get("enabled", True)),
                    "events": self._events(hook.get("events", DEFAULT_COSMOS_EVENTS)),
                    "include_dry_runs": bool(hook.get("include_dry_runs", False)),
                }
            )
        with self.lock:
            self.settings = settings
        self.store.save_record("alert_settings", "settings", settings)
        return self.settings_json()

    @staticmethod
    def _seconds(value):
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            raise AlertError("Approval alert delay must be a number of seconds") from None
        if not 0 <= seconds <= 3600:
            raise AlertError("Approval alert delay must be between 0 and 3600 seconds")
        return seconds

    @property
    def approval_delay(self):
        return float(self.settings.get("approval_escalate_after", APPROVAL_ESCALATE_AFTER))

    @staticmethod
    def _events(events):
        if not isinstance(events, list):
            raise AlertError("events must be a list")
        unknown = [e for e in events if e not in EVENTS]
        if unknown:
            raise AlertError(f"Unknown alert events: {', '.join(unknown)}")
        return list(dict.fromkeys(events))

    # ---- sending -----------------------------------------------------------

    def link(self, path):
        base = self.settings.get("base_url") or ""
        return f"{base}{path}" if path else base

    def emit(self, event, title, message="", path=None, key=None, level=None, data=None, dry_run=False):
        """Send an event to COSMOS notifications and every webhook that wants it. Returns True if sent."""
        level = level or EVENTS.get(event, ("notification", ""))[0]
        now = self.clock()
        with self.lock:
            dedup = key or f"{event}:{title}:{message}"
            if now - self.recent.get(dedup, -1e18) < DEDUP_SECONDS:
                return False
            self.recent[dedup] = now
            if len(self.recent) > 5000:
                self.recent = {k: t for k, t in self.recent.items() if now - t < DEDUP_SECONDS}
            settings = self.settings
            record = {"time": now, "event": event, "level": level, "title": title, "message": message}
            self.sent = [record] + self.sent[:99]
        text = f"{'[DRY RUN] ' if dry_run else ''}{title}" + (f": {message}" if message else "")
        cosmos = settings["cosmos"]
        if self.notify and cosmos.get("enabled") and event in cosmos.get("events", []) and not dry_run:
            try:
                self.notify(level, f"Flow Automator: {text}", path)
            except Exception as error:  # noqa: BLE001
                if self.log:
                    self.log("error", f"Could not post COSMOS notification: {error}")
        body = {
            "event": event,
            "level": level,
            "title": title,
            "message": message,
            "dry_run": dry_run,
            "url": self.link(path) if path else None,
            "time": now,
            "data": data or {},
        }
        for hook in settings["webhooks"]:
            if not hook.get("enabled", True) or event not in hook.get("events", []):
                continue
            if dry_run and not hook.get("include_dry_runs"):
                continue
            self._queue.put((hook, body))
        if not (self._thread and self._thread.is_alive()):
            self._drain()  # no delivery thread (tests): deliver inline
        return True

    def test(self, hook_id=None):
        """Send a test event to one webhook (or to COSMOS notifications) right now. Returns the result."""
        body = {
            "event": "test",
            "level": "notification",
            "title": "Test alert from the Flow Automator",
            "message": "If you can read this, alerts reach you.",
            "dry_run": False,
            "url": self.link("/tools/flowautomator"),
            "time": self.clock(),
            "data": {},
        }
        if hook_id is None:
            if not self.notify:
                raise AlertError("COSMOS notifications aren't available")
            self.notify("notification", f"Flow Automator: {body['title']}", "/tools/flowautomator")
            return {"ok": True}
        with self.lock:
            hook = next((h for h in self.settings["webhooks"] if h["id"] == hook_id), None)
        if hook is None:
            raise AlertError(f"Webhook '{hook_id}' not found")
        error = self._deliver(hook, body, retries=1)
        return {"ok": error is None, "error": error}

    def _deliver_loop(self):
        while not self._stop.is_set():
            item = self._queue.get()
            if item is None:
                continue
            self._deliver(*item)

    def _drain(self):
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is not None:
                self._deliver(*item, retries=1)

    def _deliver(self, hook, body, retries=RETRIES):
        payload = self._format(hook["format"], body)
        error = None
        for attempt in range(retries):
            try:
                self.post(hook["url"], payload)
                error = None
                break
            except Exception as failure:  # noqa: BLE001 - recorded on the hook
                error = str(failure)
                if attempt + 1 < retries:
                    time.sleep(2 * (attempt + 1))
        with self.lock:
            status = self.status.setdefault(hook["id"], {"sent": 0, "failed": 0})
            if error is None:
                status["sent"] += 1
                status["last_ok"] = self.clock()
            else:
                status["failed"] += 1
                status["last_error"] = error
                status["last_error_at"] = self.clock()
        if error and self.log:
            self.log("warn", f"Alert webhook '{hook['name']}' failed: {error}")
        return error

    @staticmethod
    def _format(fmt, body):
        if fmt == "generic":
            return body
        icon = {"alert": "🔴", "notification": "🔵"}.get(body["level"], "")
        prefix = "[DRY RUN] " if body["dry_run"] else ""
        text = f"{icon} *{prefix}{body['title']}*"
        if body["message"]:
            text += f"\n{body['message']}"
        if body["url"]:
            text += f"\n<{body['url']}|Open in COSMOS>" if fmt == "slack" else f"\n[Open in COSMOS]({body['url']})"
        return {"text": text}

    @staticmethod
    def _post(url, payload):
        response = requests.post(url, json=payload, timeout=TIMEOUT_SECONDS)
        if response.status_code >= 300:
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:200]}")
