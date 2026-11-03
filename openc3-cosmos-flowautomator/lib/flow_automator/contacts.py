"""Contact planning: satellites, ground stations, predicted passes and scheduled activities.

* Satellites carry a TLE (see ``tle_sources``); stations a location and an
  elevation mask. Contacts (passes) are predicted for every enabled
  satellite x station pair from ``lookback`` ago to ``horizon`` ahead, kept in
  memory and recomputed when a TLE or station changes and every few hours.
* An activity is a snapshot of a flow (usually a template), its parameter
  values, a contact and a start time relative to it: ``AOS - 120`` or
  ``LOS - 90``. The planner starts it as a run at that time with the contact's
  details as run variables (``contact_aos``, ``contact_los`` ...).
* When contacts are recomputed each scheduled activity follows its pass: the
  predicted pass of the same satellite and station whose TCA is closest, if it
  moved less than ``retime_tolerance`` seconds. Otherwise it needs review and
  won't run until someone moves it.
* An activity that couldn't start on time (engine down, scheduler paused)
  starts late if it's within its ``late_tolerance``, else it's marked missed.
"""

import contextlib
import copy
import threading
import time
import uuid

from .orbit import SGP4_IMPORT_ERROR, OrbitError, Propagator, Station, find_passes, parse_tle
from .templates import fill_parameters
from .tle_sources import get_source


ANCHORS = ("aos", "tca", "los")
WAITING = ("scheduled", "needs_review")
FINISHED = ("done", "failed", "stopped", "missed", "cancelled")
TLE_HISTORY = 5
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
        self.period = period
        self.lock = threading.RLock()
        self.satellites = {}
        self.stations = {}
        self.activities = {}
        self.settings = {"paused": False, "paused_reason": ""}
        self.contacts = []
        self.contacts_by_id = {}
        self.computed_at = None
        self.compute_seconds = None
        self.problems = {}  # satellite id -> why it has no contacts
        self.last_polled = {}
        self._stop = threading.Event()
        self._thread = None
        self.last_error = None

    # ---- lifecycle ---------------------------------------------------------

    def load(self):
        with self.lock:
            self.satellites = self.store.records("satellites")
            self.stations = self.store.records("stations")
            self.activities = self.store.records("activities")
            self.settings.update(self.store.records("planner_settings").get("settings", {}))
            now = self.clock()
            for activity in self.activities.values():
                if activity["status"] == "running":
                    # The engine stopped in the middle of the run, which went with it
                    self._finish(activity, "failed", "The flow engine restarted during this activity's run", now)
            self.recompute()
        self.engine.log(
            "info",
            f"Contact planner loaded {len(self.satellites)} satellites, {len(self.stations)} stations, "
            f"{len([a for a in self.activities.values() if a['status'] in WAITING])} scheduled activities",
        )

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="flow-contacts", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

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

    # ---- satellites and stations -------------------------------------------

    def _save(self, kind, record):
        self.store.save_record(kind, record["id"], record)

    def _satellite_json(self, sat, now):
        result = copy.deepcopy(sat)
        epoch = (sat.get("tle") or {}).get("epoch")
        result["tle_age_days"] = (now - epoch) / 86400 if epoch else None
        result["tle_stale"] = bool(epoch) and result["tle_age_days"] > self.stale_tle_days
        result["problem"] = self.problems.get(sat["id"])
        return result

    def save_satellite(self, payload, sat_id=None):
        if not isinstance(payload, dict):
            raise PlannerError("Satellite must be a JSON object")
        with self.lock:
            if sat_id is not None:
                sat = self.satellites.get(sat_id)
                if sat is None:
                    raise PlannerNotFoundError(f"Satellite '{sat_id}' not found")
                sat = copy.deepcopy(sat)
            else:
                sat = {"id": _short_id(), "created_at": self.clock(), "tle": None, "tle_history": []}
            name = str(payload.get("name", sat.get("name")) or "").strip()
            if not name:
                raise PlannerError("Satellite needs a name")
            sat["name"] = name
            sat["target"] = str(payload.get("target", sat.get("target")) or "").strip().upper()
            sat["enabled"] = bool(payload.get("enabled", sat.get("enabled", True)))
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
                self._apply_tle(sat, tle_text, payload.get("source") or source.get("kind", "manual"))
            self.satellites[sat["id"]] = sat
            self._save("satellites", sat)
            self.recompute()
            return self._satellite_json(sat, self.clock())

    def set_tle(self, sat_id, text, source="manual"):
        """Apply a new TLE to a satellite (from any TLE source) and recompute its contacts."""
        with self.lock:
            sat = self.satellites.get(sat_id)
            if sat is None:
                raise PlannerNotFoundError(f"Satellite '{sat_id}' not found")
            self._apply_tle(sat, text, source)
            self._save("satellites", sat)
            self.recompute()
            return self._satellite_json(sat, self.clock())

    def _apply_tle(self, sat, text, source):
        try:
            tle = parse_tle(text)
        except OrbitError as error:
            raise PlannerError(str(error)) from None
        old = sat.get("tle")
        if old and old["line1"] == tle["line1"] and old["line2"] == tle["line2"]:
            return
        tle["source"] = source
        tle["applied_at"] = self.clock()
        if old:
            sat["tle_history"] = ([old] + list(sat.get("tle_history") or []))[:TLE_HISTORY]
        sat["tle"] = tle
        self.engine.log("info", f"Satellite '{sat['name']}' TLE updated from {source} (catalog {tle['norad_id']})")

    def delete_satellite(self, sat_id):
        with self.lock:
            if self.satellites.pop(sat_id, None) is None:
                raise PlannerNotFoundError(f"Satellite '{sat_id}' not found")
            self.store.delete_record("satellites", sat_id)
            self.recompute()

    def save_station(self, payload, station_id=None):
        if not isinstance(payload, dict):
            raise PlannerError("Station must be a JSON object")
        with self.lock:
            if station_id is not None:
                station = self.stations.get(station_id)
                if station is None:
                    raise PlannerNotFoundError(f"Station '{station_id}' not found")
                station = copy.deepcopy(station)
            else:
                station = {"id": _short_id(), "created_at": self.clock()}
            merged = {**station, **payload}
            name = str(merged.get("name") or "").strip()
            if not name:
                raise PlannerError("Station needs a name")
            station.update(
                name=name,
                latitude=_number(merged.get("latitude"), "Latitude", -90, 90),
                longitude=_number(merged.get("longitude"), "Longitude", -180, 360),
                altitude=_number(merged.get("altitude") or 0, "Altitude", -500, 10000),
                min_elevation=_number(merged.get("min_elevation") or 0, "Minimum elevation", -5, 89),
                enabled=bool(merged.get("enabled", True)),
            )
            if station["longitude"] > 180:
                station["longitude"] -= 360
            self.stations[station["id"]] = station
            self._save("stations", station)
            self.recompute()
            return copy.deepcopy(station)

    def delete_station(self, station_id):
        with self.lock:
            if self.stations.pop(station_id, None) is None:
                raise PlannerNotFoundError(f"Station '{station_id}' not found")
            self.store.delete_record("stations", station_id)
            self.recompute()

    # ---- contacts ----------------------------------------------------------

    def recompute(self):
        """Predict every contact in the window, then re-time scheduled activities to follow them."""
        with self.lock:
            started = time.monotonic()
            now = self.clock()
            start, end = now - self.lookback, now + self.horizon
            contacts = []
            problems = {}
            stations = [s for s in self.stations.values() if s.get("enabled", True)]
            for sat in self.satellites.values():
                if not sat.get("enabled", True):
                    continue
                if not sat.get("tle"):
                    problems[sat["id"]] = "No TLE yet"
                    continue
                if SGP4_IMPORT_ERROR:
                    problems[sat["id"]] = SGP4_IMPORT_ERROR
                    continue
                try:
                    propagator = Propagator(sat["tle"]["line1"], sat["tle"]["line2"])
                    for station in stations:
                        site = Station(
                            station["latitude"], station["longitude"], station["altitude"], station["min_elevation"]
                        )
                        for found in find_passes(propagator, site, start, end):
                            contacts.append(self._contact(sat, station, found))
                except Exception as error:  # noqa: BLE001 - one bad orbit mustn't hide the rest
                    problems[sat["id"]] = f"Prediction failed: {error}"
            contacts.sort(key=lambda c: c["aos"])
            self.contacts = contacts
            self.contacts_by_id = {c["id"]: c for c in contacts}
            self.problems = problems
            self.computed_at = now
            self.compute_seconds = time.monotonic() - started
            self._retime(now)

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

    def _nearest_contact(self, activity):
        old = activity["contact"]
        candidates = [
            c
            for c in self.contacts
            if c["satellite_id"] == old["satellite_id"] and c["station_id"] == old["station_id"] and not c["partial"]
        ]
        return min(candidates, key=lambda c: abs(c["tca"] - old["tca"]), default=None)

    def _retime(self, now):
        for activity in self.activities.values():
            if activity["status"] not in WAITING:
                continue
            old = activity["contact"]
            if old["los"] < now:
                continue  # the scheduler marks it missed
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
            self.engine.log("warn", f"Activity '{activity['label']}' needs review: {activity['message']}")

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
                by_contact.setdefault(activity["contact"]["id"], []).append(activity["id"])
            return [{**c, "activities": by_contact.get(c["id"], [])} for c in contacts]

    # ---- activities --------------------------------------------------------

    def _template(self, template_id):
        try:
            return self.engine.get_flow(template_id)
        except Exception as error:  # noqa: BLE001 - NotFoundError from the engine
            raise PlannerNotFoundError(str(error)) from None

    def _check_activity(self, activity, template, now, require_future=True):
        contact = activity["contact"]
        if activity["anchor"] not in ANCHORS:
            raise PlannerError(f"Start must be relative to one of {', '.join(a.upper() for a in ANCHORS)}")
        start = activity_start(activity)
        if start >= contact["los"]:
            raise PlannerError("The activity would start after LOS")
        if require_future and start <= now:
            raise PlannerError("That start time has already passed")
        from .graph import MODES, validate_flow  # local: graph imports nothing from here

        if activity["mode"] not in MODES:
            raise PlannerError(f"Mode must be one of {', '.join(MODES)}")
        problems = validate_flow(template)
        if problems:
            raise PlannerError(f"The flow has problems: {problems[0]['message']}")
        if not any(n.get("type") == "trigger_manual" for n in template.get("nodes") or []):
            raise PlannerError("The flow needs a Manual trigger to be scheduled")
        _variables, param_problems = fill_parameters(template.get("parameters"), activity["params"])
        if param_problems:
            raise PlannerError("Parameters: " + "; ".join(param_problems))

    def create_activity(self, payload):
        if not isinstance(payload, dict):
            raise PlannerError("Activity must be a JSON object")
        with self.lock:
            contact = self.contacts_by_id.get(payload.get("contact_id"))
            if contact is None:
                raise PlannerNotFoundError(f"Contact '{payload.get('contact_id')}' is not predicted")
            template = self._template(payload.get("template_id"))
            now = self.clock()
            activity = {
                "id": _short_id(),
                "created_at": now,
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
            }
            self._apply_fields(activity, payload, contact, template)
            self._check_activity(activity, template, now)
            self.activities[activity["id"]] = activity
            self._save("activities", activity)
        self.engine.log("info", f"Activity '{activity['label']}' scheduled for {self._when(activity)}")
        return self.activity_json(activity, now)

    def _apply_fields(self, activity, payload, contact, template):
        activity["contact"] = self._contact_ref(contact)
        activity["label"] = str(payload.get("label", activity.get("label")) or template.get("name") or "Activity")
        activity["anchor"] = str(payload.get("anchor", activity.get("anchor")) or "aos").lower()
        activity["offset"] = _number(payload.get("offset", activity.get("offset", 0)) or 0, "Offset", -86400, 86400)
        activity["mode"] = payload.get("mode", activity.get("mode")) or template.get("mode", "run")
        activity["params"] = dict(payload.get("params", activity.get("params")) or {})
        for key, default in DEFAULTS.items():
            value = payload.get(key, activity.get(key, default))
            activity[key] = bool(value) if isinstance(default, bool) else _number(value, key, 0, 86400)
        duration = payload.get("duration", activity.get("duration"))
        activity["duration"] = None if duration in (None, "") else _number(duration, "Duration", 1, 86400)

    def update_activity(self, activity_id, payload):
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
            if activity["status"] == "needs_review":
                updated["message"] = "Moved by operator"
            self.activities[activity_id] = updated
            self._save("activities", updated)
            return self.activity_json(updated, self.clock())

    def accept_suggestion(self, activity_id):
        with self.lock:
            activity = self._activity(activity_id)
            if activity["status"] != "needs_review" or not activity.get("suggested_contact_id"):
                raise PlannerError("This activity has no new contact time to accept")
            return self.update_activity(activity_id, {"contact_id": activity["suggested_contact_id"]})

    def refresh_template(self, activity_id):
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
            )
            self._check_activity(updated, template, self.clock(), require_future=False)
            self.activities[activity_id] = updated
            self._save("activities", updated)
            return self.activity_json(updated, self.clock())

    def delete_activity(self, activity_id):
        """Remove an activity. A running one has its run stopped first."""
        with self.lock:
            activity = self._activity(activity_id)
            run_id = activity.get("run_id") if activity["status"] == "running" else None
            del self.activities[activity_id]
            self.store.delete_record("activities", activity_id)
        if run_id:
            with contextlib.suppress(Exception):  # it may have just finished
                self.engine.stop_run(run_id)

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
        result["template_changed"] = current is not None and current.get("updated_at") != activity.get(
            "template_updated_at"
        )
        result["overlaps"] = []
        return result

    def list_activities(self):
        with self.lock:
            now = self.clock()
            items = [self.activity_json(a, now) for a in self.activities.values()]
        items.sort(key=lambda a: a["start"])
        # Warn about activities sharing a contact whose windows overlap (allowed, but worth a look)
        by_contact = {}
        for item in items:
            if item["status"] in ("cancelled", "missed"):
                continue
            by_contact.setdefault(item["contact"]["id"], []).append(item)
        for group in by_contact.values():
            for i, a in enumerate(group):
                for b in group[i + 1 :]:
                    if a["start"] < b["end"] and b["start"] < a["end"]:
                        a["overlaps"].append(b["id"])
                        b["overlaps"].append(a["id"])
        return items

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
        return dict(self.settings)

    def tick(self):
        now = self.clock()
        if self.computed_at is None or now - self.computed_at >= self.recompute_every:
            self.recompute()
        self._poll_sources(now)
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
            except Exception as error:  # noqa: BLE001
                self.problems[sat["id"]] = f"TLE source {source.kind}: {error}"

    def run_variables(self, activity, now):
        contact = activity["contact"]
        variables, _problems = fill_parameters(activity["flow"].get("parameters"), activity["params"])
        variables.update(
            contact_id=contact["id"],
            contact_satellite=contact["satellite"],
            contact_target=contact.get("target", ""),
            contact_station=contact["station"],
            contact_aos=contact["aos"],
            contact_tca=contact["tca"],
            contact_los=contact["los"],
            contact_duration=contact["duration"],
            contact_max_elevation=contact["max_elevation"],
            activity_id=activity["id"],
            activity_label=activity["label"],
        )
        return variables

    def _launch(self, activity, now):
        contact = activity["contact"]
        # Held while starting so a run that ends at once can't report back before it's recorded.
        # Lock order is always planner then engine; the engine never calls in holding its own lock.
        with self.lock:
            if activity["status"] != "scheduled" or self.activities.get(activity["id"]) is not activity:
                return  # changed or deleted since this tick looked at it
            try:
                run = self.engine.start_scheduled_run(
                    activity["flow"],
                    f"Scheduled: {self._when(activity)}",
                    self.run_variables(activity, now),
                    activity["mode"],
                    approval_deadline=contact["los"] - activity["approval_margin"],
                    activity_id=activity["id"],
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
        self.engine.log("info", f"Activity '{activity['label']}' started run {run.id}")

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
        if status in ("missed", "failed"):
            self.engine.log("warn", f"Activity '{activity.get('label')}' {status}: {activity['message']}")

    # ---- overview ----------------------------------------------------------

    def status(self):
        with self.lock:
            now = self.clock()
            upcoming = [a for a in self.activities.values() if a["status"] in WAITING]
            next_activity = min(upcoming, key=activity_start, default=None)
            next_contact = next((c for c in self.contacts if c["los"] >= now), None)
            return {
                "satellites": len(self.satellites),
                "stations": len(self.stations),
                "contacts": len(self.contacts),
                "scheduled": len(upcoming),
                "needs_review": sum(1 for a in upcoming if a["status"] == "needs_review"),
                "running": sum(1 for a in self.activities.values() if a["status"] == "running"),
                "next_activity": self.activity_json(next_activity, now) if next_activity else None,
                "next_contact": next_contact,
                "paused": self.settings.get("paused", False),
                "paused_reason": self.settings.get("paused_reason", ""),
                "computed_at": self.computed_at,
                "compute_seconds": self.compute_seconds,
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
                "status": self.status(),
            }
