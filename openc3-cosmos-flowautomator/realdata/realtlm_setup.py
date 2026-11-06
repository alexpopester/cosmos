"""Post-pass checks on real satellite telemetry (docs/REAL-DATA-TESTING.md step 3b).

Needs the soak (soak_setup.py) and the openc3-cosmos-satnogs plugin, which
brings CANVAS, AEPEX and CatSat beacons from SatNOGS into COSMOS. Creates, all
named "RealTLM: ...":

- one post-pass template per satellite, like a mission's own: start 30 s before
  LOS, wait up to WAIT_S for a beacon heard at or after this pass's AOS (any
  SatNOGS station; frames are uploaded after the pass), read it, log who heard
  it and the key values, then check health: an alert names what's off, a
  notification says no beacon was heard
- one rule per satellite, running it at two soak stations that work that
  satellite (as its own ground stations), in run mode

Health limits come from the real values seen in November 2026 (step 3b in the
plan has the table). Values are raw: the SatNOGS decoders give no units.

    COSMOS_PASSWORD=... python realdata/realtlm_setup.py          # or: soak token in realdata/cache/soak/token
    python realdata/realtlm_setup.py --clear
"""

import argparse
import os
import sys


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import soak_common as common  # noqa: E402


PREFIX = "RealTLM: "
WAIT_S = 5400  # frames come in minutes to hours after the pass; the plugin polls every 15 min
POLL_S = 30  # how often the wait looks at the beacon's FRAME_TIME


def _node(node_id, node_type, x, y=120, **data):
    return {"id": node_id, "type": node_type, "position": {"x": x, "y": y}, "data": data}


def _edge(source, target, handle="out"):
    return {"id": f"{source}-{handle}-{target}", "source": source, "sourceHandle": handle, "target": target}


# satellite: (beacon "TARGET PACKET", soak satellite name, SatNOGS stations, summary template, health, problems)
# ``b`` is the beacon packet read into a variable. ``problems`` builds the alert text, naming what's off.
MISSIONS = {
    "CANVAS": {
        "beacon": "CANVAS APID_32",
        "satellite": "CANVAS",
        "stations": [2029, 2134],  # EU1AEM (Belarus), MAUSyagi (Colorado)
        "summary": "commands {{ b['BCN_CMD_RECV_CNT'] }} received / {{ b['BCN_CMD_SUCC_CNT'] }} ok / "
        "{{ b['BCN_CMD_RJCT_CNT'] }} rejected, FSW v{{ b['BCN_FSW_MAJOR_VERSION'] }}, "
        "battery temps {{ b['BCN_EPS_BATT1_TEMP'] }} / {{ b['BCN_EPS_BATT2_TEMP'] }}, "
        "{{ 'eclipse' if b['BCN_ADCS_ECLIPSE'] else 'sunlit' }}",
        "health": "b['BCN_CMD_RJCT_CNT'] == 0 and b['BCN_CMD_FAIL_CODE'] == 0 and b['BCN_FP_ERR_CNT'] == 0 "
        "and b['BCN_ADCS_ALIVE'] == 2 and 2000 <= b['BCN_EPS_BATT1_TEMP'] <= 2250 "
        "and 2000 <= b['BCN_EPS_BATT2_TEMP'] <= 2250",
        "problems": "('rejected commands ' if b['BCN_CMD_RJCT_CNT'] != 0 else '') + "
        "('command fail code ' if b['BCN_CMD_FAIL_CODE'] != 0 else '') + "
        "('fault protection errors ' if b['BCN_FP_ERR_CNT'] != 0 else '') + "
        "('ADCS not alive ' if b['BCN_ADCS_ALIVE'] != 2 else '') + "
        "('battery temperature ' if not (2000 <= b['BCN_EPS_BATT1_TEMP'] <= 2250 "
        "and 2000 <= b['BCN_EPS_BATT2_TEMP'] <= 2250) else '')",
    },
    "AEPEX": {
        "beacon": "AEPEX APID_1",
        "satellite": "AEPEX",
        "stations": [2134, 2051],  # MAUSyagi (Colorado), MAUSyagi-AK (Alaska)
        "summary": "commands {{ b['SW_CMD_RECV_COUNT'] }} received / {{ b['SW_CMD_SUCC_COUNT'] }} ok / "
        "{{ b['SW_CMD_RJCT_COUNT'] }} rejected, mode {{ b['SW_MODE_SYSTEM_MODE'] }}, "
        "last uplink {{ round(b['SW_TIME_SINCE_RX'] / 3600, 1) }} h ago",
        "health": "b['SW_CMD_RJCT_COUNT'] == 0 and b['SW_CMD_FAIL_CODE'] == 0 and b['SW_MODE_SYSTEM_MODE'] == 1 "
        "and b['SW_BAT_ALIVE_STATE_BATTERY0'] == 1 and b['SW_UHF_ALIVE'] == 1 and b['SW_TIME_SINCE_RX'] < 172800",
        "problems": "('rejected commands ' if b['SW_CMD_RJCT_COUNT'] != 0 else '') + "
        "('command fail code ' if b['SW_CMD_FAIL_CODE'] != 0 else '') + "
        "('mode ' + str(b['SW_MODE_SYSTEM_MODE']) + ' ' if b['SW_MODE_SYSTEM_MODE'] != 1 else '') + "
        "('battery not alive ' if b['SW_BAT_ALIVE_STATE_BATTERY0'] != 1 else '') + "
        "('UHF not alive ' if b['SW_UHF_ALIVE'] != 1 else '') + "
        "('no uplink for 2 days ' if b['SW_TIME_SINCE_RX'] >= 172800 else '')",
    },
    "CATSAT": {
        "beacon": "CATSAT TYPE_4",
        "satellite": "CATSAT",
        "stations": [2107, 2029],  # JH4XSY-pi (Japan), EU1AEM (Belarus)
        "summary": "battery {{ b['P60_BATT_V'] }} mV, charge {{ b['P60_BATT_CHRG'] }} / discharge "
        "{{ b['P60_BATT_DCHRG'] }} mA, board {{ b['P60_TEMP_0'] }}, up {{ round(b['P60_UPTIME'] / 3600, 1) }} h",
        "health": "b['P60_BATT_V'] >= 14500 and b['P60_LATCHUP_ACU_X1_VCC'] == 0 and b['P60_LATCHUP_PDU_X2_VCC'] == 0 "
        "and b['P60_LATCHUP_PDU_X3_VCC'] == 0 and 300 <= b['P60_TEMP_0'] <= 420",
        "problems": "('battery low ' + str(b['P60_BATT_V']) + ' mV ' if b['P60_BATT_V'] < 14500 else '') + "
        "('latch-up ' if b['P60_LATCHUP_ACU_X1_VCC'] or b['P60_LATCHUP_PDU_X2_VCC'] "
        "or b['P60_LATCHUP_PDU_X3_VCC'] else '') + "
        "('board temperature ' + str(b['P60_TEMP_0']) + ' ' if not 300 <= b['P60_TEMP_0'] <= 420 else '')",
    },
}


def template_flow(name, target=None, wait_s=WAIT_S, early_s=300, poll_s=POLL_S, prefix=PREFIX):
    """The post-pass template for one satellite.

    ``target``: read the beacon from another target (a replay: REPLAY_CANVAS). ``early_s``: how long before
    AOS a beacon still counts as this pass's (clock and upload slack; replays are exact).
    """
    mission = MISSIONS[name]
    beacon_target, packet = mission["beacon"].split()
    target = target or beacon_target
    # NEWEST_FRAME_TIME, not FRAME_TIME: a late upload of an older pass can arrive after this one and roll
    # FRAME_TIME back. (or 0): nothing received yet counts as not heard, not as an error
    heard = f"(tlm('{target} {packet} NEWEST_FRAME_TIME') or 0) >= contact_aos - {early_s:g}"
    return {
        "name": f"{prefix}{name} post-pass",
        "description": f"Real telemetry check after each {name} pass (docs/REAL-DATA-TESTING.md step 3b). "
        "Sends no commands.",
        "mode": "run",
        "template": True,
        "parameters": [],
        "nodes": [
            _node("t", "trigger_manual", 0),
            _node("los", "wait_contact", 220, label="LOS", anchor="los", offset=0),
            _node(
                "wait", "wait_tlm", 440, label="Beacon from this pass?", condition=heard, timeout=wait_s, poll=poll_s
            ),
            _node("read", "read_packet", 660, label="Read beacon", target=target, packet=packet, variable="b"),
            _node(
                "log",
                "log",
                880,
                label="Summary",
                message=f"{name} heard by {{{{ b['STATION'] }}}} at {{{{ utc(b['FRAME_TIME']) }}}}: "
                + mission["summary"],
            ),
            _node("ok", "if", 1100, label="Healthy?", condition=mission["health"]),
            _node("good", "log", 1320, 60, label="Healthy", message=f"{name} healthy after the pass"),
            _node(
                "bad",
                "notify",
                1320,
                200,
                label="Anomaly",
                level="alert",
                title=f"{name} anomaly",
                message=f"{name} after its pass: {{{{ {mission['problems']} }}}}(heard by {{{{ b['STATION'] }}}})",
            ),
            _node(
                "silent",
                "notify",
                660,
                300,
                label="Not heard",
                message=f"No {name} beacon heard within {wait_s / 60:g} min of LOS "
                "(pass over {{ contact_station }}, AOS {{ utc(contact_aos) }})",
            ),
        ],
        "edges": [
            _edge("t", "los"),
            _edge("los", "wait"),
            _edge("wait", "read", "ok"),
            _edge("wait", "silent", "timeout"),
            _edge("read", "log"),
            _edge("log", "ok"),
            _edge("ok", "good", "true"),
            _edge("ok", "bad", "false"),
        ],
    }


def rule_payload(name, template_id, satellite_ids, station_ids):
    return {
        "name": f"{PREFIX}{name} after every pass",
        "template_id": template_id,
        "satellite_ids": list(satellite_ids),
        "station_ids": list(station_ids),
        "min_elevation": 10,
        "anchor": "los",
        "offset": -30,
        "mode": "run",
        "stop_at_los": False,  # the check runs after LOS, while frames are uploaded
        "priority": 10,
        "skip_full_stations": True,  # a pass the soak couldn't take (station busy) wasn't worked
    }


def clear(fa):
    removed = {"rules": 0, "flows": 0}
    for rule in fa.get("/rules") or []:
        if rule["name"].startswith(PREFIX):
            fa.delete(f"/rules/{rule['id']}")
            removed["rules"] += 1
    flows = fa.get("/flows")
    for flow in flows.get("flows", flows) if isinstance(flows, dict) else flows:
        if flow["name"].startswith(PREFIX):
            fa.delete(f"/flows/{flow['id']}")
            removed["flows"] += 1
    return removed


def setup(fa, manifest):
    print("Removing an earlier setup:", clear(fa))
    sats = {info["name"]: sat_id for sat_id, info in manifest["satellites"].items()}
    stations = {info["satnogs_id"]: st_id for st_id, info in manifest["stations"].items()}
    result = {}
    for name, mission in MISSIONS.items():
        sat_id = sats.get(mission["satellite"])
        station_ids = [stations[s] for s in mission["stations"] if s in stations]
        if not sat_id or not station_ids:
            print(f"{name}: not in the soak, skipped", file=sys.stderr)
            continue
        flow = fa.post("/flows", template_flow(name))
        flow = flow.get("flow", flow)
        rule = fa.post("/rules", rule_payload(name, flow["id"], [sat_id], station_ids))
        result[name] = {"template_id": flow["id"], "rule_id": rule["rule"]["id"], "activities": rule["created"]}
        print(f"{name}: rule scheduled {rule['created']} post-pass checks")
    manifest["realtlm"] = result
    common.save_json(common.MANIFEST, manifest)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--clear", action="store_true")
    parser.add_argument("--url", default=os.environ.get("COSMOS_URL", "http://localhost:2900"))
    args = parser.parse_args(argv)
    fa = common.FlowAutomator(args.url, password=os.environ.get("COSMOS_PASSWORD"))
    if args.clear:
        print("Removed:", clear(fa))
        return 0
    manifest = common.load_json(common.MANIFEST)
    if not manifest:
        raise SystemExit("Set up the soak first (realdata/soak.sh setup)")
    setup(fa, manifest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
