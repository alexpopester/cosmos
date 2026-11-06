"""Record the real-pass soak as a log of changes (docs/REAL-DATA-TESTING.md step 2).

Every few minutes it reads the Flow Automator's schedule and appends what
changed for the soak's satellites and stations to
realdata/cache/soak/events-YYYYMMDD.jsonl:

- tle: a satellite got a new TLE (epoch, lines, source, when we saw it)
- contact_new / contact_moved / contact_gone: a predicted pass appeared, its
  AOS/LOS moved by at least MOVE_S, or it vanished before it happened
- activity: a rule activity changed status, pass or drift
- status: engine/planner status, once an hour
- error: the API couldn't be read (COSMOS down, token gone)

The planner prunes finished activities after a week and keeps no history of
predictions, so this log is the only full record. The report
(soak_report.py) reads it.

    python realdata/soak_recorder.py                  # forever, every 300 s
    python realdata/soak_recorder.py --once           # one pass (for testing)
"""

import argparse
import json
import os
import sys
import time


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import soak_common as common  # noqa: E402
from cfs_setup import outcome as cfs_outcome  # noqa: E402


STATE = os.path.join(common.SOAK_DIR, "recorder_state.json")
MOVE_S = 0.5
# A re-predicted pass with a new id is the same pass if its TCA is this close (passes of one satellite over
# one station are at least an orbit, ~90 minutes, apart)
SAME_PASS_S = 900
STATUS_EVERY_S = 3600
GAP_AFTER_S = 900  # this long between reads is a gap (the laptop slept, COSMOS or the recorder was down)
CONTACT_KEYS = ("aos", "tca", "los", "max_elevation", "partial")
ACTIVITY_KEYS = ("status", "message", "drift", "run_id", "started_at", "ended_at", "superseded", "anchor", "offset")


def _contact(c):
    return {
        "id": c["id"],
        "satellite_id": c["satellite_id"],
        "station_id": c["station_id"],
        **{k: c.get(k) for k in CONTACT_KEYS},
    }


def _activity(a):
    contact = a.get("contact") or {}
    return {
        "id": a["id"],
        "rule_id": a.get("rule_id"),
        "contact_id": contact.get("id"),
        "satellite_id": contact.get("satellite_id"),
        "station_id": contact.get("station_id"),
        "aos": contact.get("aos"),
        "tca": contact.get("tca"),
        "los": contact.get("los"),
        "max_elevation": contact.get("max_elevation"),
        **{k: a.get(k) for k in ACTIVITY_KEYS},
    }


def watched_rules(manifest):
    """The soak rule, the step 3b post-pass rules and the step 4 cFS pass ops rule."""
    rules = {manifest.get("rule_id")} | {v["rule_id"] for v in (manifest.get("realtlm") or {}).values()}
    return rules | ({manifest["cfs"]["rule_id"]} if manifest.get("cfs") else set())


def check_outcome(run):
    """What a post-pass check found: healthy, anomaly, not_heard, or the run's status if it got nowhere."""
    reached = {step.get("node") for step in run.get("steps") or []}
    endings = (("bad", "anomaly"), ("good", "healthy"), ("silent", "not_heard"), ("silent_alert", "not_heard"))
    outcome = next((name for node, name in endings if node in reached), run.get("status") or "unknown")
    summary = next((s.get("message") for s in run.get("steps") or [] if s.get("node") == "log"), None)
    return outcome, summary


def diff(schedule, state, manifest, now):
    """Events for what changed since ``state``, and the new state. Pure: no I/O."""
    sats, stations = set(manifest["satellites"]), set(manifest["stations"])
    events = []
    previous_tles = state.get("tles", {})
    tles = {}
    for sat in schedule["satellites"]:
        if sat["id"] not in sats:
            continue
        tle = sat.get("tle") or {}
        key = f"{tle.get('line1')}|{tle.get('line2')}"
        tles[sat["id"]] = key
        if tle.get("line1") and previous_tles.get(sat["id"]) != key:
            events.append(
                {
                    "type": "tle",
                    "satellite_id": sat["id"],
                    "norad": manifest["satellites"][sat["id"]]["norad"],
                    "epoch": tle.get("epoch"),
                    "line1": tle.get("line1"),
                    "line2": tle.get("line2"),
                    "source": tle.get("source"),
                    "received": tle.get("received_at") or tle.get("applied_at") or now,
                }
            )

    previous_contacts = state.get("contacts", {})
    contacts = {}
    for c in schedule["contacts"]:
        if c["satellite_id"] in sats and c["station_id"] in stations:
            contacts[c["id"]] = _contact(c)
    # The planner keeps a pass's id only while its TCA moves less than a few seconds; a bigger move (a TLE
    # update) gives it a new id. Pair those up again: same satellite and station, nearest TCA.
    new_ids = [cid for cid in contacts if cid not in previous_contacts]
    old_ids = {cid for cid in previous_contacts if cid not in contacts}
    renamed = {}  # new id -> old id
    for cid in sorted(new_ids, key=lambda i: contacts[i]["tca"]):
        c = contacts[cid]
        candidates = [
            o
            for o in old_ids
            if previous_contacts[o]["satellite_id"] == c["satellite_id"]
            and previous_contacts[o]["station_id"] == c["station_id"]
            and abs(previous_contacts[o]["tca"] - c["tca"]) <= SAME_PASS_S
        ]
        if candidates:
            match = min(candidates, key=lambda o: abs(previous_contacts[o]["tca"] - c["tca"]))
            renamed[cid] = match
            old_ids.discard(match)

    for cid, c in contacts.items():
        old = previous_contacts.get(cid) or previous_contacts.get(renamed.get(cid))
        if old is None:
            events.append({"type": "contact_new", **c})
        elif (
            cid in renamed
            or any(abs((c[k] or 0) - (old[k] or 0)) >= MOVE_S for k in ("aos", "los"))
            or c["partial"] != old["partial"]
        ):
            event = {
                "type": "contact_moved",
                **c,
                "aos_shift": c["aos"] - old["aos"],
                "los_shift": c["los"] - old["los"],
            }
            if cid in renamed:
                event["previous_id"] = renamed[cid]
            events.append(event)
    window_start = schedule.get("window", {}).get("start", now)
    for cid in old_ids:
        old = previous_contacts[cid]
        # Gone before its LOS (not just aged out of the lookback window): the prediction dropped it
        if old["los"] > window_start and old["los"] > now:
            events.append({"type": "contact_gone", **old})

    previous_activities = state.get("activities", {})
    activities = {}
    for a in schedule["activities"]:
        if a.get("rule_id") in watched_rules(manifest):
            activities[a["id"]] = _activity(a)
    for aid, a in activities.items():
        old = previous_activities.get(aid)
        if old != a:
            events.append(
                {
                    "type": "activity",
                    **a,
                    "previous_status": (old or {}).get("status"),
                    "previous_drift": (old or {}).get("drift"),
                }
            )

    new_state = {
        "tles": tles,
        "contacts": contacts,
        # Keep finished activities we've seen even after the planner prunes them
        "activities": {**previous_activities, **activities},
        "last_status": state.get("last_status", 0),
        "last_run": state.get("last_run"),
    }
    if now - new_state["last_status"] >= STATUS_EVERY_S:
        events.append({"type": "status", **(schedule.get("status") or {})})
        new_state["last_status"] = now
    return events, new_state


def append_events(events, now):
    if not events:
        return
    path = os.path.join(common.SOAK_DIR, f"events-{time.strftime('%Y%m%d', time.gmtime(now))}.jsonl")
    with open(path, "a") as file:
        for event in events:
            file.write(json.dumps({"time": now, **event}) + "\n")


def gap_event(last_run, now, gap_after=GAP_AFTER_S):
    """A gap in the recording, or None."""
    if last_run and now - last_run > gap_after:
        return {"type": "gap", "start": last_run, "end": now}
    return None


def record_once(fa, manifest, gap_after=GAP_AFTER_S):
    now = time.time()
    state = common.load_json(STATE, {})
    gap = gap_event(state.get("last_run"), now, gap_after)
    if gap:  # the laptop slept, or the recorder was stopped: whatever ran then wasn't watched
        append_events([gap], now)
    state["last_run"] = now
    common.save_json(STATE, state)
    try:
        schedule = fa.get("/schedule")
    except Exception as error:  # COSMOS down or token gone: note it and keep going
        append_events([{"type": "error", "message": f"{type(error).__name__}: {error}"[:500]}], now)
        return 0
    events, new_state = diff(schedule, state, manifest, now)
    realtlm = {v["rule_id"] for v in (manifest.get("realtlm") or {}).values()}
    for event in events:
        finished = event["type"] == "activity" and event["status"] in ("done", "failed", "stopped")
        if finished and event.get("rule_id") in realtlm and event.get("run_id"):
            try:
                run = fa.get(f"/runs/{event['run_id']}")
                event["outcome"], event["summary"] = check_outcome(run.get("run", run))
            except Exception as error:  # noqa: BLE001 - the outcome is extra; the event still counts
                event["outcome"] = f"unknown ({type(error).__name__})"
        cfs_rule = (manifest.get("cfs") or {}).get("rule_id")
        if finished and cfs_rule and event.get("rule_id") == cfs_rule and event.get("run_id"):
            try:
                run = fa.get(f"/runs/{event['run_id']}")
                result, los = cfs_outcome(run.get("run", run))
                event["outcome"], event["los"] = result, los
            except Exception as error:  # noqa: BLE001 - the outcome is extra; the event still counts
                event["outcome"] = f"unknown ({type(error).__name__})"
    append_events(events, now)
    common.save_json(STATE, new_state)
    return len(events)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--every", type=float, default=300.0, help="seconds between reads (default 300)")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--url", default=os.environ.get("COSMOS_URL", "http://localhost:2900"))
    parser.add_argument("--scope", default="DEFAULT")
    args = parser.parse_args(argv)

    manifest = common.load_json(common.MANIFEST)
    if not manifest:
        raise SystemExit(f"No soak manifest at {common.MANIFEST}: run soak_setup.py first")
    fa = common.FlowAutomator(args.url, args.scope)
    while True:
        manifest = common.load_json(common.MANIFEST) or manifest  # picks up rules added since (step 3b)
        count = record_once(fa, manifest)
        print(f"{common.now_iso()} recorded {count} events", flush=True)
        if args.once:
            return 0
        time.sleep(args.every)


if __name__ == "__main__":
    sys.exit(main())
