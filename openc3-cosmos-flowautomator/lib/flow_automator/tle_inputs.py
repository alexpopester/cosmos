"""Hooks for other apps to provide TLEs, and the safeguards every automated TLE goes through.

Ways in (besides pasting in the tool):

* ``POST /api/tles`` with any number of TLEs as text (2 or 3 lines each).
* A COSMOS stash key (default ``flow_automator_tles``): a script or
  microservice calls ``stash_set('flow_automator_tles', text)`` (or a dict
  ``{"tle": text, "source": "my-pipeline"}``, or a list of those). The engine
  takes it within ``poll_seconds`` and leaves the outcome in
  ``flow_automator_tles_result``.
* Watched files in the COSMOS bucket, as target paths like
  ``INST/tle/latest.tle`` (what a script's ``put_target_file`` writes, or
  Bucket Explorer under ``targets_modified``). Read again whenever they change.
* Telemetry: packets whose items carry TLE text (one item with all the lines,
  or ``line1``/``line2`` items). Read whenever the packet's count goes up.

TLEs are matched to satellites by catalog number, then by name. Automated
TLEs (anything but pasting in the tool) must pass:

1. the catalog number matches the satellite's,
2. the epoch is newer than the current TLE's,
3. the satellite's preferred source, if set, wins while its TLE is fresh,
4. the new TLE doesn't move the satellite more than ``max_jump_km`` from
   where the current one puts it now: otherwise it's held for review
   (Accept/Reject in the tool) instead of re-timing everything.
"""

import contextlib
import copy
import hashlib
import math

from .orbit import OrbitError, Propagator, parse_tle


DEFAULT_SETTINGS = {
    "stash_key": "flow_automator_tles",
    "files": [],  # target paths, e.g. "INST/tle/latest.tle"
    "packets": [],  # {"target", "packet", "item"} or {"target", "packet", "line1", "line2", "name"?}
    "max_jump_km": 100.0,
    "poll_seconds": 30.0,
}
RESULTS = ("applied", "unchanged", "skipped", "held", "rejected", "unknown")


class TleInputError(Exception):
    status = 400


def split_tles(text):
    """Split text holding any number of TLEs into [{"name", "text"}], plus [problems] for junk.

    Accepts 2-line sets and 3-line sets (a name line before, with or without "0 ").
    """
    lines = [line.rstrip() for line in str(text or "").replace("\r", "").split("\n")]
    entries, problems = [], []
    name = ""
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if not line:
            index += 1
            continue
        if line.startswith("1 ") and index + 1 < len(lines) and lines[index + 1].strip().startswith("2 "):
            text_lines = [line, lines[index + 1].strip()]
            entries.append({"name": name, "text": "\n".join(([name] if name else []) + text_lines)})
            name = ""
            index += 2
            continue
        if line.startswith(("1 ", "2 ")):
            problems.append(f"Line {index + 1}: a TLE line without its pair")
            name = ""
        else:
            if name:
                problems.append(f"Line {index}: '{name}' isn't followed by a TLE")
            name = line[2:].strip() if line.startswith("0 ") else line
        index += 1
    if name:
        problems.append(f"'{name}' isn't followed by a TLE")
    return entries, problems


def _distance_now_km(old, new, now):
    """How far apart two TLEs put the satellite now."""
    a = Propagator(old["line1"], old["line2"]).ecef([now])[0][0]
    b = Propagator(new["line1"], new["line2"]).ecef([now])[0][0]
    return math.dist(a, b)


class TleIntake:
    """Matches incoming TLEs to satellites, applies the safeguards and polls the stash, files and packets."""

    def __init__(self, planner):
        self.planner = planner
        self.settings = copy.deepcopy(DEFAULT_SETTINGS)
        self.status = {"stash": None, "files": {}, "packets": {}}
        self.file_hashes = {}
        self.packet_counts = {}
        self.last_poll = 0.0

    # ---- settings ------------------------------------------------------------

    def load(self):
        saved = self.planner.store.records("planner_settings").get("tle_inputs")
        if saved:
            self.settings = {**copy.deepcopy(DEFAULT_SETTINGS), **saved}

    def settings_json(self):
        return {**copy.deepcopy(self.settings), "status": copy.deepcopy(self.status)}

    def save_settings(self, payload):
        if not isinstance(payload, dict):
            raise TleInputError("TLE input settings must be a JSON object")
        settings = copy.deepcopy(self.settings)
        if "stash_key" in payload:
            settings["stash_key"] = str(payload["stash_key"] or "").strip()
        if "files" in payload:
            files = payload["files"] or []
            if not isinstance(files, list) or any(".." in str(f) or not str(f).strip() for f in files):
                raise TleInputError("files must be a list of target paths like INST/tle/latest.tle")
            settings["files"] = [str(f).strip().lstrip("/") for f in files]
        if "packets" in payload:
            packets = payload["packets"] or []
            if not isinstance(packets, list):
                raise TleInputError("packets must be a list")
            cleaned = []
            for packet in packets:
                entry = {
                    k: str(packet.get(k) or "").strip().upper()
                    for k in ("target", "packet", "item", "line1", "line2", "name")
                }
                if (
                    not entry["target"]
                    or not entry["packet"]
                    or not (entry["item"] or (entry["line1"] and entry["line2"]))
                ):
                    raise TleInputError("Each packet needs a target, a packet, and an item (or line1 and line2 items)")
                cleaned.append({k: v for k, v in entry.items() if v})
            settings["packets"] = cleaned
        for key, low, high in (("max_jump_km", 1, 100000), ("poll_seconds", 5, 3600)):
            if key in payload:
                try:
                    value = float(payload[key])
                except (TypeError, ValueError):
                    raise TleInputError(f"{key} must be a number") from None
                if not low <= value <= high:
                    raise TleInputError(f"{key} must be between {low} and {high}")
                settings[key] = value
        self.settings = settings
        self.planner.store.save_record("planner_settings", "tle_inputs", settings)
        return self.settings_json()

    # ---- matching and safeguards ------------------------------------------------

    def _match(self, tle, name):
        sats = list(self.planner.satellites.values())
        catalog = tle["norad_id"]
        for sat in sats:
            if str(sat.get("catalog") or (sat.get("tle") or {}).get("norad_id") or "") == str(catalog):
                return sat
        wanted = (name or tle.get("name") or "").strip().lower()
        if wanted:
            for sat in sats:
                if sat["name"].strip().lower() == wanted:
                    return sat
        return None

    def ingest(self, text, source, user=None, manual=False, now=None, satellite_id=None):
        """Apply TLE text for any number of satellites. Returns {"results": [...], "problems": [...], "counts"}.

        With satellite_id, every TLE is for that satellite (its catalog number still has to match).
        """
        planner = self.planner
        now = planner.clock() if now is None else now
        source = str(source or "api").strip() or "api"
        entries, problems = split_tles(text)
        results = []
        changed = []
        with planner.lock:
            for entry in entries:
                result = {"name": entry["name"], "satellite": None, "result": "rejected", "reason": ""}
                try:
                    tle = parse_tle(entry["text"])
                except OrbitError as error:
                    result["reason"] = str(error)
                    results.append(result)
                    continue
                result.update(catalog=tle["norad_id"], epoch=tle["epoch"])
                sat = planner.satellites.get(satellite_id) if satellite_id else self._match(tle, entry["name"])
                if sat is None:
                    result.update(result="unknown", reason=f"No satellite with catalog {tle['norad_id']} or that name")
                    results.append(result)
                    continue
                result["satellite"] = sat["name"]
                outcome, reason = self._decide(sat, tle, source, manual, now)
                result.update(result=outcome, reason=reason)
                if outcome == "applied":
                    planner._apply_tle(sat, entry["text"], source, user)
                    sat.setdefault("catalog", tle["norad_id"])
                    sat.pop("held_tle", None)
                    planner._save("satellites", sat)
                    changed.append(sat["id"])
                elif outcome == "held":
                    sat["held_tle"] = {
                        **tle,
                        "text": entry["text"],
                        "source": source,
                        "reason": reason,
                        "held_at": now,
                        "held_from": str(user or source),
                    }
                    planner._save("satellites", sat)
                results.append(result)
        if changed:
            planner.invalidate(changed)
        for result in results:
            if result["result"] == "held":
                planner.alert(
                    "tle_held",
                    f"TLE for '{result['satellite']}' held for review",
                    result["reason"],
                    key=f"tle_held:{result['satellite']}:{result['epoch']}",
                )
        counts = {k: sum(1 for r in results if r["result"] == k) for k in RESULTS}
        if entries:
            planner.audit(
                user or source,
                "ingested TLEs",
                "satellite",
                detail=f"from {source}: " + ", ".join(f"{v} {k}" for k, v in counts.items() if v),
            )
        return {"results": results, "problems": problems, "counts": counts}

    def _decide(self, sat, tle, source, manual, now):
        current = sat.get("tle")
        catalog = sat.get("catalog") or (current or {}).get("norad_id")
        if catalog and str(catalog) != str(tle["norad_id"]):
            return "rejected", f"Catalog {tle['norad_id']} doesn't match the satellite's {catalog}"
        if current and current["line1"] == tle["line1"] and current["line2"] == tle["line2"]:
            return "unchanged", "Same TLE as the current one"
        if manual:
            return "applied", "Pasted"
        if current and tle["epoch"] <= current["epoch"]:
            return "skipped", "Not newer than the current TLE"
        preferred = sat.get("preferred_source")
        if preferred and source != preferred and current and current.get("source") == preferred:
            age_days = (now - current["epoch"]) / 86400
            if age_days <= self.planner.stale_tle_days:
                return "skipped", f"The preferred source ({preferred}) has a fresh TLE ({age_days:.1f} days old)"
        if current:
            try:
                jump = _distance_now_km(current, tle, now)
            except Exception as error:  # noqa: BLE001 - can't compare: let an operator look
                return "held", f"Couldn't compare with the current TLE: {error}"
            if jump > float(self.settings["max_jump_km"]):
                return "held", (
                    f"It moves the satellite {jump:.0f} km from where the current TLE has it "
                    f"(more than {self.settings['max_jump_km']:g} km): a maneuver, or a wrong TLE?"
                )
        return "applied", ""

    def accept_held(self, sat_id, user=None):
        planner = self.planner
        with planner.lock:
            sat = planner.satellites.get(sat_id)
            if sat is None or not sat.get("held_tle"):
                raise TleInputError("This satellite has no held TLE")
            held = sat.pop("held_tle")
            planner._apply_tle(sat, held["text"], held["source"], user)
            planner._save("satellites", sat)
        planner.invalidate([sat_id])
        return held

    def reject_held(self, sat_id):
        planner = self.planner
        with planner.lock:
            sat = planner.satellites.get(sat_id)
            if sat is None or not sat.get("held_tle"):
                raise TleInputError("This satellite has no held TLE")
            held = sat.pop("held_tle")
            planner._save("satellites", sat)
        return held

    # ---- polling ---------------------------------------------------------------

    def poll(self, now):
        if now - self.last_poll < float(self.settings["poll_seconds"]):
            return
        self.last_poll = now
        backend = self.planner.engine.backend
        self._poll_stash(backend, now)
        for path in self.settings["files"]:
            self._poll_file(backend, path, now)
        for packet in self.settings["packets"]:
            self._poll_packet(backend, packet, now)

    def _record(self, kind, key, now, outcome=None, error=None):
        entry = {"time": now, "error": error}
        if outcome is not None:
            entry.update(counts=outcome["counts"], results=outcome["results"][:50], problems=outcome["problems"][:20])
        if kind == "stash":
            self.status["stash"] = entry
        else:
            self.status[kind][key] = entry

    def _poll_stash(self, backend, now):
        key = self.settings.get("stash_key")
        if not key:
            return
        try:
            value = backend.stash_take(key)
        except Exception as error:  # noqa: BLE001
            self._record("stash", key, now, error=str(error))
            return
        if value is None:
            return
        items = value if isinstance(value, list) else [value]
        merged = {"results": [], "problems": [], "counts": dict.fromkeys(RESULTS, 0)}
        for item in items:
            if isinstance(item, dict):
                text, source = item.get("tle") or item.get("text") or "", item.get("source") or "stash"
            else:
                text, source = str(item), "stash"
            outcome = self.ingest(text, source, user=f"stash:{key}", now=now)
            merged["results"] += outcome["results"]
            merged["problems"] += outcome["problems"]
            for k, v in outcome["counts"].items():
                merged["counts"][k] += v
        self._record("stash", key, now, merged)
        with contextlib.suppress(Exception):  # the result is a courtesy for the script that sent it
            backend.stash_set(f"{key}_result", {"time": now, **merged})

    def _poll_file(self, backend, path, now):
        try:
            text = backend.read_target_file(path)
        except Exception as error:  # noqa: BLE001
            self._record("files", path, now, error=f"Can't read: {error}")
            return
        if text is None:
            self._record("files", path, now, error="No such file yet")
            return
        digest = hashlib.sha256(text.encode()).hexdigest()
        if self.file_hashes.get(path) == digest:
            return
        self.file_hashes[path] = digest
        self._record("files", path, now, self.ingest(text, f"file:{path}", user=f"file:{path}", now=now))

    def _poll_packet(self, backend, packet, now):
        name = f"{packet['target']} {packet['packet']}"
        try:
            count = backend.tlm_counts([(packet["target"], packet["packet"])])[0]
            if self.packet_counts.get(name) == count:
                return
            self.packet_counts[name] = count
            if packet.get("item"):
                text = backend.tlm(f"{name} {packet['item']}", "CONVERTED")
            else:
                lines = [backend.tlm(f"{name} {packet[k]}", "CONVERTED") for k in ("line1", "line2")]
                label = backend.tlm(f"{name} {packet['name']}", "CONVERTED") if packet.get("name") else ""
                text = "\n".join(str(v) for v in ([label] if label else []) + lines)
        except Exception as error:  # noqa: BLE001
            self._record("packets", name, now, error=str(error))
            return
        if isinstance(text, (bytes, bytearray)):
            text = text.decode(errors="replace")
        self._record("packets", name, now, self.ingest(str(text), f"tlm:{name}", user=f"tlm:{name}", now=now))
