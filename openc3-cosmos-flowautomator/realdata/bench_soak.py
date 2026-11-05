"""Benchmark the planner at the soak's real scale, in memory (no COSMOS needed).

Rebuilds the real-pass soak (docs/REAL-DATA-TESTING.md step 2) on an
in-memory engine: the TLEs the soak recorded, its SatNOGS stations, its
template and rule, a 7-day horizon. Then times what the Contacts view and
/status call (EVALUATION-2026-11-05 B43, B45).

    python realdata/bench_soak.py            # needs realdata/cache/soak (run the soak first)
"""

import gzip
import json
import os
import statistics
import sys
import threading
import time


HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "lib"))
sys.path.insert(0, os.path.join(HERE, "..", "test"))

import soak_common as common  # noqa: E402
import soak_report  # noqa: E402
import soak_setup  # noqa: E402
from conftest import FakeBackend, ListLogger  # noqa: E402
from flow_automator.engine import FlowEngine  # noqa: E402
from flow_automator.store import FlowStore, MemoryRedis  # noqa: E402


def timed(fn, repeat=10):
    times = []
    result = None
    for _ in range(repeat):
        start = time.perf_counter()
        result = fn()
        times.append(time.perf_counter() - start)
    times.sort()
    return result, {"p50_ms": round(statistics.median(times) * 1000, 1), "max_ms": round(times[-1] * 1000, 1)}


def build():
    manifest = common.load_json(common.MANIFEST)
    events = soak_report.load_events()
    tles = {}
    for event in events:
        if event["type"] == "tle":
            tles[event["norad"]] = (event["line1"], event["line2"])
    stations = soak_setup.satnogs_stations() if "--fresh" in sys.argv else None

    engine = FlowEngine(FlowStore(MemoryRedis(), "DEFAULT"), FakeBackend(), ListLogger(), trigger_period=0.05)
    engine.start(start_triggers=False)
    planner = engine.planner
    planner.horizon = 7 * 86400
    template, _ = engine.save_flow(soak_setup.template_flow())
    sat_ids = []
    for info in manifest["satellites"].values():
        if info["norad"] in tles:
            line1, line2 = tles[info["norad"]]
            sat_ids.append(planner.save_satellite({"name": info["name"], "tle": f"{line1}\n{line2}"})["id"])
    station_ids = []
    for station in manifest["stations"].values():
        info = (stations or {}).get(station["satnogs_id"]) or _station_from_cache(station["satnogs_id"])
        station_ids.append(planner.save_station(soak_setup.station_payload(info))["id"])
    result = planner.save_rule(soak_setup.rule_payload(template["id"], sat_ids, station_ids))
    return engine, planner, len(sat_ids), len(station_ids), result["created"]


def _station_from_cache(satnogs_id):
    stations = common.load_json(os.path.join(HERE, "cache", "stations.json"))
    return next(s for s in stations if s["id"] == satnogs_id)


def main():
    start = time.perf_counter()
    engine, planner, sats, stations, created = build()
    print(
        f"Built {sats} satellites x {stations} stations, {len(planner.contacts)} contacts, "
        f"{created} activities in {time.perf_counter() - start:.1f}s"
    )
    now = planner.clock()
    report = {}
    schedule, report["schedule 1-day"] = timed(lambda: planner.schedule(now - 900, now + 86400))
    body = json.dumps(schedule).encode()
    report["schedule 1-day"]["bytes"] = len(body)
    report["schedule 1-day"]["gzip_bytes"] = len(gzip.compress(body, 5))
    _, report["schedule 1-day + json"] = timed(lambda: json.dumps(planner.schedule(now - 900, now + 86400)))
    _, report["planner status"] = timed(planner.status, 20)
    _, report["engine status"] = timed(engine.status, 20)

    # /status from 5 clients while the planner ticks: how long do ticks take?
    stop = threading.Event()

    def client():
        while not stop.is_set():
            engine.status()

    clients = [threading.Thread(target=client, daemon=True) for _ in range(5)]
    for thread in clients:
        thread.start()
    _, report["tick with 5 /status clients"] = timed(planner.tick, 20)
    _, report["status with 5 /status clients"] = timed(engine.status, 20)
    stop.set()
    for name, value in report.items():
        print(f"{name:32} {value}")
    engine.shutdown()


if __name__ == "__main__":
    main()
