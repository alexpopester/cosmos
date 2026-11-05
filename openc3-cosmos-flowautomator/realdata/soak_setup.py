"""Set up (or remove) the real-pass soak in the local COSMOS's Flow Automator.

Step 2 of docs/REAL-DATA-TESTING.md. Creates, all named "Soak: ...":

- satellites from soak_common.SATELLITES, kept current from CelesTrak
- the SatNOGS stations in soak_common.STATIONS, at their real position and
  horizon, capacity 1 (a SatNOGS station tracks one satellite at a time)
- a harmless template, "Soak: Pass log": log before AOS, wait for AOS, read
  INST HEALTH_STATUS, wait for TCA, check the time left, wait for LOS, log
- one rule scheduling it into every pass of those satellites over those
  stations (AOS - 60 s, run mode, stops at LOS)

and records the ids in realdata/cache/soak/manifest.json for the recorder
and the report.

    COSMOS_PASSWORD=... python realdata/soak_setup.py           # set up (replaces an earlier soak)
    python realdata/soak_setup.py --clear                        # remove everything it made
"""

import argparse
import os
import sys
import time

import requests


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import soak_common as common  # noqa: E402


TEMPLATE_NAME = f"{common.PREFIX}Pass log"
RULE_NAME = f"{common.PREFIX}every pass"


def _node(node_id, node_type, x, **data):
    return {"id": node_id, "type": node_type, "position": {"x": x, "y": 120}, "data": data}


def _edge(source, target, handle="out"):
    return {"id": f"{source}-{handle}-{target}", "source": source, "sourceHandle": handle, "target": target}


def template_flow():
    """The soak's template: touches nothing, but holds a run open for the whole pass."""
    return {
        "name": TEMPLATE_NAME,
        "description": "Real-pass soak (docs/REAL-DATA-TESTING.md step 2). Sends no commands.",
        "mode": "run",
        "template": True,
        "parameters": [],
        "nodes": [
            _node("t", "trigger_manual", 0),
            _node(
                "pre",
                "log",
                240,
                label="Pre-pass",
                message="{{ contact_satellite }} over {{ contact_station }}: AOS in {{ round(time_to_aos()) }}s, "
                "max elevation {{ contact_max_elevation }}",
            ),
            _node("aos", "wait_contact", 480, label="AOS", anchor="aos", offset=0),
            _node("read", "read_packet", 720, label="Read INST", target="INST", packet="HEALTH_STATUS", variable="hs"),
            _node("noread", "log", 720, label="No INST", message="INST HEALTH_STATUS unavailable"),
            _node("tca", "wait_contact", 960, label="TCA", anchor="tca", offset=0),
            _node("left", "contact_check", 1200, label="Time left?", min_remaining=30),
            _node("short", "log", 1440, label="Short", message="Less than 30s left at TCA"),
            _node("los", "wait_contact", 1680, label="LOS - 5", anchor="los", offset=-5),
            _node("done", "log", 1920, label="Done", message="{{ contact_satellite }} pass logged"),
        ],
        "edges": [
            _edge("t", "pre"),
            _edge("pre", "aos"),
            _edge("aos", "read"),
            _edge("read", "tca"),
            _edge("read", "noread", "error"),
            _edge("noread", "tca"),
            _edge("tca", "left"),
            _edge("left", "los", "ok"),
            _edge("left", "short", "short"),
            _edge("short", "los"),
            _edge("los", "done"),
        ],
    }


def station_payload(info):
    """A Flow Automator station for a SatNOGS station (its entry in the SatNOGS station list)."""
    return {
        "name": f"{common.PREFIX}{info['name']} #{info['id']}",
        "latitude": info["lat"],
        "longitude": info["lng"],
        "altitude": info["altitude"] or 0,
        "min_elevation": max(0, min(89, info["min_horizon"] or 0)),
        "capacity": 1,  # a SatNOGS station tracks one satellite at a time
    }


def rule_payload(template_id, satellite_ids, station_ids):
    """Every pass of the soak's satellites over its stations, from 60 s before AOS until LOS."""
    return {
        "name": RULE_NAME,
        "template_id": template_id,
        "satellite_ids": list(satellite_ids),
        "station_ids": list(station_ids),
        "min_elevation": 0,
        "anchor": "aos",
        "offset": -60,
        "mode": "run",
        "stop_at_los": True,
        "priority": 20,
        "skip_full_stations": True,
    }


def satnogs_stations():
    response = requests.get(
        "https://network.satnogs.org/api/stations/",
        params={"format": "json"},
        headers={"User-Agent": "openc3-flowautomator-realdata-soak"},
        timeout=120,
    )
    response.raise_for_status()
    return {station["id"]: station for station in response.json()}


def clear(fa):
    """Remove everything named with the soak prefix. Returns counts."""
    removed = {"rules": 0, "flows": 0, "satellites": 0, "stations": 0}
    for rule in fa.get("/rules") or []:
        if rule["name"].startswith(common.PREFIX):
            fa.delete(f"/rules/{rule['id']}")  # also removes its scheduled activities
            removed["rules"] += 1
    flows = fa.get("/flows")
    for flow in flows.get("flows", flows) if isinstance(flows, dict) else flows:
        if flow["name"].startswith(common.PREFIX):
            fa.delete(f"/flows/{flow['id']}")
            removed["flows"] += 1
    schedule = fa.get("/schedule")
    for sat in schedule["satellites"]:
        if sat["name"].startswith(common.PREFIX):
            fa.delete(f"/satellites/{sat['id']}")
            removed["satellites"] += 1
    for station in schedule["stations"]:
        if station["name"].startswith(common.PREFIX):
            fa.delete(f"/stations/{station['id']}")
            removed["stations"] += 1
    return removed


def setup(fa):
    print("Removing an earlier soak:", clear(fa))
    stations = satnogs_stations()
    manifest = {"started": time.time(), "started_iso": common.now_iso(), "satellites": {}, "stations": {}}

    for norad, name in common.SATELLITES:
        sat = fa.post(
            "/satellites",
            {"name": f"{common.PREFIX}{name}", "catalog": str(norad), "tle_source": {"kind": "celestrak"}},
        )["satellite"]
        manifest["satellites"][sat["id"]] = {"norad": norad, "name": name}
    for station_id in common.STATIONS:
        info = stations.get(station_id)
        if info is None:
            print(f"SatNOGS station {station_id} no longer exists: skipped", file=sys.stderr)
            continue
        station = fa.post("/stations", station_payload(info))["station"]
        manifest["stations"][station["id"]] = {
            "satnogs_id": station_id,
            "name": info["name"],
            "min_horizon": info["min_horizon"],
            "status": info["status"],
        }

    print("Fetching TLEs from CelesTrak (one request every few seconds)...")
    fa.post("/tle_sources/fetch", {"kind": "celestrak"})
    deadline = time.time() + 600
    while time.time() < deadline:
        sats = [s for s in fa.get("/schedule")["satellites"] if s["id"] in manifest["satellites"]]
        if all(s.get("tle") for s in sats) or all((s.get("tle_fetch") or {}).get("result") for s in sats):
            break
        time.sleep(10)
    for sat in sats:
        if not sat.get("tle"):
            print(f"No TLE for {sat['name']}: {sat.get('tle_fetch')}", file=sys.stderr)

    template = fa.post("/flows", template_flow())
    template = template.get("flow", template)
    manifest["template_id"] = template["id"]
    rule = fa.post("/rules", rule_payload(template["id"], manifest["satellites"], manifest["stations"]))
    manifest["rule_id"] = rule["rule"]["id"]
    common.save_json(common.MANIFEST, manifest)
    print(f"Rule created {rule['created']} activities. Manifest: {common.MANIFEST}")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--clear", action="store_true", help="remove the soak (rule, template, satellites, stations)")
    parser.add_argument("--url", default=os.environ.get("COSMOS_URL", "http://localhost:2900"))
    parser.add_argument("--scope", default="DEFAULT")
    args = parser.parse_args(argv)

    password = os.environ.get("COSMOS_PASSWORD")
    fa = common.FlowAutomator(args.url, args.scope, password=password)
    if password:
        common.write_token(fa.token)  # the recorder uses this session token, never the password
    if args.clear:
        print("Removed:", clear(fa))
        return 0
    setup(fa)
    return 0


if __name__ == "__main__":
    sys.exit(main())
