"""Contact planning: satellites, ground stations, predicted passes and scheduled activities.

* Satellites carry a TLE (see ``tle_sources``); stations a location, an
  elevation mask and a capacity (how many satellites they can work at once).
  Contacts (passes) are predicted for every enabled satellite x station pair
  from ``lookback`` ago to ``horizon`` ahead and kept in memory.
* Prediction runs in a background thread, one satellite at a time, and only
  for what changed: a new TLE recomputes that satellite, a station edit that
  station. Everything is refreshed every ``recompute_hours`` so the window
  rolls forward. API calls never wait for it; ``status()["predicting"]``
  says how many satellites are queued. (Without the thread, as in tests and
  before ``start()``, prediction runs inline.)
* An activity is a snapshot of a flow (usually a template), its parameter
  values, a contact and a start time relative to it: ``AOS - 120`` or
  ``LOS - 90``. The planner starts it as a run at that time with the contact's
  details as run variables (``contact_aos``, ``contact_los`` ...).
* A rule schedules a template into every matching contact (satellites,
  stations, minimum elevation and duration) as contacts are predicted.
  Deleting an activity a rule made cancels it, so the rule won't put it back.
  Higher priority rules claim contacts first, and a rule can skip contacts
  whose station is already at capacity.
* When contacts are recomputed each scheduled activity follows its pass: the
  predicted pass of the same satellite and station whose TCA is closest, if it
  moved less than ``retime_tolerance`` seconds. Otherwise it needs review and
  won't run until someone moves it (a rule's activity is cancelled and the
  rule schedules the new pass instead).
* An activity that couldn't start on time (engine down, scheduler paused)
  starts late if it's within its ``late_tolerance``, else it's marked missed.
* Finished activities are deleted ``retention_days`` after they end.
"""

import contextlib
import copy
import threading
import time
import uuid

from .audit import user_name
from .orbit import (
    SGP4_IMPORT_ERROR,
    OrbitError,
    Propagator,
    Station,
    find_passes_for_stations,
    parse_tle,
)
from .templates import fill_parameters
from .tle_inputs import TleIntake
from .tle_sources import get_source


ANCHORS = ("aos", "tca", "los")
WAITING = ("scheduled", "needs_review")
FINISHED = ("done", "failed", "stopped", "missed", "cancelled")
TLE_HISTORY = 5
SCHEDULER = "scheduler"
RULE_LEAD_SECONDS = 10  # a rule doesn't create activities that would start sooner than this
DEFAULTS = {
    "late_tolerance": 30.0,
    "approval_margin": 30.0,
    "stop_at_los": False,
}


class PlannerError(Exception):
    status = 400


class PlannerNotFoundError(PlannerError):
    status = 404


def _number(value, name, minimum=None, maximum=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise PlannerError(f"{name} must be a number") from None
    if number != number:  # NaN
        raise PlannerError(f"{name} must be a number")
    if minimum is not None and number < minimum:
        raise PlannerError(f"{name} must be at least {minimum:g}")
    if maximum is not None and number > maximum:
        raise PlannerError(f"{name} must be at most {maximum:g}")
    return number


def _short_id():
    return uuid.uuid4().hex[:10]


def activity_start(activity):
    contact = activity["contact"]
    return contact[activity.get("anchor", "aos")] + float(activity.get("offset") or 0)


def _overlap(a, b):
    return a["aos"] < b["los"] and b["aos"] < a["los"]


class ContactPlanner:
    def __init__(
        self,
        engine,
        store,
        clock=time.time,
        horizon_days=7.0,
        lookback_hours=6.0,
        recompute_hours=6.0,
        retime_tolerance=120.0,
        stale_tle_days=3.0,
        retention_days=7.0,
        period=1.0,
    ):
        self.engine = engine
        self.store = store
        self.clock = clock
        self.horizon = horizon_days * 86400
        self.lookback = lookback_hours * 3600
        self.recompute_every = recompute_hours * 3600
        self.retime_tolerance = retime_tolerance
        self.stale_tle_days = stale_tle_days
        self.retention = retention_days * 86400
        self.period = period
        self.lock = threading.RLock()
        self.satellites = {}
        self.stations = {}
        self.activities = {}
        self.rules = {}
        self.settings = {"paused": False, "paused_reason": ""}
        self.pair_contacts = {}  # (satellite id, station id) -> contacts
        self.contacts = []
        self.contacts_by_id = {}
        self.pending = {}  # satellite id -> set of station ids to predict (None = all)
        self.computed_at = None  # last time the whole window was refreshed
        self.compute_seconds = None
        self.problems = {}  # satellite id -> why it has no contacts
        self.last_polled = {}
        self.last_pruned = 0.0
        self.last_rules = 0.0
        self.intake = TleIntake(self)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._worker = None
        self.last_error = None

    # ---- lifecycle ---------------------------------------------------------

    def alert(self, event, title, message="", key=None, path="/tools/flowautomator?view=contacts", **extra):
        alerts = getattr(self.engine, "alerts", None)
        if alerts is not None:
            alerts.emit(event, title, message, path=path, key=key, **extra)

    def audit(self, user, action, kind, target_id=None, target_name=None, detail=""):
        audit = getattr(self.engine, "audit", None)
        if audit is not None:
            audit.record(user, action, kind, target_id, target_name, detail)

    def load(self):
        with self.lock:
            self.satellites = self.store.records("satellites")
            self.stations = self.store.records("stations")
            self.activities = self.store.records("activities")
            self.rules = self.store.records("rules")
            self.settings.update(self.store.records("planner_settings").get("settings", {}))
            self.intake.load()
            now = self.clock()
            for activity in self.activities.values():
                if activity["status"] == "running":
                    # The engine stopped in the middle of the run, which went with it
                    self._finish(activity, "failed", "The flow engine restarted during this activity's run", now)
            self.pair_contacts = {}
            self.contacts = []
            self.contacts_by_id = {}
            self.pending = dict.fromkeys(self.satellites)
            self.computed_at = None
        self.engine.log(
            "info",
            f"Contact planner loaded {len(self.satellites)} satellites, {len(self.stations)} stations, "
            f"{len(self.rules)} rules, "
            f"{len([a for a in self.activities.values() if a['status'] in WAITING])} scheduled activities",
        )

    def start(self):
        self._stop.clear()
        self._worker = threading.Thread(target=self._predict_loop, name="flow-predict", daemon=True)
        self._worker.start()
        self._wake.set()
        self._thread = threading.Thread(target=self._loop, name="flow-contacts", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        for thread in (self._thread, self._worker):
            if thread:
                thread.join(timeout=5)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
                self.last_error = None
            except Exception as error:  # noqa: BLE001 - keep scheduling even if one tick fails
                if str(error) != self.last_error:
                    self.engine.log("error", f"Contact planner tick failed: {error}")
                self.last_error = str(error)
            self._stop.wait(self.period)

    def _predict_loop(self):
        while not self._stop.is_set():
            self._wake.wait(5)
            self._wake.clear()
            try:
                self.predict_pending()
            except Exception as error:  # noqa: BLE001
                self.engine.log("error", f"Pass prediction failed: {error}")

    @property
    def background(self):
        return self._worker is not None and self._worker.is_alive()

    # ---- prediction --------------------------------------------------------

    def invalidate(self, satellite_ids=None, station_ids=None):
        """Queue contacts for prediction: these satellites (default all) at these stations (default all)."""
        with self.lock:
            for sat_id in self.satellites if satellite_ids is None else satellite_ids:
                if sat_id not in self.satellites:
                    continue
                if station_ids is None or self.pending.get(sat_id, ()) is None:
                    self.pending[sat_id] = None
                else:
                    self.pending[sat_id] = set(self.pending.get(sat_id) or ()) | set(station_ids)
        if self.background:
            self._wake.set()
        else:
            self.predict_pending()

    def recompute(self):
        """Re-predict everything. Runs in the background when the planner is started."""
        self.invalidate()

    def predict_pending(self):
        """Predict queued satellites one at a time, merging each as it finishes."""
        while True:
            with self.lock:
                if not self.pending:
                    if self.computed_at is None or self._batch_started is not None:
                        self.computed_at = self.clock()
                        self.compute_seconds = time.monotonic() - (self._batch_started or time.monotonic())
                        self._batch_started = None
                    return
                if self._batch_started is None:
                    self._batch_started = time.monotonic()
                sat_id, station_ids = next(iter(self.pending.items()))
                del self.pending[sat_id]
                self.in_flight = sat_id
                sat = copy.deepcopy(self.satellites.get(sat_id))
                stations = [
                    copy.deepcopy(s)
                    for s in self.stations.values()
                    if s.get("enabled", True) and (station_ids is None or s["id"] in station_ids)
                ]
                now = self.clock()
            if sat is None:
                continue
            try:
                contacts, problem = self._predict(sat, stations, now - self.lookback, now + self.horizon)
            finally:
                self.in_flight = None
            with self.lock:
                current = self.satellites.get(sat_id)
                if (
                    current is None
                    or current.get("tle") != sat.get("tle")
                    or current.get("enabled") != sat.get("enabled")
                ):
                    continue  # changed while predicting; it's queued again
                station_set = {s["id"] for s in stations} if station_ids is not None else None
                previous = {}
                for key in list(self.pair_contacts):
                    if key[0] == sat_id and (station_set is None or key[1] in station_set):
                        previous[key] = self.pair_contacts.pop(key)
                for station in stations:
                    live = self.stations.get(station["id"])
                    if live is None or not live.get("enabled", True):
                        continue
                    key = (sat_id, station["id"])
                    self.pair_contacts[key] = _keep_ids(contacts.get(station["id"], []), previous.get(key, []))
                if problem:
                    if problem.startswith("Prediction failed") and self.problems.get(sat_id) != problem:
                        self.alert(
                            "prediction_failed",
                            f"Pass prediction failed for '{sat['name']}'",
                            problem,
                            key=f"prediction:{sat_id}:{problem}",
                        )
                    self.problems[sat_id] = problem
                else:
                    self.problems.pop(sat_id, None)
                self._merge(now)

    _batch_started = None
    in_flight = None  # satellite being predicted right now (no longer in pending)

    def predicting(self):
        return len(self.pending) + (1 if self.in_flight and self.in_flight not in self.pending else 0)

    def _predict(self, sat, stations, start, end):
        """Returns ({station id: contacts}, problem or None). Runs without the lock."""
        if not sat.get("enabled", True):
            return {}, None
        if not sat.get("tle"):
            return {}, "No TLE yet"
        if SGP4_IMPORT_ERROR:
            return {}, SGP4_IMPORT_ERROR
        if not stations:
            return {}, None
        try:
            propagator = Propagator(sat["tle"]["line1"], sat["tle"]["line2"])
            sites = [Station(s["latitude"], s["longitude"], s["altitude"], s["min_elevation"]) for s in stations]
            found = find_passes_for_stations(propagator, sites, start, end)
        except Exception as error:  # noqa: BLE001 - one bad orbit mustn't hide the rest
            return {}, f"Prediction failed: {error}"
        return {
            station["id"]: [self._contact(sat, station, p) for p in passes]
            for station, passes in zip(stations, found, strict=True)
        }, None

    def _drop_pairs(self, satellite_id=None, station_id=None):
        for key in list(self.pair_contacts):
            if (satellite_id and key[0] == satellite_id) or (station_id and key[1] == station_id):
                del self.pair_contacts[key]

    def _merge(self, now):
        contacts = [c for pair in self.pair_contacts.values() for c in pair]
        contacts.sort(key=lambda c: c["aos"])
        self.contacts = contacts
        self.contacts_by_id = {c["id"]: c for c in contacts}
        self._retime(now)
        self._apply_rules(now)

    def _contact(self, sat, station, found):
        contact = dict(found)
        contact.update(
            id=f"{sat['id']}-{station['id']}-{int(round(found['tca']))}",
            satellite_id=sat["id"],
            satellite=sat["name"],
            target=sat.get("target", ""),
            station_id=station["id"],
            station=station["name"],
        )
        return contact

    # ---- satellites and stations -------------------------------------------

    def _save(self, kind, record):
        self.store.save_record(kind, record["id"], record)

    def _satellite_json(self, sat, now):
        result = copy.deepcopy(sat)
        epoch = (sat.get("tle") or {}).get("epoch")
        result["tle_age_days"] = (now - epoch) / 86400 if epoch else None
        result["tle_stale"] = bool(epoch) and result["tle_age_days"] > self.stale_tle_days
        result["problem"] = self.problems.get(sat["id"])
        result["predicting"] = sat["id"] in self.pending or sat["id"] == self.in_flight
        return result

    def save_satellite(self, payload, sat_id=None, user=None):
        if not isinstance(payload, dict):
            raise PlannerError("Satellite must be a JSON object")
        with self.lock:
            if sat_id is not None:
                sat = self.satellites.get(sat_id)
                if sat is None:
                    raise PlannerNotFoundError(f"Satellite '{sat_id}' not found")
                sat = copy.deepcopy(sat)
            else:
                sat = {
                    "id": _short_id(),
                    "created_at": self.clock(),
                    "created_by": user_name(user),
                    "tle": None,
                    "tle_history": [],
                }
            name = str(payload.get("name", sat.get("name")) or "").strip()
            if not name:
                raise PlannerError("Satellite needs a name")
            sat["name"] = name
            sat["target"] = str(payload.get("target", sat.get("target")) or "").strip().upper()
            sat["enabled"] = bool(payload.get("enabled", sat.get("enabled", True)))
            catalog = str(payload.get("catalog", sat.get("catalog")) or "").strip()
            sat["catalog"] = int(catalog) if catalog.isdigit() else (catalog or None)
            sat["preferred_source"] = (
                str(payload.get("preferred_source", sat.get("preferred_source")) or "").strip() or None
            )
            source = payload.get("tle_source", sat.get("tle_source")) or {"kind": "manual"}
            try:
                problems = get_source(source.get("kind")).check_config(source)
            except KeyError as error:
                raise PlannerError(str(error)) from None
            if problems:
                raise PlannerError(f"TLE source: {problems[0]}")
            sat["tle_source"] = source
            tle_text = payload.get("tle")
            if isinstance(tle_text, str) and tle_text.strip():
                self._apply_tle(sat, tle_text, payload.get("source") or source.get("kind", "manual"), user)
            self.satellites[sat["id"]] = sat
            self._save("satellites", sat)
            if not sat["enabled"]:
                self._drop_pairs(satellite_id=sat["id"])
                self._merge(self.clock())
        self.invalidate([sat["id"]])
        with self.lock:
            return self._satellite_json(sat, self.clock())

    def set_tle(self, sat_id, text, source="manual", user=None):
        """Apply a new TLE to a satellite and queue its contacts for prediction.

        Pasted ("manual") TLEs apply directly; any other source goes through the safeguards in tle_inputs
        and the result says what happened.
        """
        if source != "manual":
            if sat_id not in self.satellites:
                raise PlannerNotFoundError(f"Satellite '{sat_id}' not found")
            outcome = self.intake.ingest(text, source, user, satellite_id=sat_id)
            if not outcome["results"]:
                raise PlannerError("; ".join(outcome["problems"]) or "No TLE in the text")
            with self.lock:
                result = self._satellite_json(self.satellites[sat_id], self.clock())
            result["ingest"] = outcome["results"][0]
            return result
        with self.lock:
            sat = self.satellites.get(sat_id)
            if sat is None:
                raise PlannerNotFoundError(f"Satellite '{sat_id}' not found")
            self._apply_tle(sat, text, source, user)
            self._save("satellites", sat)
        self.invalidate([sat_id])
        with self.lock:
            return self._satellite_json(sat, self.clock())

    def _apply_tle(self, sat, text, source, user=None):
        try:
            tle = parse_tle(text)
        except OrbitError as error:
            raise PlannerError(str(error)) from None
        old = sat.get("tle")
        if old and old["line1"] == tle["line1"] and old["line2"] == tle["line2"]:
            return
        tle["source"] = source
        tle["applied_at"] = self.clock()
        tle["applied_by"] = user_name(user) if user is not None else source
        if not sat.get("catalog"):
            sat["catalog"] = tle["norad_id"]
        if old:
            sat["tle_history"] = ([old] + list(sat.get("tle_history") or []))[:TLE_HISTORY]
        sat["tle"] = tle
        self.engine.log("info", f"Satellite '{sat['name']}' TLE updated from {source} (catalog {tle['norad_id']})")

    def delete_satellite(self, sat_id):
        with self.lock:
            sat = self.satellites.pop(sat_id, None)
            if sat is None:
                raise PlannerNotFoundError(f"Satellite '{sat_id}' not found")
            self.store.delete_record("satellites", sat_id)
            self.pending.pop(sat_id, None)
            self.problems.pop(sat_id, None)
            self._drop_pairs(satellite_id=sat_id)
            self._merge(self.clock())
            return sat

    def save_station(self, payload, station_id=None, user=None):
        if not isinstance(payload, dict):
            raise PlannerError("Station must be a JSON object")
        with self.lock:
            if station_id is not None:
                station = self.stations.get(station_id)
                if station is None:
                    raise PlannerNotFoundError(f"Station '{station_id}' not found")
                station = copy.deepcopy(station)
            else:
                station = {"id": _short_id(), "created_at": self.clock(), "created_by": user_name(user)}
            merged = {**station, **payload}
            name = str(merged.get("name") or "").strip()
            if not name:
                raise PlannerError("Station needs a name")
            previous = {k: station.get(k) for k in ("latitude", "longitude", "altitude", "min_elevation", "enabled")}
            station.update(
                name=name,
                latitude=_number(merged.get("latitude"), "Latitude", -90, 90),
                longitude=_number(merged.get("longitude"), "Longitude", -180, 360),
                altitude=_number(merged.get("altitude") or 0, "Altitude", -500, 10000),
                min_elevation=_number(merged.get("min_elevation") or 0, "Minimum elevation", -5, 89),
                capacity=int(_number(merged.get("capacity") or 1, "Capacity", 1, 100)),
                enabled=bool(merged.get("enabled", True)),
            )
            if station["longitude"] > 180:
                station["longitude"] -= 360
            self.stations[station["id"]] = station
            self._save("stations", station)
            geometry_changed = previous != {k: station.get(k) for k in previous}
            if not station["enabled"]:
                self._drop_pairs(station_id=station["id"])
                self._merge(self.clock())
            elif not geometry_changed:
                # Only the name or capacity: relabel the contacts without predicting again
                for (_sat, st_id), pair in self.pair_contacts.items():
                    if st_id == station["id"]:
                        for contact in pair:
                            contact["station"] = name
                self._merge(self.clock())
        if station["enabled"] and geometry_changed:
            self.invalidate(station_ids=[station["id"]])
        return copy.deepcopy(station)

    def delete_station(self, station_id):
        with self.lock:
            station = self.stations.pop(station_id, None)
            if station is None:
                raise PlannerNotFoundError(f"Station '{station_id}' not found")
            self.store.delete_record("stations", station_id)
            self._drop_pairs(station_id=station_id)
            self._merge(self.clock())
            return station

    # ---- re-timing ---------------------------------------------------------

    def _nearest_contact(self, activity):
        old = activity["contact"]
        pair = self.pair_contacts.get((old["satellite_id"], old["station_id"]), [])
        candidates = [c for c in pair if not c["partial"]]
        return min(candidates, key=lambda c: abs(c["tca"] - old["tca"]), default=None)

    def _retime(self, now):
        for activity in self.activities.values():
            if self._is_tombstone(activity) and activity["contact"]["los"] >= now:
                # A rule activity an operator cancelled follows its pass too, so it keeps blocking that pass
                # however far TLE updates move it over the days
                nearest = self._nearest_contact(activity)
                close = (
                    nearest is not None and abs(nearest["tca"] - activity["contact"]["tca"]) <= self.retime_tolerance
                )
                if close and nearest["id"] != activity["contact"]["id"]:
                    activity["contact"] = self._contact_ref(nearest)
                    self._save("activities", activity)
                continue
            if activity["status"] not in WAITING:
                continue
            old = activity["contact"]
            if old["los"] < now:
                continue  # the scheduler marks it missed
            if (old["satellite_id"], old["station_id"]) not in self.pair_contacts and (
                old["satellite_id"] in self.pending
            ):
                continue  # not predicted yet
            nearest = self._nearest_contact(activity)
            shift = None if nearest is None else nearest["tca"] - old["tca"]
            if nearest is not None and abs(shift) <= self.retime_tolerance:
                moved = abs(shift) >= 0.5
                if moved or nearest["id"] != old["id"] or activity["status"] == "needs_review":
                    activity["contact"] = self._contact_ref(nearest)
                    activity["suggested_contact_id"] = None
                    if activity["status"] == "needs_review":
                        activity["status"] = "scheduled"
                        activity["message"] = "Contact predicted again"
                    if moved:
                        activity["drift"] = float(activity.get("drift") or 0) + shift
                        activity["message"] = f"Re-timed {shift:+.1f}s to follow the updated prediction"
                    self._save("activities", activity)
                continue
            if activity.get("rule_id"):
                # The rule schedules whatever pass is predicted now; this one is gone
                why = "contact no longer predicted" if nearest is None else f"contact moved {shift:+.0f}s"
                self._finish(activity, "cancelled", f"Cancelled: {why}; the rule schedules the new prediction", now)
                activity["superseded"] = True
                self._save("activities", activity)
                continue
            if activity["status"] == "needs_review" and activity.get("suggested_contact_id") == (
                nearest and nearest["id"]
            ):
                continue
            activity["status"] = "needs_review"
            activity["suggested_contact_id"] = nearest["id"] if nearest else None
            if nearest is None:
                activity["message"] = "This contact is no longer predicted; move or delete the activity"
            else:
                activity["message"] = (
                    f"The contact moved {shift:+.0f}s (more than {self.retime_tolerance:g}s); "
                    "accept the new time or move the activity"
                )
            self._save("activities", activity)
            self.audit(
                SCHEDULER, "flagged for review", "activity", activity["id"], activity["label"], activity["message"]
            )
            self.alert(
                "activity_needs_review",
                f"Activity '{activity['label']}' needs review",
                activity["message"],
                key=f"review:{activity['id']}:{activity.get('suggested_contact_id')}",
            )

    @staticmethod
    def _is_tombstone(activity):
        return activity["status"] == "cancelled" and activity.get("rule_id") and not activity.get("superseded")

    @staticmethod
    def _contact_ref(contact):
        keys = (
            "id",
            "aos",
            "tca",
            "los",
            "duration",
            "max_elevation",
            "satellite_id",
            "satellite",
            "target",
            "station_id",
            "station",
        )
        return {k: contact[k] for k in keys}

    # ---- station capacity --------------------------------------------------

    def _used_contacts(self, exclude_activity=None):
        """Contacts that have a scheduled or running activity: {contact id: contact ref}."""
        used = {}
        for activity in self.activities.values():
            if activity is exclude_activity or not (activity["status"] in WAITING or activity["status"] == "running"):
                continue
            used[activity["contact"]["id"]] = activity["contact"]
        return used

    def _capacity(self, station_id):
        return int((self.stations.get(station_id) or {}).get("capacity") or 1)

    def _station_conflicts(self, used):
        """Contacts in use that overlap more than their station's capacity: {contact id: [other contact ids]}."""
        by_station = {}
        for contact in used.values():
            by_station.setdefault(contact["station_id"], []).append(contact)
        conflicts = {}
        for station_id, contacts in by_station.items():
            capacity = self._capacity(station_id)
            contacts.sort(key=lambda c: c["aos"])
            for contact in contacts:
                others = [o for o in contacts if o is not contact and _overlap(o, contact)]
                if len(others) < capacity:
                    continue
                # Worst moment inside this contact: count the overlapping ones at each of their starts
                worst = 0
                for moment in [contact["aos"]] + [o["aos"] for o in others if o["aos"] > contact["aos"]]:
                    worst = max(worst, sum(1 for o in others if o["aos"] <= moment < o["los"]))
                if worst >= capacity:
                    conflicts[contact["id"]] = [o["id"] for o in others]
        return conflicts

    def _station_full(self, contact, used):
        """True if scheduling into contact would put its station over capacity."""
        if contact["id"] in used:
            return False  # already worked; another activity in the same pass doesn't need an antenna
        others = [o for o in used.values() if o["station_id"] == contact["station_id"] and _overlap(o, contact)]
        if len(others) < self._capacity(contact["station_id"]):
            return False
        moments = [contact["aos"]] + [o["aos"] for o in others if o["aos"] > contact["aos"]]
        worst = max(sum(1 for o in others if o["aos"] <= m < o["los"]) for m in moments)
        return worst >= self._capacity(contact["station_id"])

    def list_contacts(self, start=None, end=None, satellite_id=None, station_id=None):
        with self.lock:
            contacts = self.contacts
            if start is not None:
                contacts = [c for c in contacts if c["los"] >= start]
            if end is not None:
                contacts = [c for c in contacts if c["aos"] <= end]
            if satellite_id:
                contacts = [c for c in contacts if c["satellite_id"] == satellite_id]
            if station_id:
                contacts = [c for c in contacts if c["station_id"] == station_id]
            by_contact = {}
            for activity in self.activities.values():
                if activity.get("superseded"):
                    continue
                by_contact.setdefault(activity["contact"]["id"], []).append(activity["id"])
            used = self._used_contacts()
            conflicts = self._station_conflicts(used)
            return [
                {
                    **c,
                    "activities": by_contact.get(c["id"], []),
                    "conflicts": conflicts.get(c["id"], []),
                    "station_full": self._station_full(c, used),
                }
                for c in contacts
            ]

    # ---- activities --------------------------------------------------------

    def _template(self, template_id):
        try:
            return self.engine.get_flow(template_id)
        except Exception as error:  # noqa: BLE001 - NotFoundError from the engine
            raise PlannerNotFoundError(str(error)) from None

    @staticmethod
    def _check_template(template, params):
        from .graph import validate_flow  # local: graph imports nothing from here

        problems = validate_flow(template)
        if problems:
            raise PlannerError(f"The flow has problems: {problems[0]['message']}")
        if not any(n.get("type") == "trigger_manual" for n in template.get("nodes") or []):
            raise PlannerError("The flow needs a Manual trigger to be scheduled")
        _variables, param_problems = fill_parameters(template.get("parameters"), params)
        if param_problems:
            raise PlannerError("Parameters: " + "; ".join(param_problems))

    def _check_timing(self, record):
        from .graph import MODES

        if record["anchor"] not in ANCHORS:
            raise PlannerError(f"Start must be relative to one of {', '.join(a.upper() for a in ANCHORS)}")
        if record["mode"] not in MODES:
            raise PlannerError(f"Mode must be one of {', '.join(MODES)}")

    def _check_activity(self, activity, template, now, require_future=True):
        self._check_timing(activity)
        start = activity_start(activity)
        if start >= activity["contact"]["los"]:
            raise PlannerError("The activity would start after LOS")
        if require_future and start <= now:
            raise PlannerError("That start time has already passed")
        self._check_template(template, activity["params"])

    def _new_activity(self, template, now, user):
        return {
            "id": _short_id(),
            "created_at": now,
            "created_by": user_name(user),
            "template_id": template["id"],
            "template_name": template.get("name"),
            "template_updated_at": template.get("updated_at"),
            "flow": template,
            "status": "scheduled",
            "message": "",
            "run_id": None,
            "runs": [],
            "drift": 0.0,
            "suggested_contact_id": None,
            "rule_id": None,
        }

    def create_activity(self, payload, user=None):
        if not isinstance(payload, dict):
            raise PlannerError("Activity must be a JSON object")
        with self.lock:
            contact = self.contacts_by_id.get(payload.get("contact_id"))
            if contact is None:
                raise PlannerNotFoundError(f"Contact '{payload.get('contact_id')}' is not predicted")
            template = self._template(payload.get("template_id"))
            now = self.clock()
            activity = self._new_activity(template, now, user)
            self._apply_fields(activity, payload, contact, template)
            self._check_activity(activity, template, now)
            self.activities[activity["id"]] = activity
            self._save("activities", activity)
        self.engine.log("info", f"Activity '{activity['label']}' scheduled for {self._when(activity)}")
        return self.activity_json(activity, now)

    def _apply_fields(self, record, payload, contact, template):
        if contact is not None:
            record["contact"] = self._contact_ref(contact)
        record["label"] = str(payload.get("label", record.get("label")) or template.get("name") or "Activity")
        record["anchor"] = str(payload.get("anchor", record.get("anchor")) or "aos").lower()
        record["offset"] = _number(payload.get("offset", record.get("offset", 0)) or 0, "Offset", -86400, 86400)
        record["mode"] = payload.get("mode", record.get("mode")) or template.get("mode", "run")
        record["params"] = dict(payload.get("params", record.get("params")) or {})
        for key, default in DEFAULTS.items():
            value = payload.get(key, record.get(key, default))
            record[key] = bool(value) if isinstance(default, bool) else _number(value, key, 0, 86400)
        duration = payload.get("duration", record.get("duration"))
        record["duration"] = None if duration in (None, "") else _number(duration, "Duration", 1, 86400)

    def update_activity(self, activity_id, payload, user=None):
        with self.lock:
            activity = self._activity(activity_id)
            if activity["status"] not in WAITING:
                raise PlannerError(f"A {activity['status']} activity can't be changed")
            updated = copy.deepcopy(activity)
            contact_id = payload.get("contact_id", activity["contact"]["id"])
            contact = self.contacts_by_id.get(contact_id)
            if contact is None:
                raise PlannerNotFoundError(f"Contact '{contact_id}' is not predicted")
            template = updated["flow"]
            self._apply_fields(updated, payload, contact, template)
            self._check_activity(updated, template, self.clock())
            updated["status"] = "scheduled"
            updated["suggested_contact_id"] = None
            updated["updated_by"] = user_name(user)
            if activity["status"] == "needs_review":
                updated["message"] = "Moved by operator"
            self.activities[activity_id] = updated
            self._save("activities", updated)
            return self.activity_json(updated, self.clock())

    def accept_suggestion(self, activity_id, user=None):
        with self.lock:
            activity = self._activity(activity_id)
            if activity["status"] != "needs_review" or not activity.get("suggested_contact_id"):
                raise PlannerError("This activity has no new contact time to accept")
            return self.update_activity(activity_id, {"contact_id": activity["suggested_contact_id"]}, user)

    def refresh_template(self, activity_id, user=None):
        """Re-copy the activity's flow from its template's current version."""
        with self.lock:
            activity = self._activity(activity_id)
            if activity["status"] not in WAITING:
                raise PlannerError(f"A {activity['status']} activity can't be changed")
            template = self._template(activity["template_id"])
            updated = copy.deepcopy(activity)
            updated.update(
                flow=template,
                template_name=template.get("name"),
                template_updated_at=template.get("updated_at"),
                updated_by=user_name(user),
            )
            self._check_activity(updated, template, self.clock(), require_future=False)
            self.activities[activity_id] = updated
            self._save("activities", updated)
            return self.activity_json(updated, self.clock())

    def delete_activity(self, activity_id, user=None):
        """Remove an activity, stopping its run if it's running.

        An activity a rule made is cancelled instead, so the rule doesn't schedule it again;
        it's deleted with the other finished activities after the retention period.
        """
        with self.lock:
            activity = self._activity(activity_id)
            run_id = activity.get("run_id") if activity["status"] == "running" else None
            if activity.get("rule_id") and activity["status"] not in FINISHED:
                self._finish(activity, "cancelled", f"Cancelled by {user_name(user)}", self.clock())
            else:
                del self.activities[activity_id]
                self.store.delete_record("activities", activity_id)
        if run_id:
            with contextlib.suppress(Exception):  # it may have just finished
                self.engine.stop_run(run_id)
        return activity

    def _activity(self, activity_id):
        activity = self.activities.get(activity_id)
        if activity is None:
            raise PlannerNotFoundError(f"Activity '{activity_id}' not found")
        return activity

    def activity_json(self, activity, now=None):
        now = self.clock() if now is None else now
        result = {k: copy.deepcopy(v) for k, v in activity.items() if k != "flow"}
        result["start"] = activity_start(activity)
        result["end"] = (
            min(result["start"] + activity["duration"], activity["contact"]["los"])
            if activity.get("duration")
            else activity["contact"]["los"]
        )
        result["parameters"] = copy.deepcopy(activity["flow"].get("parameters") or [])
        current = self.engine.flows.get(activity["template_id"])
        result["template_missing"] = current is None
        result["template_version"] = activity["flow"].get("version")
        result["template_current_version"] = current.get("version") if current else None
        result["template_changed"] = current is not None and current.get("updated_at") != activity.get(
            "template_updated_at"
        )
        rule = self.rules.get(activity.get("rule_id")) if activity.get("rule_id") else None
        result["rule_name"] = rule["name"] if rule else None
        result["overlaps"] = []
        result["conflicts"] = []
        return result

    def list_activities(self, include_superseded=False):
        with self.lock:
            now = self.clock()
            items = [
                self.activity_json(a, now)
                for a in self.activities.values()
                if include_superseded or not a.get("superseded")
            ]
            conflicts = self._station_conflicts(self._used_contacts())
        items.sort(key=lambda a: a["start"])
        # Activities sharing a contact whose windows overlap (allowed, but worth a look)
        by_contact = {}
        for item in items:
            if item["status"] in ("cancelled", "missed"):
                continue
            by_contact.setdefault(item["contact"]["id"], []).append(item)
            item["conflicts"] = conflicts.get(item["contact"]["id"], []) if item["status"] != "done" else []
        for group in by_contact.values():
            for i, a in enumerate(group):
                for b in group[i + 1 :]:
                    if a["start"] < b["end"] and b["start"] < a["end"]:
                        a["overlaps"].append(b["id"])
                        b["overlaps"].append(a["id"])
        return items

    # ---- rules ---------------------------------------------------------------

    def save_rule(self, payload, rule_id=None, user=None):
        if not isinstance(payload, dict):
            raise PlannerError("Rule must be a JSON object")
        with self.lock:
            if rule_id is not None:
                rule = copy.deepcopy(self.rules.get(rule_id))
                if rule is None:
                    raise PlannerNotFoundError(f"Rule '{rule_id}' not found")
            else:
                rule = {"id": _short_id(), "created_at": self.clock(), "created_by": user_name(user)}
            template = self._template(payload.get("template_id", rule.get("template_id")))
            rule["template_id"] = template["id"]
            rule["name"] = str(payload.get("name", rule.get("name")) or template.get("name") or "Rule").strip()
            rule["enabled"] = bool(payload.get("enabled", rule.get("enabled", True)))
            for key in ("satellite_ids", "station_ids"):
                value = payload.get(key, rule.get(key)) or []
                if not isinstance(value, list):
                    raise PlannerError(f"{key} must be a list (empty for all)")
                rule[key] = [str(v) for v in value]
            unknown = [s for s in rule["satellite_ids"] if s not in self.satellites]
            unknown += [s for s in rule["station_ids"] if s not in self.stations]
            if unknown:
                raise PlannerNotFoundError(f"Unknown satellite or station: {', '.join(unknown)}")
            rule["min_elevation"] = _number(
                payload.get("min_elevation", rule.get("min_elevation", 0)) or 0, "Minimum elevation", 0, 90
            )
            rule["min_duration"] = _number(
                payload.get("min_duration", rule.get("min_duration", 0)) or 0, "Minimum duration", 0, 86400
            )
            rule["priority"] = int(_number(payload.get("priority", rule.get("priority", 0)) or 0, "Priority"))
            rule["skip_full_stations"] = bool(payload.get("skip_full_stations", rule.get("skip_full_stations", True)))
            self._apply_fields(rule, payload, None, template)
            rule.pop("label", None)  # its activities are labelled with the rule's name
            self._check_timing(rule)
            self._check_template(template, rule["params"])
            rule["updated_at"] = self.clock()
            rule["updated_by"] = user_name(user)
            rule["last_error"] = None
            self.rules[rule["id"]] = rule
            self._save("rules", rule)
            # Re-plan: the rule's future activities follow its new settings
            removed = self._remove_rule_activities(rule["id"])
            created = self._apply_rules(self.clock(), only=rule["id"])
            return {"rule": copy.deepcopy(rule), "removed": removed, "created": created}

    def delete_rule(self, rule_id):
        with self.lock:
            rule = self.rules.pop(rule_id, None)
            if rule is None:
                raise PlannerNotFoundError(f"Rule '{rule_id}' not found")
            self.store.delete_record("rules", rule_id)
            removed = self._remove_rule_activities(rule_id, keep_cancelled=False)
            return rule, removed

    def _remove_rule_activities(self, rule_id, keep_cancelled=True):
        """Delete a rule's activities that haven't started. Cancelled ones stay (unless asked) to block re-creation."""
        removed = 0
        for activity in list(self.activities.values()):
            if activity.get("rule_id") != rule_id:
                continue
            if activity["status"] in WAITING or (not keep_cancelled and activity["status"] == "cancelled"):
                del self.activities[activity["id"]]
                self.store.delete_record("activities", activity["id"])
                removed += 1
        return removed

    def _rule_matches(self, rule, contact, now):
        if contact["partial"]:
            return False
        if rule["satellite_ids"] and contact["satellite_id"] not in rule["satellite_ids"]:
            return False
        if rule["station_ids"] and contact["station_id"] not in rule["station_ids"]:
            return False
        if contact["max_elevation"] < rule["min_elevation"] or contact["duration"] < rule["min_duration"]:
            return False
        start = contact[rule["anchor"]] + rule["offset"]
        return now + RULE_LEAD_SECONDS < start < contact["los"]

    def _apply_rules(self, now, only=None):
        """Create activities for every enabled rule's matching contacts that don't have one yet."""
        created = 0
        rules = sorted(
            (r for r in self.rules.values() if r.get("enabled", True) and (only is None or r["id"] == only)),
            key=lambda r: -int(r.get("priority") or 0),
        )
        if not rules:
            return 0
        # Passes each rule already has an activity for, including ones an operator cancelled. Matched by
        # satellite, station and TCA within the re-time tolerance, not by contact id: a routine TLE update
        # moves TCA a little, which changes the id, and a cancelled pass must not come back because of it.
        taken = {}
        for activity in self.activities.values():
            if activity.get("rule_id") and not activity.get("superseded"):
                ref = activity["contact"]
                taken.setdefault(activity["rule_id"], []).append((ref["satellite_id"], ref["station_id"], ref["tca"]))

        def already_taken(rule_id, contact):
            return any(
                sat == contact["satellite_id"]
                and station == contact["station_id"]
                and abs(tca - contact["tca"]) <= self.retime_tolerance
                for sat, station, tca in taken.get(rule_id, ())
            )

        used = self._used_contacts()
        for rule in rules:
            template = self.engine.flows.get(rule["template_id"])
            if template is None:
                rule["last_error"] = "Its template was deleted"
                continue
            try:
                self._check_template(template, rule["params"])
            except PlannerError as error:
                rule["last_error"] = str(error)
                continue
            rule["last_error"] = None
            skipped_full = 0
            for contact in self.contacts:
                if already_taken(rule["id"], contact) or not self._rule_matches(rule, contact, now):
                    continue
                if rule.get("skip_full_stations", True) and self._station_full(contact, used):
                    skipped_full += 1
                    continue
                activity = self._new_activity(copy.deepcopy(template), now, f"rule:{rule['name']}")
                self._apply_fields(activity, rule, contact, template)
                activity["label"] = rule.get("label") or rule["name"]
                activity["rule_id"] = rule["id"]
                activity["message"] = f"Scheduled by rule '{rule['name']}'"
                self.activities[activity["id"]] = activity
                self._save("activities", activity)
                taken.setdefault(rule["id"], []).append(
                    (contact["satellite_id"], contact["station_id"], contact["tca"])
                )
                used[contact["id"]] = activity["contact"]
                created += 1
            rule["skipped_full"] = skipped_full
        if created:
            self.audit(SCHEDULER, "scheduled from rules", "activity", detail=f"{created} new activities")
        return created

    def rule_json(self, rule):
        result = copy.deepcopy(rule)
        mine = [a for a in self.activities.values() if a.get("rule_id") == rule["id"]]
        result["scheduled"] = sum(1 for a in mine if a["status"] in WAITING)
        result["template_name"] = (self.engine.flows.get(rule["template_id"]) or {}).get("name")
        return result

    def list_rules(self):
        with self.lock:
            return sorted((self.rule_json(r) for r in self.rules.values()), key=lambda r: -int(r.get("priority") or 0))

    # ---- scheduling --------------------------------------------------------

    def _when(self, activity):
        offset = float(activity.get("offset") or 0)
        sign = "+" if offset >= 0 else "-"
        return (
            f"{activity['anchor'].upper()} {sign} {abs(offset):g}s of {activity['contact']['satellite']} "
            f"over {activity['contact']['station']}"
        )

    def set_paused(self, paused, reason=""):
        with self.lock:
            self.settings["paused"] = bool(paused)
            self.settings["paused_reason"] = reason if paused else ""
            self.store.save_record("planner_settings", "settings", self.settings)
        self.engine.log("warn" if paused else "info", f"Contact scheduler {'paused' if paused else 'resumed'}")
        self.alert(
            "scheduler_paused",
            f"Contact scheduler {'paused' if paused else 'resumed'}",
            reason if paused else "",
            key=f"paused:{paused}:{self.clock()}",
        )
        return dict(self.settings)

    def tick(self):
        now = self.clock()
        if self.computed_at is not None and now - self.computed_at >= self.recompute_every and not self.predicting():
            self.recompute()
        elif not self.background and self.pending:
            self.predict_pending()
        self._poll_sources(now)
        try:
            self.intake.poll(now)
        except Exception as error:  # noqa: BLE001 - a bad input mustn't stop scheduling
            self.engine.log("error", f"TLE inputs: {error}")
        if now - self.last_pruned >= 300:
            self.prune(now)
            self._check_stale_tles(now)
        if now - self.last_rules >= 60:
            self.last_rules = now
            with self.lock:
                self._apply_rules(now)
        to_start = []
        to_stop = []
        with self.lock:
            for activity in self.activities.values():
                status = activity["status"]
                contact = activity["contact"]
                if status in WAITING:
                    start = activity_start(activity)
                    if now < start:
                        continue
                    if status == "needs_review":
                        self._finish(
                            activity, "missed", f"Not run: still needed review at start. {activity['message']}", now
                        )
                        continue
                    late = now - start
                    if self.settings.get("paused"):
                        if late > activity["late_tolerance"] or now >= contact["los"]:
                            self._finish(activity, "missed", "Scheduler was paused at the start time", now)
                        continue
                    if late > activity["late_tolerance"] or now >= contact["los"]:
                        self._finish(
                            activity,
                            "missed",
                            f"Start time passed {late:.0f}s ago (late tolerance {activity['late_tolerance']:g}s)",
                            now,
                        )
                        continue
                    to_start.append(activity)
                elif status == "running" and activity.get("stop_at_los") and now >= contact["los"]:
                    to_stop.append(activity)
        for activity in to_start:
            self._launch(activity, now)
        for activity in to_stop:
            run = self.engine.runs.get(activity.get("run_id"))
            if run is not None and not run.cancelled():
                run.message = "Stopped at LOS"
                run.stop()

    def _check_stale_tles(self, now):
        """Alert once per TLE when it goes stale."""
        with self.lock:
            stale = [
                s
                for s in self.satellites.values()
                if s.get("enabled", True) and s.get("tle") and (now - s["tle"]["epoch"]) / 86400 > self.stale_tle_days
            ]
        for sat in stale:
            age = (now - sat["tle"]["epoch"]) / 86400
            self.alert(
                "tle_stale",
                f"TLE for '{sat['name']}' is {age:.1f} days old",
                "Its predicted passes are getting less accurate; paste a newer TLE",
                key=f"tle_stale:{sat['id']}:{sat['tle']['epoch']}",
            )

    def prune(self, now=None):
        """Delete finished activities whose contact ended more than the retention period ago."""
        now = self.clock() if now is None else now
        self.last_pruned = now
        removed = 0
        with self.lock:
            for activity in list(self.activities.values()):
                if activity["status"] not in FINISHED:
                    continue
                ended = max(float(activity.get("ended_at") or 0), activity["contact"]["los"])
                if now - ended > self.retention:
                    del self.activities[activity["id"]]
                    self.store.delete_record("activities", activity["id"])
                    removed += 1
        if removed:
            self.engine.log("info", f"Deleted {removed} finished activities older than the retention period")
        return removed

    def _poll_sources(self, now):
        for sat in list(self.satellites.values()):
            config = sat.get("tle_source") or {}
            try:
                source = get_source(config.get("kind"))
            except KeyError:
                continue
            if not source.poll_period or now - self.last_polled.get(sat["id"], 0) < source.poll_period:
                continue
            self.last_polled[sat["id"]] = now
            try:
                text = source.fetch(sat, config, self.engine.backend)
                if text:
                    self.set_tle(sat["id"], text, source.kind)
                    self.audit(source.kind, "updated TLE", "satellite", sat["id"], sat["name"])
            except Exception as error:  # noqa: BLE001
                self.problems[sat["id"]] = f"TLE source {source.kind}: {error}"

    def run_variables(self, activity, now):
        variables, _problems = fill_parameters(activity["flow"].get("parameters"), activity["params"])
        variables.update(self._contact_variables(activity["contact"]))
        variables.update(activity_id=activity["id"], activity_label=activity["label"])
        return variables

    @staticmethod
    def _contact_variables(contact):
        return {
            "contact_id": contact["id"],
            "contact_satellite": contact["satellite"],
            "contact_target": contact.get("target", ""),
            "contact_station": contact["station"],
            "contact_aos": contact["aos"],
            "contact_tca": contact["tca"],
            "contact_los": contact["los"],
            "contact_duration": contact["duration"],
            "contact_max_elevation": contact["max_elevation"],
        }

    def contact_variables(self, spec):
        """Contact variables for a manual run: a predicted contact ({"contact_id"}) or a simulated one.

        A simulated contact starts ``aos_in`` seconds from now (default 60) and lasts ``duration`` (300).
        """
        if not isinstance(spec, dict):
            raise PlannerError("contact must be an object")
        if spec.get("contact_id"):
            with self.lock:
                contact = self.contacts_by_id.get(spec["contact_id"])
            if contact is None:
                raise PlannerNotFoundError(f"Contact '{spec['contact_id']}' is not predicted")
            return self._contact_variables(contact)
        now = self.clock()
        aos = now + _number(spec.get("aos_in", 60), "AOS in", -86400, 86400)
        duration = _number(spec.get("duration", 300), "Duration", 1, 86400)
        return self._contact_variables(
            {
                "id": f"simulated-{int(aos)}",
                "satellite": str(spec.get("satellite") or "SIMULATED"),
                "target": str(spec.get("target") or ""),
                "station": str(spec.get("station") or "SIMULATED"),
                "aos": aos,
                "tca": aos + duration / 2,
                "los": aos + duration,
                "duration": duration,
                "max_elevation": _number(spec.get("max_elevation", 45), "Max elevation", 0, 90),
            }
        )

    def rehearse(self, activity_id, user=None, skip_waits=True):
        """Dry run an activity's flow with its parameters and real contact, without touching the activity."""
        with self.lock:
            activity = self._activity(activity_id)
            flow = copy.deepcopy(activity["flow"])
            variables = self.run_variables(activity, self.clock())
            info = self._trigger_info(activity) | {"kind": "rehearsal", "user": user_name(user)}
        return self.engine.start_scheduled_run(
            flow,
            f"Rehearsal of '{activity['label']}' by {user_name(user)}",
            variables,
            activity["mode"],
            trigger_info=info,
            dry_run=True,
            skip_waits=bool(skip_waits),
        )

    def _trigger_info(self, activity):
        contact = activity["contact"]
        rule = self.rules.get(activity.get("rule_id")) if activity.get("rule_id") else None
        return {
            "kind": "activity",
            "activity_id": activity["id"],
            "activity_label": activity["label"],
            "scheduled_by": activity.get("created_by"),
            "rule_id": activity.get("rule_id"),
            "rule_name": rule["name"] if rule else None,
            "contact_id": contact["id"],
            "satellite": contact["satellite"],
            "station": contact["station"],
            "aos": contact["aos"],
            "los": contact["los"],
            "scheduled_start": activity_start(activity),
            "template_version": activity["flow"].get("version"),
            "detail": f"Scheduled: {self._when(activity)}",
        }

    def _launch(self, activity, now):
        contact = activity["contact"]
        # Held while starting so a run that ends at once can't report back before it's recorded.
        # Lock order is always planner then engine; the engine never calls in holding its own lock.
        with self.lock:
            if activity["status"] != "scheduled" or self.activities.get(activity["id"]) is not activity:
                return  # changed or deleted since this tick looked at it
            if self.settings.get("paused"):
                return  # paused (e.g. STOP ALL) since this tick looked; the next tick marks it missed if too late
            try:
                run = self.engine.start_scheduled_run(
                    activity["flow"],
                    f"Scheduled: {self._when(activity)}",
                    self.run_variables(activity, now),
                    activity["mode"],
                    approval_deadline=contact["los"] - activity["approval_margin"],
                    activity_id=activity["id"],
                    trigger_info=self._trigger_info(activity)
                    | {"started_late": max(0.0, now - activity_start(activity))},
                )
            except Exception as error:  # noqa: BLE001
                self._finish(activity, "failed", f"Could not start: {error}", now)
                return
            activity["status"] = "running"
            activity["run_id"] = run.id
            activity["started_at"] = now
            late = now - activity_start(activity)
            activity["started_late"] = late if late >= 1 else 0.0
            activity["message"] = f"Started {late:.1f}s late" if late >= 1 else "Started"
            activity["runs"] = (list(activity.get("runs") or []) + [run.id])[-10:]
            self._save("activities", activity)
        self.audit(SCHEDULER, "started", "activity", activity["id"], activity["label"], f"run {run.id}")
        self.alert(
            "activity_started",
            f"Activity '{activity['label']}' started",
            self._when(activity),
            key=f"activity_started:{activity['id']}:{run.id}",
            path=f"/tools/flowautomator?flow={activity['template_id']}&run={run.id}",
        )

    def on_run_finished(self, run):
        """Called by the engine when a run that belongs to an activity ends."""
        with self.lock:
            activity = self.activities.get(getattr(run, "activity_id", None))
            if activity is None or activity.get("run_id") != run.id:
                return
            status = {"success": "done", "failed": "failed", "stopped": "stopped"}.get(run.status, "failed")
            message = run.message or {"done": "Completed", "stopped": "Stopped"}.get(status, status)
            if activity.get("started_late"):
                message += f" (started {activity['started_late']:.0f}s late)"
            self._finish(activity, status, message, self.clock())

    def _finish(self, activity, status, message, now):
        activity["status"] = status
        activity["message"] = message or status
        activity["ended_at"] = now
        self._save("activities", activity)
        if status in ("missed", "failed", "done", "stopped"):
            self.audit(SCHEDULER, status, "activity", activity.get("id"), activity.get("label"), activity["message"])
        event = {"missed": "activity_missed", "failed": "activity_failed", "done": "activity_done"}.get(status)
        if event:
            path = "/tools/flowautomator?view=contacts"
            if activity.get("run_id") and status != "missed":
                path = f"/tools/flowautomator?flow={activity.get('template_id')}&run={activity['run_id']}"
            self.alert(
                event,
                f"Activity '{activity.get('label')}' {status}",
                f"{activity['message']} ({activity['contact']['satellite']} over {activity['contact']['station']})",
                key=f"{event}:{activity.get('id')}",
                path=path,
            )

    # ---- overview ----------------------------------------------------------

    def status(self):
        with self.lock:
            now = self.clock()
            upcoming = [a for a in self.activities.values() if a["status"] in WAITING]
            next_activity = min(upcoming, key=activity_start, default=None)
            next_contact = next((c for c in self.contacts if c["los"] >= now), None)
            conflicts = self._station_conflicts(self._used_contacts())
            return {
                "satellites": len(self.satellites),
                "stations": len(self.stations),
                "contacts": len(self.contacts),
                "rules": len(self.rules),
                "scheduled": len(upcoming),
                "needs_review": sum(1 for a in upcoming if a["status"] == "needs_review"),
                "running": sum(1 for a in self.activities.values() if a["status"] == "running"),
                "conflicts": len(conflicts),
                "next_activity": self.activity_json(next_activity, now) if next_activity else None,
                "next_contact": next_contact,
                "paused": self.settings.get("paused", False),
                "paused_reason": self.settings.get("paused_reason", ""),
                "predicting": self.predicting(),
                "computed_at": self.computed_at,
                "compute_seconds": self.compute_seconds,
                "retention_days": self.retention / 86400,
                "sgp4_error": SGP4_IMPORT_ERROR,
                "error": self.last_error,
            }

    def schedule(self, start=None, end=None):
        """Everything the Contacts view shows, in one response."""
        now = self.clock()
        with self.lock:
            return {
                "time": now,
                "window": {"start": now - self.lookback, "end": now + self.horizon},
                "satellites": [self._satellite_json(s, now) for s in self.satellites.values()],
                "stations": [copy.deepcopy(s) for s in self.stations.values()],
                "contacts": self.list_contacts(start, end),
                "activities": self.list_activities(),
                "rules": self.list_rules(),
                "status": self.status(),
            }


SAME_CONTACT_SECONDS = 5.0


def _keep_ids(contacts, previous):
    """Give each newly predicted contact the id of the previous prediction of the same pass.

    Ids end in the rounded TCA, and TCA moves by milliseconds between predictions (and by more after a
    TLE update), so without this a pass could change id while activities, alerts and links refer to it.
    """
    if not previous:
        return contacts
    for contact in contacts:
        match = min(previous, key=lambda old: abs(old["tca"] - contact["tca"]))
        if abs(match["tca"] - contact["tca"]) <= SAME_CONTACT_SECONDS:
            contact["id"] = match["id"]
    return contacts
