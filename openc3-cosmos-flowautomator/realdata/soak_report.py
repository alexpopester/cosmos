"""Score the real-pass soak (docs/REAL-DATA-TESTING.md step 2).

Reads the recorder's event log and the manifest, fetches what SatNOGS
stations actually heard from the soak's satellites since the soak started,
and reports:

1. Operational accuracy: for every SatNOGS observation of a soak satellite
   (any station), our prediction from the TLE the Flow Automator actually held
   at the time, against the frames heard and the SatNOGS window, by TLE age.
2. Coverage at the soak stations: of the passes SatNOGS heard at the stations
   we imported, how many we had predicted and scheduled, and what happened.
3. Scheduler behaviour: activity outcomes, how late runs started, retimes
   (drift), cancellations, misses and failures.
4. Prediction churn: how far a pass's AOS moved between when we first
   predicted it and when it happened, by lead time.

    python realdata/soak_report.py              # fetch new SatNOGS data, report
    python realdata/soak_report.py --offline    # use only cached SatNOGS data
"""

import argparse
import bisect
import collections
import copy
import glob
import json
import os
import sys
import time


HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import satnogs_check as sc  # noqa: E402
import soak_common as common  # noqa: E402


SATNOGS_CACHE = os.path.join(common.SOAK_DIR, "satnogs")
AGE_BINS_H = [(0, 12), (12, 24), (24, 48), (48, 96), (96, None)]
LEAD_BINS_H = [(0, 6), (6, 24), (24, 48), (48, None)]


# ---- loading -----------------------------------------------------------------


def load_events(folder=common.SOAK_DIR):
    events = []
    for path in sorted(glob.glob(os.path.join(folder, "events-*.jsonl"))):
        with open(path) as file:
            for line in file:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
    events.sort(key=lambda e: e["time"])
    return events


class TleTimeline:
    """The TLEs each satellite held, in the order the Flow Automator got them."""

    def __init__(self, events):
        self.by_norad = collections.defaultdict(list)  # norad -> [(received, epoch, line1, line2)]
        for event in events:
            if event["type"] == "tle":
                self.by_norad[event["norad"]].append(
                    (float(event["received"]), float(event["epoch"]), event["line1"], event["line2"])
                )
        for entries in self.by_norad.values():
            entries.sort()

    def held_at(self, norad, t):
        """(epoch, line1, line2) of the TLE held at time t, or None if none had arrived yet."""
        entries = self.by_norad.get(norad) or []
        index = bisect.bisect_right([e[0] for e in entries], t) - 1
        if index < 0:
            return None
        return entries[index][1:]


def final_activities(events):
    """Each rule activity's last recorded state."""
    activities = {}
    for event in events:
        if event["type"] == "activity":
            activities[event["id"]] = event
    return activities


# ---- statistics --------------------------------------------------------------


def stats(values):
    return sc._stats(values)


def _bin(value, bins):
    for low, high in bins:
        if value >= low and (high is None or value < high):
            return f"{low}-{high}h" if high is not None else f">{low}h"
    return "negative"


# ---- 1. operational accuracy ---------------------------------------------------


def operational_accuracy(observations, timeline, stations):
    """check_observation for each observation, with the TLE we held then instead of SatNOGS's."""
    results = []
    for observation in observations:
        start = sc.parse_time(observation["start"])
        held = timeline.held_at(observation["norad_cat_id"], start)
        if held is None:
            continue
        epoch, line1, line2 = held
        ours = copy.deepcopy(observation)
        ours["tle1"], ours["tle2"] = line1, line2
        result = sc.check_observation(ours, stations.get(observation.get("ground_station")))
        result["tle_age_h"] = round((start - epoch) / 3600, 2)
        try:
            theirs = sc.orbit.parse_tle(observation["tle1"], observation["tle2"])["epoch"]
            result["satnogs_tle_age_h"] = round((start - theirs) / 3600, 2)
            result["same_tle"] = observation["tle1"].strip() == line1.strip()
        except (sc.orbit.OrbitError, KeyError):
            pass
        results.append(result)
    return results


def accuracy_by_age(results):
    rows = []
    for low, high in AGE_BINS_H:
        label = _bin(low, [(low, high)])
        group = [r for r in results if "error" not in r and _bin(r["tle_age_h"], [(low, high)]) == label]
        timed = [r for r in group if r.get("full_pass") and "window_shifted" not in r["notes"]]
        rows.append(
            {
                "tle_age": label,
                "observations": len(group),
                "frames": sum(r.get("frames", 0) for r in group),
                "frames_below_horizon": sum(r.get("frames_below_horizon", 0) for r in group),
                "aos_diff_s": stats([r["aos_diff"] for r in timed]),
                "los_diff_s": stats([r["los_diff"] for r in timed]),
                "max_elevation_diff_deg": stats([r["max_elevation_diff"] for r in group if "max_elevation_diff" in r]),
            }
        )
    return rows


# ---- 2. coverage at the soak stations ----------------------------------------------


def station_coverage(observations, manifest, events, activities):
    """For SatNOGS passes at our stations: did we predict them, schedule them, and run them?"""
    station_by_satnogs = {v["satnogs_id"]: k for k, v in manifest["stations"].items()}
    sat_by_norad = {v["norad"]: k for k, v in manifest["satellites"].items()}
    predicted = collections.defaultdict(list)  # (sat, station) -> [(aos, los)]
    for event in events:
        if event["type"] in ("contact_new", "contact_moved"):
            predicted[(event["satellite_id"], event["station_id"])].append((event["aos"], event["los"]))
    by_pair = collections.defaultdict(list)
    for activity in activities.values():
        if activity.get("rule_id") == manifest.get("rule_id"):  # the soak's own, not post-pass checks
            by_pair[(activity["satellite_id"], activity["station_id"])].append(activity)

    rows = []
    for observation in observations:
        station_id = station_by_satnogs.get(observation.get("ground_station"))
        sat_id = sat_by_norad.get(observation.get("norad_cat_id"))
        if station_id is None or sat_id is None:
            continue
        start, end = sc.parse_time(observation["start"]), sc.parse_time(observation["end"])

        def overlaps(aos, los, start=start, end=end):
            return min(los, end) - max(aos, start) > 0

        was_predicted = any(overlaps(aos, los) for aos, los in predicted[(sat_id, station_id)])
        matching = [a for a in by_pair[(sat_id, station_id)] if a["aos"] and overlaps(a["aos"], a["los"])]
        outcome = "not scheduled"
        if matching:
            # The last activity for the pass (a cancelled one may have been replaced)
            outcome = sorted(matching, key=lambda a: a["time"])[-1]["status"]
        rows.append(
            {
                "observation": observation["id"],
                "satellite": manifest["satellites"][sat_id]["name"],
                "station": manifest["stations"][station_id]["name"],
                "start": observation["start"],
                "frames": len(observation.get("demoddata") or []),
                "predicted": was_predicted,
                "outcome": outcome,
            }
        )
    return rows


# ---- 3. scheduler behaviour -------------------------------------------------------


def in_maintenance(activity, windows):
    """Whether an activity's time (planned start to LOS) overlaps a maintenance window (an engine restart)."""
    anchor_time = activity.get(activity.get("anchor") or "aos") or activity.get("aos")
    if anchor_time is None or activity.get("los") is None:
        return False
    start = anchor_time + float(activity.get("offset") or 0)
    return any(start <= w["end"] and activity["los"] >= w["start"] for w in windows)


def scheduler_behaviour(activities, events, maintenance=()):
    affected = {k for k, a in activities.items() if in_maintenance(a, maintenance)}
    activities = {k: a for k, a in activities.items() if k not in affected}
    statuses = collections.Counter(a["status"] for a in activities.values())
    late = []
    for a in activities.values():
        if a.get("started_at") and a.get("aos") is not None:
            anchor_time = a.get(a.get("anchor") or "aos")
            if anchor_time is not None:
                late.append(a["started_at"] - (anchor_time + float(a.get("offset") or 0)))
    drift = [abs(a["drift"]) for a in activities.values() if a.get("drift")]
    messages = collections.Counter()
    for a in activities.values():
        if a["status"] in ("failed", "missed", "cancelled", "stopped"):
            text = str(a.get("message") or a["status"])
            messages[f"{a['status']}: {text[:90]}"] += 1
    retimes = sum(
        1
        for e in events
        if e["type"] == "activity"
        and e.get("previous_status")
        and (e.get("drift") or 0) != (e.get("previous_drift") or 0)
    )
    return {
        "activities": len(activities),
        "left_out_for_maintenance": len(affected),
        "statuses": dict(statuses),
        "start_lateness_s": stats(late),
        "late_over_5s": sum(1 for x in late if x > 5),
        "retime_events": retimes,
        "abs_drift_s": stats(drift),
        "problems": dict(messages.most_common(15)),
        "recorder_errors": sum(1 for e in events if e["type"] == "error"),
    }


# ---- 4. prediction churn ----------------------------------------------------------


def prediction_churn(events):
    first, last = {}, {}
    root = {}  # contact id -> id of the pass's first prediction (ids change when a pass moves far)
    for event in events:
        if event["type"] in ("contact_new", "contact_moved"):
            key = root.setdefault(event["id"], root.get(event.get("previous_id"), event["id"]))
            first.setdefault(key, event)
            last[key] = event
    rows = []
    for cid, initial in first.items():
        final = last[cid]
        if final["aos"] > (events[-1]["time"] if events else 0):
            continue  # hasn't happened yet: its prediction may still move
        lead_h = (initial["aos"] - initial["time"]) / 3600
        rows.append({"lead_h": lead_h, "aos_shift": final["aos"] - initial["aos"]})
    table = []
    for low, high in LEAD_BINS_H:
        label = _bin(low, [(low, high)])
        group = [r["aos_shift"] for r in rows if _bin(r["lead_h"], [(low, high)]) == label]
        table.append({"first_predicted": f"{label} before AOS", "contacts": len(group), "aos_shift_s": stats(group)})
    return table


# ---- 5. post-pass checks on real telemetry (step 3b) ---------------------------------


def post_pass_checks(manifest, activities):
    """Per satellite: how its post-pass checks came out (healthy, anomaly, not_heard ...)."""
    result = {}
    for name, info in (manifest.get("realtlm") or {}).items():
        mine = sorted((a for a in activities.values() if a.get("rule_id") == info["rule_id"]), key=lambda a: a["time"])
        finished = [a for a in mine if a.get("outcome")]
        result[name] = {
            "activities": len(mine),
            "finished": len(finished),
            "outcomes": dict(collections.Counter(a["outcome"] for a in finished)),
            "anomalies": [a.get("summary") or a["id"] for a in finished if a["outcome"] == "anomaly"],
            "last_summary": next((a.get("summary") for a in reversed(finished) if a.get("summary")), None),
        }
    return result


def cfs_passes(manifest, activities):
    """Step 4: how the real-pass cFS pass ops came out (nominal / recovered / failed, and the LOS check)."""
    rule = (manifest.get("cfs") or {}).get("rule_id")
    if not rule:
        return None
    finished = [a for a in activities.values() if a.get("rule_id") == rule and a.get("outcome")]
    return {
        "finished": len(finished),
        "outcomes": dict(collections.Counter(a["outcome"] for a in finished)),
        "los": dict(collections.Counter(str(a.get("los")) for a in finished)),
    }


# ---- SatNOGS data -----------------------------------------------------------------


def fetch_observations(client, manifest, offline=False):
    """Every good SatNOGS observation of a soak satellite since the soak started (cached per observation)."""
    os.makedirs(SATNOGS_CACHE, exist_ok=True)
    if not offline:
        for info in manifest["satellites"].values():
            url = f"{sc.NETWORK_API}/observations/"
            params = {
                "format": "json",
                "status": "good",
                "norad_cat_id": info["norad"],
                "start": manifest["started_iso"],
            }
            while url:
                response = client._get(url, params)
                batch = response.json()
                for observation in batch:
                    common.save_json(os.path.join(SATNOGS_CACHE, f"{observation['id']}.json"), observation)
                url, params = response.links.get("next", {}).get("url"), None
                if not batch:
                    break
    observations = []
    started = manifest["started"]
    for path in glob.glob(os.path.join(SATNOGS_CACHE, "*.json")):
        observation = common.load_json(path)
        if sc.parse_time(observation["start"]) >= started:
            observations.append(observation)
    return observations


# ---- output -------------------------------------------------------------------------


def _fmt(s):
    if not s:
        return "–"
    return f"{s['median']:.2f} / {s['p95_abs']:.2f} / {s['max_abs']:.2f} (n={s['count']})"


def markdown(report):
    lines = [
        f"# Soak report {report['generated']}",
        "",
        f"Soak started {report['started']} ({report['days']:.1f} days). "
        f"{report['satellites']} satellites, {report['stations']} stations.",
        "",
    ]
    lines += [
        "## 1. Operational accuracy (TLE we held vs what SatNOGS heard)",
        "",
        f"{report['accuracy_total']} observations, {report['frames_total']} frames, "
        f"**{report['frames_below_total']} frames heard below our predicted horizon**.",
        "",
        "| TLE age | Obs | Frames | Below horizon | AOS diff s (median / p95 / max) | LOS diff s | Max el diff ° |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in report["accuracy_by_age"]:
        lines.append(
            f"| {row['tle_age']} | {row['observations']} | {row['frames']} | {row['frames_below_horizon']} | "
            f"{_fmt(row['aos_diff_s'])} | {_fmt(row['los_diff_s'])} | {_fmt(row['max_elevation_diff_deg'])} |"
        )
    if report["worst_frames"]:
        lines += ["", "Frames below our horizon:"]
        lines += [
            f"- obs {r['id']} {r['satellite']} @ {r['station']}: {r['frames_below_horizon']}/{r['frames']} "
            f"frames, min {r['min_frame_elevation']}°, TLE age {r['tle_age_h']} h"
            for r in report["worst_frames"]
        ]
    coverage = report["coverage"]
    counts = collections.Counter(r["outcome"] for r in coverage)
    lines += [
        "",
        "## 2. Coverage at the soak stations",
        "",
        f"{len(coverage)} SatNOGS-heard passes at our stations; "
        f"predicted {sum(r['predicted'] for r in coverage)}; outcomes: "
        + (", ".join(f"{k} {v}" for k, v in counts.most_common()) or "none"),
    ]
    missed = [r for r in coverage if not r["predicted"]]
    for r in missed[:20]:
        lines.append(f"- NOT PREDICTED: obs {r['observation']} {r['satellite']} @ {r['station']} {r['start']}")
    behaviour = report["scheduler"]
    lines += [
        "",
        "## 3. Scheduler behaviour",
        "",
        f"- Activities: {behaviour['activities']} — "
        + ", ".join(f"{k} {v}" for k, v in sorted(behaviour["statuses"].items())),
        f"- Left out (engine restarts for upgrades, gaps in the recording): {behaviour['left_out_for_maintenance']}",
        f"- Gaps in the recording (laptop asleep, COSMOS or the recorder down): {len(report['gaps'])}, "
        f"{sum(g['end'] - g['start'] for g in report['gaps']) / 3600:.1f} h",
        f"- Start lateness s (median / p95 / max): {_fmt(behaviour['start_lateness_s'])}; "
        f"over 5 s late: {behaviour['late_over_5s']}",
        f"- Retimes: {behaviour['retime_events']} events; |drift| s: {_fmt(behaviour['abs_drift_s'])}",
        f"- Recorder errors (COSMOS unreachable): {behaviour['recorder_errors']}",
    ]
    for text, count in behaviour["problems"].items():
        lines.append(f"  - {count} × {text}")
    lines += [
        "",
        "## 4. Prediction churn (first prediction → final, AOS)",
        "",
        "| First predicted | Contacts | AOS shift s (median / p95 / max) |",
        "|---|---|---|",
    ]
    for row in report["churn"]:
        lines.append(f"| {row['first_predicted']} | {row['contacts']} | {_fmt(row['aos_shift_s'])} |")
    lines += ["", "## 5. Post-pass checks on real telemetry (step 3b)", ""]
    if not report["post_pass"]:
        lines.append("Not set up (realdata/realtlm_setup.py).")
    for name, row in report["post_pass"].items():
        counts = ", ".join(f"{k} {v}" for k, v in sorted(row["outcomes"].items())) or "none finished yet"
        lines.append(f"- **{name}**: {row['finished']} finished of {row['activities']} — {counts}")
        for text in row["anomalies"][:10]:
            lines.append(f"  - anomaly: {text}")
        if row["last_summary"]:
            lines.append(f"  - last: {row['last_summary']}")
    cfs = report.get("cfs")
    lines += ["", "## 6. Commanding cFS in real passes (step 4)", ""]
    if not cfs:
        lines.append("Not set up (realdata/cfs_setup.py).")
    else:
        outcomes = ", ".join(f"{k} {v}" for k, v in sorted(cfs["outcomes"].items())) or "none finished yet"
        los = ", ".join(f"{k} {v}" for k, v in sorted(cfs["los"].items()))
        lines.append(f"- {cfs['finished']} passes worked: {outcomes}; LOS check: {los or '-'}")
    return "\n".join(lines) + "\n"


def build_report(manifest, events, observations, stations):
    timeline = TleTimeline(events)
    # Upgrades (soak.sh install) and gaps in the recording (the laptop slept): left out of the scores
    gaps = [{"start": e["start"], "end": e["end"], "why": "gap in the recording"} for e in events if e["type"] == "gap"]
    windows = (manifest.get("maintenance") or []) + gaps
    accuracy = operational_accuracy(observations, timeline, stations)
    activities = final_activities(events)
    checked = [r for r in accuracy if "error" not in r]
    worst = sorted([r for r in checked if r.get("frames_below_horizon")], key=lambda r: r["min_frame_elevation"])
    now = time.time()
    return {
        "generated": common.now_iso(now),
        "started": manifest["started_iso"],
        "days": (now - manifest["started"]) / 86400,
        "satellites": len(manifest["satellites"]),
        "stations": len(manifest["stations"]),
        "accuracy_total": len(checked),
        "frames_total": sum(r.get("frames", 0) for r in checked),
        "frames_below_total": sum(r.get("frames_below_horizon", 0) for r in checked),
        "accuracy_by_age": accuracy_by_age(checked),
        "worst_frames": worst[:20],
        "coverage": station_coverage(observations, manifest, events, activities),
        "scheduler": scheduler_behaviour(activities, events, windows),
        "gaps": gaps,
        "churn": prediction_churn(events),
        "post_pass": post_pass_checks(manifest, final_activities(events)),
        "cfs": cfs_passes(manifest, final_activities(events)),
        "accuracy": accuracy,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--offline", action="store_true", help="don't fetch from SatNOGS")
    parser.add_argument("--token", default=os.environ.get("SATNOGS_TOKEN"))
    parser.add_argument(
        "--maintenance",
        nargs=3,
        metavar=("START", "END", "WHY"),
        help="record a maintenance window (Unix seconds) the scores leave out, and stop",
    )
    args = parser.parse_args(argv)

    manifest = common.load_json(common.MANIFEST)
    if not manifest:
        raise SystemExit(f"No soak manifest at {common.MANIFEST}")
    if args.maintenance:
        start, end, why = float(args.maintenance[0]), float(args.maintenance[1]), args.maintenance[2]
        manifest.setdefault("maintenance", []).append({"start": start, "end": end, "why": why})
        common.save_json(common.MANIFEST, manifest)
        print(f"Maintenance {common.now_iso(start)} to {common.now_iso(end)} recorded: {why}")
        return 0
    events = load_events()
    client = sc.SatnogsClient(token=args.token, offline=args.offline)
    stations = client.stations()
    observations = fetch_observations(client, manifest, args.offline)
    report = build_report(manifest, events, observations, stations)

    text = markdown(report)
    print(text)
    os.makedirs(sc.REPORT_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    with open(os.path.join(sc.REPORT_DIR, f"soak_report_{stamp}.md"), "w") as file:
        file.write(text)
    common.save_json(os.path.join(sc.REPORT_DIR, f"soak_report_{stamp}.json"), report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
