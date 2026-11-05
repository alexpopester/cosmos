"""Where a satellite's TLE comes from, and fetching it from CelesTrak or Space-Track.

Each satellite has a ``tle_source``:

* ``{"kind": "manual"}``: pasted in the tool (or sent through the TLE inputs).
* ``{"kind": "celestrak"}``: CelesTrak's GP API, one request per catalog number.
* ``{"kind": "spacetrack"}``: Space-Track's GP class, one query for every
  satellite, logged in with COSMOS secrets (Admin Console → Secrets; the names
  are settings, defaulting to ``SPACETRACK_USERNAME`` and ``SPACETRACK_PASSWORD``).
  The credentials are read from COSMOS when needed: they're never stored by the
  engine, shown by the API or put in backups. They're only ever sent to
  Space-Track itself: its address is fixed (an admin can point it at another
  https host with the ``FLOW_SPACETRACK_URL`` environment variable, never
  through the API or a backup), and secret names must start with
  ``SPACETRACK_``, so the setting can't name another COSMOS secret.

Internet sources fetch by the satellite's catalog number. A fetched TLE is
never applied directly: it goes through ``TleIntake.ingest`` with the source's
kind, so the safeguards apply (catalog match, newer epoch, preferred source,
held for review on a big jump).

Politeness (both services block clients that ask too often):

* each catalog number is fetched at most every ``hours`` (default 4), and
  never more often than ``min_hours`` (CelesTrak 2, Space-Track 1), even when
  someone presses Fetch now;
* a failure of the whole source (network, login, refused) backs off from 10
  minutes, doubling up to ``hours``. When a service refused (403, 429, rate
  limit), Fetch now waits for the back-off too;
* when each catalog number was fetched, and the back-off, are kept in Valkey,
  so restarting the engine doesn't fetch everything again;
* a request that fails part way through a batch keeps what was fetched
  before it;
* no redirects are followed, and responses are size-limited.

A source that starts failing raises the ``tle_source_failing`` alert once,
until it works again.

To add a source: subclass ``TleSource``, implement ``fetch`` and register it
in ``SOURCES``. The planner's TLE input thread calls ``TleFetcher.poll`` every
second; network calls happen outside the planner's lock.
"""

import contextlib
import copy
import os
import re
import time
import types
from urllib.parse import urlparse

import requests

from .alerts import blocked_address
from .tle_inputs import split_tles


TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
FIRST_BACKOFF_SECONDS = 600
CELESTRAK_SPACING_SECONDS = 1.0  # between requests for different catalog numbers
SPACETRACK_BATCH = 100  # catalog numbers per query (keeps the URL short)
# Where the Space-Track login is sent: fixed, never a setting a user (or a backup) can change
SPACETRACK_URL = (os.environ.get("FLOW_SPACETRACK_URL") or "https://www.space-track.org").rstrip("/")
SECRET_PREFIX = "SPACETRACK_"
# A catalog number: up to 9 digits, or Alpha-5 (a letter other than I or O, then 4 digits)
_CATALOG_RE = re.compile(r"^(\d{1,9}|[A-HJ-NP-Z]\d{4})$")
_ALPHA5 = "ABCDEFGHJKLMNPQRSTUVWXYZ"
DEFAULT_SETTINGS = {
    "celestrak": {"url": "https://celestrak.org", "hours": 4.0},
    "spacetrack": {
        "url": SPACETRACK_URL,
        "hours": 4.0,
        "username_secret": "SPACETRACK_USERNAME",
        "password_secret": "SPACETRACK_PASSWORD",
    },
}


class FetchError(Exception):
    """The whole source failed (network, login, refused): back off and try again later.

    ``refused``: the service itself refused (a ban or rate limit), so not even Fetch now may retry early.
    ``partial``: (texts, reasons) fetched before the failure, which still count.
    """

    def __init__(self, message, refused=False, partial=None):
        super().__init__(message)
        self.refused = refused
        self.partial = partial


class TleSourceError(Exception):
    status = 400


def catalog_key(value):
    """Catalog numbers compare as text without leading zeros ("05544" is 5544; Alpha-5 like "A0001" as is)."""
    text = str(value or "").strip().upper()
    return str(int(text)) if text.isdigit() else text


def _tle_catalog(line1):
    return catalog_key(line1[2:7])


def valid_catalog(value):
    """True for a catalog number the internet sources can be asked for (digits, or Alpha-5)."""
    return bool(_CATALOG_RE.match(str(value or "").strip().upper()))


def catalog_number(key):
    """The integer catalog number for a query: Alpha-5 "A0001" is 100001 (A=10, skipping I and O)."""
    text = str(key).strip().upper()
    if text.isdigit():
        return int(text)
    return (_ALPHA5.index(text[0]) + 10) * 10000 + int(text[1:])


def _check_https_host(url, label):
    """Refuse an address the login or a request mustn't go to (checked again at every fetch)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise FetchError(f"{label}'s address isn't a web address")
    blocked = blocked_address(parsed.hostname)
    if blocked:
        raise FetchError(f"{label} address: {blocked}")
    return parsed


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
    """GET with no redirects, reading at most MAX_RESPONSE_BYTES (a huge answer is stopped, not buffered)."""
    try:
        response = session.get(url, timeout=TIMEOUT_SECONDS, allow_redirects=False, stream=True, **kwargs)
        if not hasattr(response, "iter_content"):
            body = response.content or b""  # a test double
        else:
            chunks, total = [], 0
            for chunk in response.iter_content(64 * 1024):
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    response.close()
                    raise FetchError(f"{what}'s answer was too large")
                chunks.append(chunk)
            body = b"".join(chunks)
    except requests.RequestException as error:
        raise FetchError(f"Couldn't reach {what}: {type(error).__name__}") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise FetchError(f"{what}'s answer was too large")
    encoding = getattr(response, "encoding", None) or "utf-8"
    return types.SimpleNamespace(status_code=response.status_code, text=body.decode(encoding, errors="replace"))


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
        _check_https_host(base, "CelesTrak")
        for index, catalog in enumerate(catalogs):
            if index:
                time.sleep(CELESTRAK_SPACING_SECONDS)  # one request at a time, not a burst
            try:
                response = _get(
                    session,
                    f"{base}/NORAD/elements/gp.php",
                    "CelesTrak",
                    params={"CATNR": str(catalog_number(catalog)), "FORMAT": "TLE"},
                )
            except FetchError as error:
                raise FetchError(str(error), partial=(texts, reasons)) from None
            if response.status_code in (403, 429):
                raise FetchError(
                    f"CelesTrak refused the request ({response.status_code}). It blocks clients that fetch the same "
                    "data too often; the engine waits before trying again",
                    refused=True,
                    partial=(texts, reasons),
                )
            if response.status_code != 200:
                raise FetchError(f"CelesTrak answered {response.status_code}", partial=(texts, reasons))
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
        for key in ("username_secret", "password_secret"):
            if not str(settings.get(key) or "").startswith(SECRET_PREFIX):
                raise FetchError(f"Space-Track secret names must start with {SECRET_PREFIX}")
        # The login only ever goes to the fixed Space-Track address, over https
        base = SPACETRACK_URL
        if _check_https_host(base, "Space-Track").scheme != "https":
            raise FetchError("Space-Track's address must use https: the login is sent to it")
        username = backend.get_secret(settings["username_secret"])
        password = backend.get_secret(settings["password_secret"])
        if not username or not password:
            raise FetchError(
                f"Set the COSMOS secrets {settings['username_secret']} and {settings['password_secret']} "
                "(Admin Console → Secrets) to your Space-Track login"
            )
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
            raise FetchError(
                f"Space-Track login failed ({login.status_code}): check the COSMOS secrets",
                refused=login.status_code in (401, 403, 429),
            )
        texts, reasons = {}, {}
        try:
            ordered = sorted(catalogs)
            for start in range(0, len(ordered), SPACETRACK_BATCH):
                batch = ordered[start : start + SPACETRACK_BATCH]
                numbers = ",".join(str(catalog_number(c)) for c in batch)
                url = f"{base}/basicspacedata/query/class/gp/NORAD_CAT_ID/{numbers}/orderby/NORAD_CAT_ID/format/3le"
                try:
                    response = _get(session, url, "Space-Track")
                except FetchError as error:
                    raise FetchError(str(error), partial=(texts, reasons)) from None
                if response.status_code == 429 or "rate limit" in (response.text or "").lower():
                    raise FetchError(
                        "Space-Track says the query rate limit was exceeded; the engine waits",
                        refused=True,
                        partial=(texts, reasons),
                    )
                if response.status_code != 200:
                    raise FetchError(
                        f"Space-Track answered {response.status_code}",
                        refused=response.status_code in (401, 403),
                        partial=(texts, reasons),
                    )
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
        records = self.planner.store.records("planner_settings")
        self._load_state(records.get("tle_fetch_state") or {})
        saved = records.get("tle_sources") or {}
        for kind, defaults in DEFAULT_SETTINGS.items():
            self.settings[kind] = {**defaults, **(saved.get(kind) or {})}
        # Whatever was stored (an older version, a restored backup): Space-Track's address is the fixed one,
        # and its secret names must be Space-Track's
        spacetrack = self.settings["spacetrack"]
        spacetrack["url"] = SPACETRACK_URL
        for key in ("username_secret", "password_secret"):
            if not str(spacetrack.get(key) or "").startswith(SECRET_PREFIX):
                spacetrack[key] = DEFAULT_SETTINGS["spacetrack"][key]

    def _load_state(self, state):
        """When each catalog number was fetched, and each source's back-off, as saved before a restart."""
        for key, when in (state.get("fetched") or {}).items():
            kind, _, catalog = str(key).partition(":")
            if kind in DEFAULT_SETTINGS and catalog and isinstance(when, (int, float)):
                self.fetched[(kind, catalog)] = float(when)
        for kind, saved in (state.get("sources") or {}).items():
            if kind in DEFAULT_SETTINGS and isinstance(saved, dict):
                for key in ("retry_at", "refused", "error", "last_attempt", "last_fetch"):
                    if saved.get(key) is not None:
                        self.status[kind][key] = saved[key]
                if isinstance(saved.get("backoff"), (int, float)):
                    self.backoff[kind] = float(saved["backoff"])

    def _save_state(self):
        state = {
            "fetched": {f"{kind}:{catalog}": when for (kind, catalog), when in self.fetched.items()},
            "sources": {
                kind: {
                    **{
                        k: self.status[kind].get(k)
                        for k in ("retry_at", "refused", "error", "last_attempt", "last_fetch")
                    },
                    "backoff": self.backoff.get(kind),
                }
                for kind in DEFAULT_SETTINGS
            },
        }
        # Deferred: polite fetching must survive a restart, but a Valkey hiccup mustn't stop fetching
        self.planner.store.save_record("planner_settings", "tle_fetch_state", state, defer=True)

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
            if kind == "spacetrack" and "url" in changes:
                if str(changes["url"] or "").strip().rstrip("/") != SPACETRACK_URL:
                    raise TleSourceError(
                        "Space-Track's address can't be changed here: it receives your login. An admin can set "
                        "the engine's FLOW_SPACETRACK_URL environment variable"
                    )
            elif "url" in changes:
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
                    if not name.startswith(SECRET_PREFIX):
                        raise TleSourceError(
                            f"Space-Track secret names must start with {SECRET_PREFIX} (so this setting can't "
                            "send another COSMOS secret anywhere)"
                        )
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
            status = self.status[name]
            if status.get("refused") and status.get("retry_at") and now < status["retry_at"]:
                # The service refused us: asking again early is what gets a client banned
                result[name] = {
                    "catalogs": len(self._wanted(name)),
                    "now": 0,
                    "waiting_until": {},
                    "refused_until": status["retry_at"],
                }
                continue
            status.pop("retry_at", None)
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
                if not catalog or not valid_catalog(catalog):
                    self.sat_status[sat["id"]] = {
                        "source": kind,
                        "time": None,
                        "result": "rejected",
                        "reason": "Needs a catalog number" if not catalog else f"'{catalog}' isn't a catalog number",
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
        failure = None
        try:
            texts, reasons = source.fetch(due, settings, backend, session)
        except FetchError as error:
            failure = error
            texts, reasons = error.partial or ({}, {})
        finally:
            close = getattr(session, "close", None)
            if close:
                close()
        if failure is not None:
            # What arrived before the failure still counts (and isn't asked for again before its time)
            done = [c for c in due if c in texts or c in reasons]
            self._record(kind, done, texts, reasons, wanted, now)
            delay = min(self.backoff.get(kind, FIRST_BACKOFF_SECONDS / 2) * 2, settings["hours"] * 3600)
            self.backoff[kind] = delay
            first = not status.get("error")
            status.update(error=str(failure), refused=failure.refused, retry_at=now + delay, last_attempt=now)
            self.planner.engine.log("warn", f"TLE fetch from {source.label} failed: {failure}")
            if first:
                self.planner.alert(
                    "tle_source_failing",
                    f"Can't fetch TLEs from {source.label}",
                    f"{failure}. Trying again in {delay / 60:.0f} minutes; satellites keep their current TLEs.",
                    key=f"tle_source_failing:{kind}:{now}",
                )
            self._save_state()
            return
        self.backoff.pop(kind, None)
        counts = self._record(kind, due, texts, reasons, wanted, now)
        status.update(error=None, refused=False, retry_at=None, last_fetch=now, last_attempt=now, counts=counts)
        self._save_state()

    def _record(self, kind, catalogs, texts, reasons, wanted, now):
        """Ingest what was fetched for these catalog numbers and note when each was fetched."""
        counts = {}
        for catalog in catalogs:
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
        return counts
