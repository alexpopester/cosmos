"""HTTP API for the Flow Automator tool, served by the engine microservice.

Traefik routes ``<prefix>/*`` (the plugin's ROUTE_PREFIX) here without
stripping the prefix. Every route except ``/health`` needs a COSMOS token in the
``Authorization`` header; ``authorize(token, write)`` decides whether it's
good enough (writes need COSMOS's script_run permission) and returns the user,
which every write records in the audit log.

    GET    <prefix>/api/health                   no token; {"ok", "store": "ok" | "degraded"}
    GET    <prefix>/api/status
    GET    <prefix>/api/whoami                   the user the token belongs to
    POST   <prefix>/api/demo                     reset and load the demo (needs the COSMOS demo plugin's INST)
    DELETE <prefix>/api/demo                     remove the demo
    GET    <prefix>/api/audit?limit=&kind=&user=&target_id=&search=   who did what, newest first
    GET    <prefix>/api/backup?history=1         everything in this scope as one JSON document (backup.py);
                                                 history=1 adds run history and the audit log
    POST   <prefix>/api/restore                  {"backup": ...}: merge a backup in; restored flows are
                                                 disarmed and the scheduler is paused
    GET    <prefix>/api/flows
    POST   <prefix>/api/flows                 create
    POST   <prefix>/api/validate              check a flow without saving
    GET    <prefix>/api/flows/<id>
    PUT    <prefix>/api/flows/<id>            replace (never changes armed); {"note", "base_version", "force"}
                                               409 if someone saved a newer version since base_version
    DELETE <prefix>/api/flows/<id>
    POST   <prefix>/api/flows/<id>/arm
    POST   <prefix>/api/flows/<id>/disarm
    POST   <prefix>/api/flows/<id>/run        {"trigger_node"?, "variables"?, "mode"?, "dry_run"?, "skip_waits"?,
                                               "contact"?: {"contact_id"} or {"aos_in", "duration", ...}}
    GET    <prefix>/api/runs?flow_id=&limit=
    GET    <prefix>/api/runs/<id>
    POST   <prefix>/api/runs/<id>/stop
    POST   <prefix>/api/runs/<id>/approve     {"approval_id": id, "decision": "go" | "run" | "abort"}
    POST   <prefix>/api/stop_all                 also pauses the contact scheduler

  History:
    GET    <prefix>/api/flows/<id>/versions      every saved version (who, when, note, what changed)
    GET    <prefix>/api/flows/<id>/versions/<n>  one version, with the whole flow
    GET    <prefix>/api/flows/<id>/diff?from=<n>&to=<m|current>
    POST   <prefix>/api/flows/<id>/versions/<n>/restore   saves version n as a new version
    GET    <prefix>/api/flows/deleted
    POST   <prefix>/api/flows/<id>/undelete
    GET    <prefix>/api/runs?flow_id=&status=&trigger=&user=&activity_id=&dry_run=&since=&until=&search=

  Alerts:
    GET    <prefix>/api/alerts                   settings (webhook URLs masked)
    PUT    <prefix>/api/alerts
    POST   <prefix>/api/alerts/test              {"webhook_id"?}: none tests COSMOS notifications

  Contacts (see contacts.py):
    GET    <prefix>/api/schedule                 satellites, stations, contacts, activities
    GET    <prefix>/api/contacts?start=&end=&satellite_id=&station_id=
    POST   <prefix>/api/contacts/recompute
    POST   <prefix>/api/satellites               {"name", "target", "tle"}
    PUT    <prefix>/api/satellites/<id>
    DELETE <prefix>/api/satellites/<id>
    POST   <prefix>/api/satellites/<id>/tle      {"tle": "line1\nline2", "source"?}: a source other than
                                                 "manual" goes through the safeguards (tle_inputs.py)
    POST   <prefix>/api/satellites/<id>/tle/accept   apply a held TLE
    POST   <prefix>/api/satellites/<id>/tle/reject
    POST   <prefix>/api/tles                     {"tle": text with any number of TLEs, "source"}: matched by
                                                 catalog number then name; returns what happened to each
    GET    <prefix>/api/tle_inputs               stash key, watched files and packets, safeguards, status
    PUT    <prefix>/api/tle_inputs
    GET    <prefix>/api/tle_sources              CelesTrak / Space-Track settings and status (never the credentials)
    PUT    <prefix>/api/tle_sources              {"celestrak": {"url", "hours"}, "spacetrack": {..., "username_secret",
                                                 "password_secret"}}
    POST   <prefix>/api/tle_sources/fetch        {"kind"?}: fetch what's allowed now (tle_sources.py rate limits)
    POST   <prefix>/api/stations                 {"name", "latitude", "longitude", "altitude", "min_elevation"}
    PUT    <prefix>/api/stations/<id>
    DELETE <prefix>/api/stations/<id>
    GET    <prefix>/api/activities
    POST   <prefix>/api/activities               {"template_id", "contact_id", "params", "anchor", "offset", "mode", ...}
    PUT    <prefix>/api/activities/<id>
    DELETE <prefix>/api/activities/<id>          stops its run if it's running
    POST   <prefix>/api/activities/<id>/accept   move a needs-review activity to the suggested contact
    POST   <prefix>/api/activities/<id>/refresh_template
    GET    <prefix>/api/rules                    recurring rules: a template into every matching contact
    POST   <prefix>/api/rules                    {"template_id", "satellite_ids", "station_ids", "min_elevation", ...}
    PUT    <prefix>/api/rules/<id>
    DELETE <prefix>/api/rules/<id>               removes its scheduled activities
    POST   <prefix>/api/scheduler/pause
    POST   <prefix>/api/scheduler/resume
"""

import contextlib
import json
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .alerts import AlertError
from .audit import user_name
from .backup import BackupError, make_backup, restore_backup
from .contacts import PlannerError
from .engine import EngineError
from .graph import validate_flow
from .tle_inputs import TleInputError
from .tle_sources import TleSourceError


MAX_BODY_BYTES = 5 * 1024 * 1024
MAX_RESTORE_BYTES = 25 * 1024 * 1024  # a backup with every version; one restore is read at a time
_restoring = threading.Semaphore(1)
MAX_CONCURRENT_REQUESTS = 64
# When every slot is busy (slow or hostile clients), up to this many more connections are peeked at, and served
# only if they ask for STOP ALL or /health: a full server can always still stop everything
MAX_PRIORITY_REQUESTS = 8
PRIORITY_PEEK_SECONDS = 2.0
_PRIORITY_PATH = re.compile(rb"^(GET|POST) \S*/api/(stop_all|health)/?[?\s]")
SOCKET_TIMEOUT_SECONDS = 30


def _unverified_user(token):
    from .cosmos_backend import token_identity  # local: cosmos_backend imports this module

    identity = token_identity(token)
    return {**identity, "username": f"{identity['username']} (token not verified: COSMOS unreachable)"}


class AuthError(Exception):
    def __init__(self, message, status=401):
        super().__init__(message)
        self.status = status


def _routes():
    # (method, regex, handler name, needs write permission)
    return [
        ("GET", r"/api/health", "health", None),
        ("GET", r"/api/status", "status", False),
        ("GET", r"/api/whoami", "whoami", False),
        ("POST", r"/api/demo", "load_demo", True),
        ("DELETE", r"/api/demo", "clear_demo", True),
        ("GET", r"/api/audit", "audit_log", False),
        ("GET", r"/api/backup", "backup", False),
        ("POST", r"/api/restore", "restore", True),
        ("GET", r"/api/flows", "list_flows", False),
        ("POST", r"/api/flows", "create_flow", True),
        ("POST", r"/api/validate", "validate", True),  # write: validation is costly, and only editors need it
        ("GET", r"/api/flows/deleted", "list_deleted", False),
        ("POST", r"/api/flows/(?P<flow_id>[\w-]+)/undelete", "undelete_flow", True),
        ("GET", r"/api/flows/(?P<flow_id>[\w-]+)/versions", "list_versions", False),
        ("GET", r"/api/flows/(?P<flow_id>[\w-]+)/versions/(?P<version>\d+)", "get_version", False),
        ("GET", r"/api/flows/(?P<flow_id>[\w-]+)/diff", "diff_versions", False),
        ("POST", r"/api/flows/(?P<flow_id>[\w-]+)/versions/(?P<version>\d+)/restore", "restore_version", True),
        ("GET", r"/api/flows/(?P<flow_id>[\w-]+)", "get_flow", False),
        ("PUT", r"/api/flows/(?P<flow_id>[\w-]+)", "update_flow", True),
        ("DELETE", r"/api/flows/(?P<flow_id>[\w-]+)", "delete_flow", True),
        ("POST", r"/api/flows/(?P<flow_id>[\w-]+)/arm", "arm", True),
        ("POST", r"/api/flows/(?P<flow_id>[\w-]+)/disarm", "disarm", True),
        ("POST", r"/api/flows/(?P<flow_id>[\w-]+)/run", "run", True),
        ("GET", r"/api/runs", "list_runs", False),
        ("GET", r"/api/runs/(?P<run_id>[\w-]+)", "get_run", False),
        ("POST", r"/api/runs/(?P<run_id>[\w-]+)/stop", "stop_run", True),
        ("POST", r"/api/runs/(?P<run_id>[\w-]+)/approve", "approve", True),
        ("POST", r"/api/stop_all", "stop_all", True),
        ("GET", r"/api/schedule", "schedule", False),
        ("GET", r"/api/contacts", "list_contacts", False),
        ("POST", r"/api/contacts/recompute", "recompute", True),
        ("POST", r"/api/satellites", "create_satellite", True),
        ("PUT", r"/api/satellites/(?P<item_id>[\w-]+)", "update_satellite", True),
        ("DELETE", r"/api/satellites/(?P<item_id>[\w-]+)", "delete_satellite", True),
        ("POST", r"/api/satellites/(?P<item_id>[\w-]+)/tle", "set_tle", True),
        ("POST", r"/api/satellites/(?P<item_id>[\w-]+)/tle/accept", "accept_held_tle", True),
        ("POST", r"/api/satellites/(?P<item_id>[\w-]+)/tle/reject", "reject_held_tle", True),
        ("POST", r"/api/tles", "ingest_tles", True),
        ("GET", r"/api/tle_inputs", "get_tle_inputs", False),
        ("PUT", r"/api/tle_inputs", "save_tle_inputs", True),
        ("GET", r"/api/tle_sources", "get_tle_sources", False),
        ("PUT", r"/api/tle_sources", "save_tle_sources", True),
        ("POST", r"/api/tle_sources/fetch", "fetch_tles", True),
        ("POST", r"/api/stations", "create_station", True),
        ("PUT", r"/api/stations/(?P<item_id>[\w-]+)", "update_station", True),
        ("DELETE", r"/api/stations/(?P<item_id>[\w-]+)", "delete_station", True),
        ("GET", r"/api/activities", "list_activities", False),
        ("POST", r"/api/activities", "create_activity", True),
        ("PUT", r"/api/activities/(?P<item_id>[\w-]+)", "update_activity", True),
        ("DELETE", r"/api/activities/(?P<item_id>[\w-]+)", "delete_activity", True),
        ("POST", r"/api/activities/(?P<item_id>[\w-]+)/accept", "accept_activity", True),
        ("POST", r"/api/activities/(?P<item_id>[\w-]+)/refresh_template", "refresh_template", True),
        ("POST", r"/api/activities/(?P<item_id>[\w-]+)/rehearse", "rehearse_activity", True),
        ("GET", r"/api/alerts", "get_alerts", False),
        ("PUT", r"/api/alerts", "save_alerts", True),
        ("POST", r"/api/alerts/test", "test_alert", True),
        ("GET", r"/api/rules", "list_rules", False),
        ("POST", r"/api/rules", "create_rule", True),
        ("PUT", r"/api/rules/(?P<item_id>[\w-]+)", "update_rule", True),
        ("DELETE", r"/api/rules/(?P<item_id>[\w-]+)", "delete_rule", True),
        ("POST", r"/api/scheduler/pause", "pause_scheduler", True),
        ("POST", r"/api/scheduler/resume", "resume_scheduler", True),
    ]


class FlowApi:
    """Routes requests to the engine. Kept separate from the HTTP server so tests can call it directly."""

    def __init__(self, engine, authorize, prefix="/flow-automator"):
        self.engine = engine
        self.authorize = authorize
        self.prefix = "/" + prefix.strip("/")
        self.routes = [(m, re.compile(re.escape(self.prefix) + p + r"/?$"), h, w) for m, p, h, w in _routes()]

    def preauthorize(self, method, path, headers):
        """Check the token before the body is read. Returns None, or (status, body) to refuse with.

        A client with no valid token can't hold a request slot by sending its body slowly.
        """
        for route_method, pattern, handler, write in self.routes:
            if route_method != method or not pattern.match(path):
                continue
            if write is None:
                return None
            token = headers.get("Authorization") or headers.get("authorization")
            try:
                self.authorize(token, write)
            except AuthError as error:
                if handler == "stop_all" and error.status == 503 and token:
                    return None  # see handle(): STOP ALL still works when COSMOS can't check tokens
                return error.status, {"error": str(error)}
            return None
        return None

    def handle(self, method, path, query, headers, body):
        """Returns (status, json-able body)."""
        for route_method, pattern, handler, write in self.routes:
            match = pattern.match(path)
            if not match or route_method != method:
                continue
            try:
                user = None
                if write is not None:
                    token = headers.get("Authorization") or headers.get("authorization")
                    try:
                        user = self.authorize(token, write)
                    except AuthError as error:
                        # STOP ALL only reduces activity: if COSMOS can't be reached to check the token,
                        # still let someone holding a token stop everything (and say it was unverified)
                        if handler != "stop_all" or error.status != 503 or not token:
                            raise
                        user = _unverified_user(token)
                payload = json.loads(body) if body else {}
                if not isinstance(payload, dict):
                    return 400, {"error": "The request body must be a JSON object"}
                return 200, getattr(self, handler)(payload=payload, query=query, user=user, **match.groupdict())
            except AuthError as error:
                return error.status, {"error": str(error)}
            except EngineError as error:
                body = {"error": str(error), "problems": error.problems}
                body.update(getattr(error, "details", {}))
                return error.status, body
            except PlannerError as error:
                return error.status, {"error": str(error), "problems": []}
            except AlertError as error:
                return error.status, {"error": str(error), "problems": []}
            except TleInputError as error:
                return error.status, {"error": str(error), "problems": []}
            except TleSourceError as error:
                return error.status, {"error": str(error), "problems": []}
            except BackupError as error:
                return error.status, {"error": str(error), "problems": []}
            except json.JSONDecodeError as error:
                return 400, {"error": f"Invalid JSON: {error}"}
        if any(pattern.match(path) for _m, pattern, _h, _w in self.routes):
            return 405, {"error": f"{method} not allowed on {path}"}
        return 404, {"error": f"No route for {method} {path}"}

    # ---- handlers --------------------------------------------------------
    # Every write records who did it (``user`` comes from their token) in the audit log.

    def _audit(self, user, action, kind, target_id=None, target_name=None, detail=""):
        self.engine.audit.record(user, action, kind, target_id, target_name, detail)

    def _flow_name(self, flow_id):
        flow = self.engine.flows.get(flow_id)
        return flow.get("name") if flow else flow_id

    def backup(self, query, user, **_):
        history = (query.get("history") or [""])[0] in ("1", "true", "yes")
        return make_backup(self.engine, include_history=history)

    def restore(self, payload, user, **_):
        backup = payload.get("backup", payload)
        result = restore_backup(self.engine, backup, user)
        counts = ", ".join(
            f"{sum(len(v) for k, v in result[key].items() if k != 'failed')} {key}"
            for key in ("flows", "satellites", "stations", "rules")
        )
        self._audit(user, "restored", "backup", detail=f"{counts}; scheduler paused")
        return result

    def health(self, **_):
        # No token needed, so only a word: 200 while the engine answers, "store" says whether Valkey takes writes
        # (/status has the details)
        return {"ok": True, "store": "ok" if self.engine.store.health()["ok"] else "degraded"}

    def status(self, **_):
        return self.engine.status()

    def whoami(self, user, **_):
        return {"user": user or {"username": "anonymous", "name": "Anonymous"}}

    def list_flows(self, **_):
        return self.engine.list_flows()

    @staticmethod
    def _save_options(payload):
        flow = {k: v for k, v in payload.items() if k not in ("note", "base_version", "force")}
        return flow, payload.get("note") or "", payload.get("base_version"), bool(payload.get("force"))

    def create_flow(self, payload, user, **_):
        flow, note, _base, _force = self._save_options(payload)
        flow, problems = self.engine.save_flow(flow, user=user, note=note)
        self._audit(
            user, "created", "flow", flow["id"], flow["name"], f"v{flow['version']}, {len(flow['nodes'])} nodes"
        )
        return {"flow": flow, "problems": problems}

    def validate(self, payload, **_):
        return {"problems": validate_flow(payload)}

    def get_flow(self, flow_id, **_):
        flow = self.engine.get_flow(flow_id)
        return {"flow": flow, "problems": validate_flow(flow)}

    def update_flow(self, flow_id, payload, user, **_):
        body, note, base, force = self._save_options(payload)
        before = self.engine.flows.get(flow_id, {}).get("version")
        flow, problems = self.engine.save_flow(body, flow_id, user=user, note=note, base_version=base, force=force)
        if flow.get("version") != before:
            latest = self.engine.versions.latest(flow_id) or {}
            detail = f"v{flow['version']}: {latest.get('summary', '')}" + (f" ({note})" if note else "")
            if force and base is not None and int(base) != int(before or 0):
                detail += f" (overwrote v{before} from v{base})"
            self._audit(user, "saved", "flow", flow_id, flow["name"], detail)
        return {"flow": flow, "problems": problems}

    def list_versions(self, flow_id, **_):
        return self.engine.list_versions(flow_id)

    def get_version(self, flow_id, version, **_):
        return self.engine.get_version(flow_id, int(version))

    def diff_versions(self, flow_id, query, **_):
        def number(name):
            value = (query.get(name) or [None])[0]
            if value in (None, "", "current"):
                return None
            try:
                return int(value)
            except ValueError:
                raise EngineError(f"{name} must be a version number") from None

        old = number("from")
        if old is None:
            raise EngineError("from is required")
        return self.engine.diff_versions(flow_id, old, number("to"))

    def restore_version(self, flow_id, version, payload, user, **_):
        flow, problems = self.engine.restore_version(flow_id, int(version), user, payload.get("note") or "")
        self._audit(user, "restored", "flow", flow_id, flow["name"], f"v{version} as v{flow['version']}")
        return {"flow": flow, "problems": problems}

    def list_deleted(self, **_):
        return self.engine.list_deleted()

    def undelete_flow(self, flow_id, user, **_):
        flow, problems = self.engine.restore_deleted(flow_id, user)
        self._audit(user, "restored deleted", "flow", flow_id, flow["name"], f"as v{flow['version']}")
        return {"flow": flow, "problems": problems}

    def delete_flow(self, flow_id, user, **_):
        flow = self.engine.delete_flow(flow_id, user)
        self._audit(user, "deleted", "flow", flow_id, flow.get("name"), f"last version v{flow.get('version')}")
        return {"deleted": flow_id}

    def arm(self, flow_id, user, **_):
        flow = self.engine.set_armed(flow_id, True)
        self._audit(user, "armed", "flow", flow_id, flow["name"])
        return {"flow": flow}

    def disarm(self, flow_id, user, **_):
        flow = self.engine.set_armed(flow_id, False)
        self._audit(user, "disarmed", "flow", flow_id, flow["name"])
        return {"flow": flow}

    def run(self, flow_id, payload, user, **_):
        variables = payload.get("variables") or {}
        if not isinstance(variables, dict):
            raise EngineError("variables must be an object")
        dry_run = bool(payload.get("dry_run"))
        info = {"kind": "manual", "user": user_name(user)}
        contact = payload.get("contact")
        if contact:
            variables = {**self.engine.planner.contact_variables(contact), **variables}
            info["simulated_contact"] = variables.get("contact_id")
        kind = "Dry run" if dry_run else "Manual run"
        result = self.engine.trigger(
            flow_id,
            payload.get("trigger_node"),
            payload.get("reason") or f"{kind} by {user_name(user)}",
            variables,
            manual=True,
            mode=payload.get("mode"),
            trigger_info=info,
            dry_run=dry_run,
            skip_waits=bool(payload.get("skip_waits")),
        )
        run = result.get("run") or {}
        if run:
            extra = ", dry run" if run.get("dry_run") else ""
            detail = f"run {run['id']} ({run.get('mode')}{extra}, v{run.get('flow_version')})"
            action = "dry ran" if run.get("dry_run") else "ran"
        else:
            detail = result.get("message", result["result"])
            action = f"run {result['result']}"
        self._audit(user, action, "flow", flow_id, self._flow_name(flow_id), detail)
        return result

    def list_runs(self, query, **_):
        def arg(name):
            return (query.get(name) or [None])[0]

        try:
            limit = max(1, min(2000, int(arg("limit") or 50)))
            since = float(arg("since")) if arg("since") else None
            until = float(arg("until")) if arg("until") else None
        except ValueError:
            raise EngineError("limit, since and until must be numbers") from None
        dry = arg("dry_run")
        return self.engine.list_runs(
            arg("flow_id"),
            limit,
            status=arg("status"),
            trigger=arg("trigger"),
            user=arg("user"),
            activity_id=arg("activity_id"),
            dry_run=None if dry in (None, "") else dry.lower() in ("1", "true", "yes"),
            since=since,
            until=until,
            search=arg("search"),
        )

    def get_run(self, run_id, **_):
        return self.engine.get_run(run_id)

    def approve(self, run_id, payload, user, **_):
        run = self.engine.runs.get(run_id)
        pending = {a["id"]: a for a in run.pending_approvals()} if run else {}
        result = self.engine.approve(
            run_id, payload.get("approval_id"), payload.get("decision"), user, payload.get("value")
        )
        approval = pending.get(payload.get("approval_id")) or {}
        detail = f"'{payload.get('decision')}' at '{approval.get('label', '?')}' in run {run_id}"
        if approval.get("reason"):
            detail += f" ({approval['reason']})"
        self._audit(
            user,
            "approved" if payload.get("decision") != "abort" else "aborted",
            "flow",
            result["run"]["flow_id"],
            result["run"]["flow_name"],
            detail,
        )
        return result

    def stop_run(self, run_id, user, **_):
        run = self.engine.runs.get(run_id)
        self.engine.stop_run(run_id)
        self._audit(
            user,
            "stopped run",
            "flow",
            run.flow.get("id") if run else None,
            run.flow.get("name") if run else None,
            f"run {run_id}",
        )
        return {"stopping": run_id}

    def stop_all(self, user, **_):
        result = self.engine.stop_all(user)
        self._audit(
            user, "STOP ALL", "engine", detail=f"disarmed {result['disarmed']}, stopped {result['stopped']} runs"
        )
        return result

    def audit_log(self, query, **_):
        def arg(name):
            return (query.get(name) or [None])[0]

        try:
            limit = int(arg("limit") or 200)
        except ValueError:
            raise EngineError("limit must be a number") from None
        return self.engine.audit.entries(limit, arg("kind"), arg("user"), arg("target_id"), arg("search"))

    # ---- contacts --------------------------------------------------------

    @property
    def planner(self):
        return self.engine.planner

    @staticmethod
    def _query_number(query, name):
        value = (query.get(name) or [None])[0]
        if value in (None, ""):
            return None
        try:
            return float(value)
        except ValueError:
            raise PlannerError(f"{name} must be a number (Unix seconds)") from None

    def schedule(self, query, **_):
        return self.planner.schedule(self._query_number(query, "start"), self._query_number(query, "end"))

    def list_contacts(self, query, **_):
        return self.planner.list_contacts(
            self._query_number(query, "start"),
            self._query_number(query, "end"),
            (query.get("satellite_id") or [None])[0],
            (query.get("station_id") or [None])[0],
        )

    def recompute(self, user, **_):
        self.planner.recompute()
        self._audit(user, "recomputed contacts", "scheduler")
        return self.planner.status()

    @staticmethod
    def _tle_detail(sat):
        tle = sat.get("tle") or {}
        return f"TLE {tle.get('norad_id')} epoch {tle.get('epoch')}" if tle else ""

    def create_satellite(self, payload, user, **_):
        sat = self.planner.save_satellite(payload, user=user)
        self._audit(user, "added", "satellite", sat["id"], sat["name"], self._tle_detail(sat))
        return {"satellite": sat}

    def update_satellite(self, item_id, payload, user, **_):
        before = (self.planner.satellites.get(item_id) or {}).get("tle")
        sat = self.planner.save_satellite(payload, item_id, user=user)
        changed = " (new TLE)" if sat.get("tle") != before else ""
        self._audit(user, "saved", "satellite", item_id, sat["name"], self._tle_detail(sat) + changed)
        return {"satellite": sat}

    def delete_satellite(self, item_id, user, **_):
        sat = self.planner.delete_satellite(item_id)
        self._audit(user, "deleted", "satellite", item_id, sat["name"])
        return {"deleted": item_id}

    def set_tle(self, item_id, payload, user, **_):
        source = payload.get("source") or "manual"
        sat = self.planner.set_tle(item_id, payload.get("tle"), source, user=user)
        if source == "manual":
            self._audit(user, "updated TLE", "satellite", item_id, sat["name"], self._tle_detail(sat))
        return {"satellite": sat, "result": sat.pop("ingest", None)}

    def ingest_tles(self, payload, user, **_):
        text = payload.get("tle") or payload.get("text") or payload.get("tles")
        if isinstance(text, list):
            text = "\n".join(str(t) for t in text)
        if not isinstance(text, str) or not text.strip():
            raise PlannerError("Send the TLEs as text in 'tle' (2 or 3 lines each, any number)")
        return self.planner.intake.ingest(text, payload.get("source") or "api", user)

    def accept_held_tle(self, item_id, user, **_):
        held = self.planner.intake.accept_held(item_id, user)
        sat = self.planner.satellites[item_id]
        self._audit(
            user, "accepted held TLE", "satellite", item_id, sat["name"], f"from {held['source']}: {held['reason']}"
        )
        return {"satellite": self.planner._satellite_json(sat, self.planner.clock())}

    def reject_held_tle(self, item_id, user, **_):
        held = self.planner.intake.reject_held(item_id)
        sat = self.planner.satellites[item_id]
        self._audit(user, "rejected held TLE", "satellite", item_id, sat["name"], f"from {held['source']}")
        return {"satellite": self.planner._satellite_json(sat, self.planner.clock())}

    def get_tle_inputs(self, **_):
        return self.planner.intake.settings_json()

    def save_tle_inputs(self, payload, user, **_):
        result = self.planner.intake.save_settings(payload)
        self._audit(
            user,
            "saved",
            "tle inputs",
            detail=f"stash {result['stash_key'] or 'off'}, {len(result['files'])} files, "
            f"{len(result['packets'])} packets, max jump {result['max_jump_km']:g} km",
        )
        return result

    def get_tle_sources(self, **_):
        return self.planner.fetcher.settings_json()

    def save_tle_sources(self, payload, user, **_):
        result = self.planner.fetcher.save_settings(payload)
        detail = "; ".join(
            f"{k} every {result[k]['hours']:g} h from {result[k]['url']}" for k in ("celestrak", "spacetrack")
        )
        self._audit(user, "changed", "tle sources", detail=detail)
        return result

    def fetch_tles(self, payload, user, **_):
        result = self.planner.fetcher.fetch_now(payload.get("kind"))
        self._audit(user, "fetched TLEs", "tle sources", detail=", ".join(result))
        return result

    @staticmethod
    def _station_detail(station):
        return (
            f"{station['latitude']:.4f}, {station['longitude']:.4f}, mask {station['min_elevation']:g}°, "
            f"capacity {station.get('capacity', 1)}"
        )

    def create_station(self, payload, user, **_):
        station = self.planner.save_station(payload, user=user)
        self._audit(user, "added", "station", station["id"], station["name"], self._station_detail(station))
        return {"station": station}

    def update_station(self, item_id, payload, user, **_):
        station = self.planner.save_station(payload, item_id, user=user)
        self._audit(user, "saved", "station", item_id, station["name"], self._station_detail(station))
        return {"station": station}

    def delete_station(self, item_id, user, **_):
        station = self.planner.delete_station(item_id)
        self._audit(user, "deleted", "station", item_id, station["name"])
        return {"deleted": item_id}

    @staticmethod
    def _activity_detail(activity):
        contact = activity["contact"]
        offset = float(activity.get("offset") or 0)
        return (
            f"{activity['template_name']} at {activity['anchor'].upper()} {'+' if offset >= 0 else '-'} "
            f"{abs(offset):g}s of {contact['satellite']} over {contact['station']} ({activity['mode']})"
        )

    def list_activities(self, **_):
        return self.planner.list_activities()

    def create_activity(self, payload, user, **_):
        activity = self.planner.create_activity(payload, user=user)
        self._audit(user, "scheduled", "activity", activity["id"], activity["label"], self._activity_detail(activity))
        return {"activity": activity}

    def update_activity(self, item_id, payload, user, **_):
        activity = self.planner.update_activity(item_id, payload, user=user)
        self._audit(user, "changed", "activity", item_id, activity["label"], self._activity_detail(activity))
        return {"activity": activity}

    def delete_activity(self, item_id, user, **_):
        activity = self.planner.delete_activity(item_id, user=user)
        action = "cancelled" if activity.get("rule_id") and activity["status"] == "cancelled" else "deleted"
        self._audit(user, action, "activity", item_id, activity["label"], f"was {activity['status']}")
        return {"deleted": item_id}

    def accept_activity(self, item_id, user, **_):
        activity = self.planner.accept_suggestion(item_id, user=user)
        self._audit(user, "accepted new time", "activity", item_id, activity["label"], self._activity_detail(activity))
        return {"activity": activity}

    def refresh_template(self, item_id, user, **_):
        activity = self.planner.refresh_template(item_id, user=user)
        self._audit(user, "updated from template", "activity", item_id, activity["label"])
        return {"activity": activity}

    def rehearse_activity(self, item_id, payload, user, **_):
        run = self.planner.rehearse(item_id, user=user, skip_waits=payload.get("skip_waits", True))
        activity = self.planner.activities.get(item_id) or {}
        self._audit(user, "rehearsed", "activity", item_id, activity.get("label"), f"dry run {run.id}")
        return {"run": run.as_json(include_steps=False)}

    def load_demo(self, user, **_):
        from .demo import load_demo

        try:
            summary = load_demo(self.engine, user=user_name(user))
        except RuntimeError as error:
            raise EngineError(str(error)) from None
        self._audit(user, "loaded the demo", "engine", detail=str(summary["first_pass"]))
        return summary

    def clear_demo(self, user, **_):
        from .demo import clear_demo

        removed = clear_demo(self.engine)
        self._audit(user, "removed the demo", "engine", detail=str(removed))
        return removed

    def get_alerts(self, **_):
        return self.engine.alerts.settings_json()

    def save_alerts(self, payload, user, **_):
        result = self.engine.alerts.save_settings(payload)
        hooks = ", ".join(h["name"] for h in result["webhooks"]) or "no webhooks"
        cosmos = "COSMOS notifications " + ("on" if result["cosmos"]["enabled"] else "off")
        self._audit(user, "saved", "alerts", detail=f"{cosmos}; {hooks}")
        return result

    def test_alert(self, payload, user, **_):
        result = self.engine.alerts.test(payload.get("webhook_id"))
        self._audit(user, "tested", "alerts", detail=str(payload.get("webhook_id") or "COSMOS notifications"))
        return result

    def list_rules(self, **_):
        return self.planner.list_rules()

    def create_rule(self, payload, user, **_):
        result = self.planner.save_rule(payload, user=user)
        rule = result["rule"]
        self._audit(user, "created", "rule", rule["id"], rule["name"], f"scheduled {result['created']} activities")
        return result

    def update_rule(self, item_id, payload, user, **_):
        result = self.planner.save_rule(payload, item_id, user=user)
        rule = result["rule"]
        self._audit(
            user,
            "saved",
            "rule",
            item_id,
            rule["name"],
            f"{'enabled' if rule['enabled'] else 'disabled'}; replaced {result['removed']} with {result['created']}",
        )
        return result

    def delete_rule(self, item_id, user, **_):
        rule, removed = self.planner.delete_rule(item_id)
        self._audit(user, "deleted", "rule", item_id, rule["name"], f"removed {removed} scheduled activities")
        return {"deleted": item_id, "removed": removed}

    def pause_scheduler(self, payload, user, **_):
        reason = payload.get("reason") or f"Paused by {user_name(user)}"
        self._audit(user, "paused", "scheduler", detail=reason)
        return self.planner.set_paused(True, reason)

    def resume_scheduler(self, user, **_):
        self._audit(user, "resumed", "scheduler")
        return self.planner.set_paused(False)


def make_handler(api, logger=None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = SOCKET_TIMEOUT_SECONDS  # a client that stops sending doesn't hold a thread forever
        server_version = "FlowEngine"
        sys_version = ""

        def _dispatch(self, method):
            parsed = urlparse(self.path)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0:
                self.close_connection = True
                self._send(400, {"error": "Bad Content-Length"})
                return
            restoring = method == "POST" and parsed.path.rstrip("/").endswith("/api/restore")
            if length > (MAX_RESTORE_BYTES if restoring else MAX_BODY_BYTES):
                self.close_connection = True
                self._send(413, {"error": "Request body too large"})
                return
            if length:
                # Every route with a body needs a token: check it before reading (and holding) the body
                refused = api.preauthorize(method, parsed.path, self.headers)
                if refused:
                    self.close_connection = True
                    self._send(*refused)
                    return
            if restoring and not _restoring.acquire(blocking=False):
                self.close_connection = True
                self._send(409, {"error": "A restore is already running"})
                return
            try:
                body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
                try:
                    status, result = api.handle(method, parsed.path, parse_qs(parsed.query), self.headers, body)
                except Exception as error:  # noqa: BLE001 - never drop the connection without a response
                    if logger:
                        logger.error(f"Flow API {method} {parsed.path} failed: {type(error).__name__}: {error}")
                    status, result = 500, {"error": "Internal error in the flow engine (details are in its log)"}
            finally:
                if restoring:
                    _restoring.release()
            self._send(status, result)

        def _send(self, status, result):
            data = json.dumps(result, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler naming
            self._dispatch("GET")

        def do_POST(self):  # noqa: N802
            self._dispatch("POST")

        def do_PUT(self):  # noqa: N802
            self._dispatch("PUT")

        def do_DELETE(self):  # noqa: N802
            self._dispatch("DELETE")

        def log_message(self, format, *args):  # noqa: A002 - quiet; COSMOS logs are for events, not requests
            pass

    return Handler


class _BoundedHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a cap on concurrent requests: past it, new connections get a quick 503."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
        self._priority = threading.BoundedSemaphore(MAX_PRIORITY_REQUESTS)

    def process_request(self, request, client_address):
        if self._slots.acquire(blocking=False):
            thread = threading.Thread(
                target=self._serve, args=(request, client_address, self._slots), name="flow-http-req", daemon=True
            )
        elif self._priority.acquire(blocking=False):
            thread = threading.Thread(
                target=self._serve_priority, args=(request, client_address), name="flow-http-prio", daemon=True
            )
        else:
            self._refuse(request)
            return
        thread.start()

    def _serve(self, request, client_address, slots):
        try:
            self.process_request_thread(request, client_address)
        finally:
            slots.release()

    def _serve_priority(self, request, client_address):
        """All slots busy: serve this connection only if it asks for STOP ALL or /health."""
        try:
            try:
                request.settimeout(PRIORITY_PEEK_SECONDS)
                head = request.recv(512, socket.MSG_PEEK)
            except OSError:
                head = b""
            if not _PRIORITY_PATH.match(head):
                self._refuse(request)
                return
            self.process_request_thread(request, client_address)
        finally:
            self._priority.release()

    def _refuse(self, request):
        with contextlib.suppress(OSError):
            request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        self.shutdown_request(request)


class FlowHttpServer:
    def __init__(self, api, port, logger=None, host="0.0.0.0"):  # noqa: S104 - must be reachable by Traefik
        self.server = _BoundedHTTPServer((host, port), make_handler(api, logger))
        self.server.daemon_threads = True
        self.thread = None

    @property
    def port(self):
        return self.server.server_address[1]

    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever, name="flow-http", daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
