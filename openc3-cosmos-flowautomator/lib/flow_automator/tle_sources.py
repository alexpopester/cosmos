"""Where a satellite's TLE comes from, and fetching it from CelesTrak or Space-Track.

Each satellite has a ``tle_source``:

* ``{"kind": "manual"}``: pasted in the tool (or sent through the TLE inputs).
* ``{"kind": "celestrak"}``: CelesTrak's GP API, one request per catalog number.
* ``{"kind": "spacetrack"}``: Space-Track's GP class, one query for every
  satellite, logged in with COSMOS secrets (Admin Console → Secrets; the names
  are settings, defaulting to ``SPACETRACK_USERNAME`` and ``SPACETRACK_PASSWORD``).
  The credentials are read from COSMOS when needed: they're never stored by the
  engine, shown by the API or put in backups.

Internet sources fetch by the satellite's catalog number. A fetched TLE is
never applied directly: it goes through ``TleIntake.ingest`` with the source's
kind, so the safeguards apply (catalog match, newer epoch, preferred source,
held for review on a big jump).

Politeness (both services block clients that ask too often):

* each catalog number is fetched at most every ``hours`` (default 4), and
  never more often than ``min_hours`` (CelesTrak 2, Space-Track 1), even when
  someone presses Fetch now;
* a failure of the whole source (network, login, refused) backs off from 10
  minutes, doubling up to ``hours``;
* no redirects are followed, and responses are size-limited.

To add a source: subclass ``TleSource``, implement ``fetch`` and register it
in ``SOURCES``. The planner's TLE input thread calls ``TleFetcher.poll`` every
second; network calls happen outside the planner's lock.
"""

import contextlib
import copy
from urllib.parse import urlparse

import requests

from .alerts import blocked_address
from .tle_inputs import split_tles


TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
FIRST_BACKOFF_SECONDS = 600
SPACETRACK_BATCH = 100  # catalog numbers per query (keeps the URL short)
DEFAULT_SETTINGS = {
    "celestrak": {"url": "https://celestrak.org", "hours": 4.0},
    "spacetrack": {
        "url": "https://www.space-track.org",
        "hours": 4.0,
        "username_secret": "SPACETRACK_USERNAME",
        "password_secret": "SPACETRACK_PASSWORD",
    },
}


class FetchError(Exception):
    """The whole source failed (network, login, refused): back off and try again later."""


class TleSourceError(Exception):
    status = 400


def catalog_key(value):
    """Catalog numbers compare as text without leading zeros ("05544" is 5544; Alpha-5 like "A0001" as is)."""
    text = str(value or "").strip().upper()
    return str(int(text)) if text.isdigit() else text


def _tle_catalog(line1):
    return catalog_key(line1[2:7])


class TleSource:
    kind = ""
    label = ""
    internet = False
    min_hours = None

    def check_config(self, config):
        """Problems with a satellite's source settings, as messages."""
        return []

    def fetch(self, catalogs, settings, backend, session):
        """Fetch TLEs for catalog numbers. Returns ({catalog: tle text}, {catalog: why there's none}).

        Raises FetchError if the source as a whole failed.
        """
        return {}, {}


class ManualSource(TleSource):
    kind = "manual"
    label = "Pasted, or sent by other apps"


def _get(session, url, what, **kwargs):
    try:
        response = session.get(url, timeout=TIMEOUT_SECONDS, allow_redirects=False, **kwargs)
    except requests.RequestException as error:
        raise FetchError(f"Couldn't reach {what}: {type(error).__name__}") from None
    if len(response.content or b"") > MAX_RESPONSE_BYTES:
        raise FetchError(f"{what}'s answer was too large")
    return response


def _by_catalog(text, wanted):
    found = {}
    entries, _problems = split_tles(text)
    for entry in entries:
        lines = entry["text"].split("\n")
        catalog = _tle_catalog(lines[-2])
        if catalog in wanted:
            found[catalog] = entry["text"]
    return found


class CelestrakSource(TleSource):
    kind = "celestrak"
    label = "CelesTrak"
    internet = True
    min_hours = 2  # CelesTrak updates GP data about every 2 hours and blocks clients that ask more often

    def fetch(self, catalogs, settings, backend, session):
        texts, reasons = {}, {}
        base = settings["url"].rstrip("/")
        for catalog in catalogs:
            response = _get(
                session, f"{base}/NORAD/elements/gp.php", "CelesTrak", params={"CATNR": catalog, "FORMAT": "TLE"}
            )
            if response.status_code == 403:
                raise FetchError(
                    "CelesTrak refused the request (403). It blocks clients that fetch the same data too often; "
                    "the engine waits before trying again"
                )
            if response.status_code != 200:
                raise FetchError(f"CelesTrak answered {response.status_code}")
            found = _by_catalog(response.text, {catalog})
            if catalog in found:
                texts[catalog] = found[catalog]
            elif "no gp data" in response.text.lower():
                reasons[catalog] = "CelesTrak has no element set for this catalog number (decayed, or not public?)"
            else:
                reasons[catalog] = "CelesTrak's answer had no TLE for this catalog number"
        return texts, reasons


class SpacetrackSource(TleSource):
    kind = "spacetrack"
    label = "Space-Track"
    internet = True
    min_hours = 1  # Space-Track asks for GP data to be fetched at most once an hour

    def fetch(self, catalogs, settings, backend, session):
        username = backend.get_secret(settings["username_secret"])
        password = backend.get_secret(settings["password_secret"])
        if not username or not password:
            raise FetchError(
                f"Set the COSMOS secrets {settings['username_secret']} and {settings['password_secret']} "
                "(Admin Console → Secrets) to your Space-Track login"
            )
        base = settings["url"].rstrip("/")
        try:
            login = session.post(
                f"{base}/ajaxauth/login",
                data={"identity": username, "password": password},
                timeout=TIMEOUT_SECONDS,
                allow_redirects=False,
            )
        except requests.RequestException as error:
            raise FetchError(f"Couldn't reach Space-Track: {type(error).__name__}") from None
        if login.status_code != 200 or "failed" in (login.text or "").lower():
            raise FetchError(f"Space-Track login failed ({login.status_code}): check the COSMOS secrets")
        texts, reasons = {}, {}
        try:
            ordered = sorted(catalogs)
            for start in range(0, len(ordered), SPACETRACK_BATCH):
                batch = ordered[start : start + SPACETRACK_BATCH]
                url = (
                    f"{base}/basicspacedata/query/class/gp/NORAD_CAT_ID/{','.join(batch)}"
                    "/orderby/NORAD_CAT_ID/format/3le"
                )
                response = _get(session, url, "Space-Track")
                if response.status_code == 429 or "rate limit" in (response.text or "").lower():
                    raise FetchError("Space-Track says the query rate limit was exceeded; the engine waits")
                if response.status_code != 200:
                    raise FetchError(f"Space-Track answered {response.status_code}")
                texts.update(_by_catalog(response.text, set(batch)))
        finally:
            with contextlib.suppress(Exception):  # logging out is a courtesy
                session.get(f"{base}/ajaxauth/logout", timeout=TIMEOUT_SECONDS, allow_redirects=False)
        for catalog in catalogs:
            if catalog not in texts:
                reasons[catalog] = "Space-Track has no current element set for this catalog number (decayed?)"
        return texts, reasons


SOURCES = {source.kind: source for source in (ManualSource(), CelestrakSource(), SpacetrackSource())}


def get_source(kind):
    source = SOURCES.get(kind or "manual")
    if source is None:
        raise KeyError(f"Unknown TLE source '{kind}' (known: {', '.join(SOURCES)})")
    return source


class TleFetcher:
    """Keeps satellites whose source is CelesTrak or Space-Track current."""

    def __init__(self, planner, session_factory=requests.Session):
        self.planner = planner
        self.session_factory = session_factory
        self.settings = copy.deepcopy(DEFAULT_SETTINGS)
        self.fetched = {}  # (kind, catalog) -> when it was last fetched
        self.status = {kind: {} for kind in DEFAULT_SETTINGS}  # last_fetch, error, retry_at, counts
        self.backoff = {}
        self.forced = set()  # kinds someone asked to fetch now
        self.sat_status = {}  # satellite id -> {"source", "time", "result", "reason"}

    # ---- settings ----------------------------------------------------------------

    def load(self):
        saved = self.planner.store.records("planner_settings").get("tle_sources") or {}
        for kind, defaults in DEFAULT_SETTINGS.items():
            self.settings[kind] = {**defaults, **(saved.get(kind) or {})}

    def settings_json(self):
        result = copy.deepcopy(self.settings)
        backend = self.planner.engine.backend
        spacetrack = result["spacetrack"]
        try:
            spacetrack["credentials_set"] = bool(
                backend.get_secret(spacetrack["username_secret"]) and backend.get_secret(spacetrack["password_secret"])
            )
        except Exception:  # noqa: BLE001 - say we couldn't tell rather than fail the dialog
            spacetrack["credentials_set"] = None
        with self.planner.lock:
            sats = list(self.planner.satellites.values())
        for kind in DEFAULT_SETTINGS:
            result[kind]["min_hours"] = SOURCES[kind].min_hours
            result[kind]["status"] = copy.deepcopy(self.status[kind])
            result[kind]["satellites"] = sum(1 for s in sats if (s.get("tle_source") or {}).get("kind") == kind)
        return result

    def save_settings(self, payload):
        if not isinstance(payload, dict):
            raise TleSourceError("TLE source settings must be a JSON object")
        settings = copy.deepcopy(self.settings)
        for kind in DEFAULT_SETTINGS:
            changes = payload.get(kind)
            if changes is None:
                continue
            if not isinstance(changes, dict):
                raise TleSourceError(f"'{kind}' settings must be an object")
            label = SOURCES[kind].label
            current = settings[kind]
            if "url" in changes:
                url = str(changes["url"] or "").strip().rstrip("/")
                parsed = urlparse(url)
                if parsed.scheme not in ("http", "https") or not parsed.netloc:
                    raise TleSourceError(f"{label} address must start with https:// (or http:// for a local mirror)")
                blocked = blocked_address(parsed.hostname)
                if blocked:
                    raise TleSourceError(f"{label} address: {blocked}")
                current["url"] = url
            if "hours" in changes:
                try:
                    hours = float(changes["hours"])
                except (TypeError, ValueError):
                    raise TleSourceError(f"{label}: hours must be a number") from None
                low = SOURCES[kind].min_hours
                if not low <= hours <= 168:
                    raise TleSourceError(
                        f"{label} may be fetched every {low} to 168 hours (it blocks clients asking more often)"
                    )
                current["hours"] = hours
            for key in ("username_secret", "password_secret"):
                if kind == "spacetrack" and key in changes:
                    name = str(changes[key] or "").strip()
                    if not name or len(name) > 100 or any(c.isspace() for c in name):
                        raise TleSourceError("Secret names must be non-empty, without spaces")
                    current[key] = name
        self.planner.store.save_record("planner_settings", "tle_sources", settings)
        self.settings = settings
        for kind in DEFAULT_SETTINGS:
            # New settings (a fixed address, new secrets) deserve a prompt retry
            self.status[kind].pop("retry_at", None)
            self.backoff.pop(kind, None)
        return self.settings_json()

    # ---- fetching ----------------------------------------------------------------

    def fetch_now(self, kind=None):
        """Fetch every catalog number of this source (all internet sources if None) that's allowed now."""
        kinds = [kind] if kind else [k for k, s in SOURCES.items() if s.internet]
        for name in kinds:
            if name not in SOURCES or not SOURCES[name].internet:
                raise TleSourceError(f"'{name}' isn't an internet TLE source")
        now = self.planner.clock()
        result = {}
        for name in kinds:
            self.forced.add(name)
            self.status[name].pop("retry_at", None)
            self.backoff.pop(name, None)
            catalogs = self._wanted(name)
            waiting = {
                c: self.fetched[(name, c)] + SOURCES[name].min_hours * 3600
                for c in catalogs
                if (name, c) in self.fetched and now - self.fetched[(name, c)] < SOURCES[name].min_hours * 3600
            }
            result[name] = {"catalogs": len(catalogs), "now": len(catalogs) - len(waiting), "waiting_until": waiting}
        return result

    def _wanted(self, kind):
        """Catalog numbers of enabled satellites using this source -> [satellite ids]."""
        wanted = {}
        with self.planner.lock:
            for sat in self.planner.satellites.values():
                if (sat.get("tle_source") or {}).get("kind") != kind or sat.get("enabled") is False:
                    continue
                catalog = catalog_key(sat.get("catalog") or (sat.get("tle") or {}).get("norad_id"))
                if not catalog:
                    self.sat_status[sat["id"]] = {
                        "source": kind,
                        "time": None,
                        "result": "rejected",
                        "reason": "Needs a catalog number",
                    }
                    continue
                wanted.setdefault(catalog, []).append(sat["id"])
        return wanted

    def poll(self, now):
        backend = self.planner.engine.backend
        for kind, source in SOURCES.items():
            if not source.internet:
                continue
            status = self.status[kind]
            if status.get("retry_at") and now < status["retry_at"]:
                continue
            settings = self.settings[kind]
            interval = (source.min_hours if kind in self.forced else settings["hours"]) * 3600
            wanted = self._wanted(kind)
            due = [c for c in wanted if now - self.fetched.get((kind, c), -1e18) >= interval]
            self.forced.discard(kind)
            if not due:
                continue
            self._fetch(kind, source, settings, due, wanted, backend, now)

    def _fetch(self, kind, source, settings, due, wanted, backend, now):
        status = self.status[kind]
        session = self.session_factory()
        try:
            texts, reasons = source.fetch(due, settings, backend, session)
        except FetchError as error:
            delay = min(self.backoff.get(kind, FIRST_BACKOFF_SECONDS / 2) * 2, settings["hours"] * 3600)
            self.backoff[kind] = delay
            status.update(error=str(error), retry_at=now + delay, last_attempt=now)
            self.planner.engine.log("warn", f"TLE fetch from {source.label} failed: {error}")
            return
        finally:
            close = getattr(session, "close", None)
            if close:
                close()
        self.backoff.pop(kind, None)
        counts = {}
        for catalog in due:
            self.fetched[(kind, catalog)] = now
            for sat_id in wanted[catalog]:
                if catalog in texts:
                    outcome = self.planner.intake.ingest(texts[catalog], kind, satellite_id=sat_id, now=now)
                    first = (outcome["results"] or [{"result": "rejected", "reason": "; ".join(outcome["problems"])}])[
                        0
                    ]
                    entry = {"result": first["result"], "reason": first.get("reason", ""), "epoch": first.get("epoch")}
                else:
                    entry = {"result": "missing", "reason": reasons.get(catalog, "No TLE in the answer")}
                self.sat_status[sat_id] = {"source": kind, "time": now, **entry}
                counts[entry["result"]] = counts.get(entry["result"], 0) + 1
        status.update(error=None, retry_at=None, last_fetch=now, last_attempt=now, counts=counts)
