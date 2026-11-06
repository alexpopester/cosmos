"""Commanding real flight software in contacts (docs/REAL-DATA-TESTING.md step 4).

Needs the openc3-cosmos-cfs plugin and its containers (cFS behind a contact gate that
passes traffic only while Fairbanks can see NOAA 19). Creates, all named "CFS: ...":

- satellite "CFS: NOAA 19" (the orbit the cFS flies; TLE from CelesTrak like the gate)
- station "CFS: Fairbanks" (where the gate's ground station is)
- template "CFS: pass ops": at AOS, switch telemetry on (needed after any restart),
  acquire housekeeping, send executive services and sample app no-ops and check each
  command counter went up by one. Any gap (no telemetry, no acknowledgement: a dropout,
  a reboot) gets one more full attempt; then an alert names what failed. After LOS it
  checks telemetry stopped, which compares the planner's LOS with the gate's.
- a rule running it in every pass (about ten a day)

    COSMOS_PASSWORD=... python realdata/cfs_setup.py          # or the soak's stored token
    python realdata/cfs_setup.py --clear
"""

import argparse
import os
import sys


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import soak_common as common  # noqa: E402


PREFIX = "CFS: "
NORAD = 33591  # NOAA 19: what the gate flies (docker/run.sh NORAD)
STATION = {"latitude": 64.85, "longitude": -147.85, "altitude": 130, "min_elevation": 10}  # the gate's
ACQUIRE_S = 30  # wait for housekeeping after switching telemetry on
ACK_S = 15  # wait for a command counter to go up
LOS_QUIET_S = 20  # after LOS + this, telemetry should have stopped

# Where each run ended up, by the nodes it reached (see outcome())
OUTCOMES = (("failed", "failed"), ("recovered", "recovered"), ("done", "nominal"))


def _node(node_id, node_type, x, y=120, **data):
    return {"id": node_id, "type": node_type, "position": {"x": x, "y": y}, "data": data}


def _edge(source, target, handle="out"):
    return {"id": f"{source}-{handle}-{target}", "source": source, "sourceHandle": handle, "target": target}


def template_flow(acquire_s=ACQUIRE_S, ack_s=ACK_S, los_quiet_s=LOS_QUIET_S, prefix=PREFIX):
    hk = "CFS ES_HK CMD_CNT"
    return {
        "name": f"{prefix}pass ops",
        "description": "Command real cFS flight software in a contact (docs/REAL-DATA-TESTING.md step 4): "
        "telemetry on, acquire, no-ops acknowledged, one retry, LOS confirmed.",
        "mode": "run",
        "template": True,
        "parameters": [],
        "nodes": [
            _node("t", "trigger_manual", 0),
            _node(
                "init",
                "set_var",
                200,
                label="Start",
                name="ok",
                expression="False",
                more={"attempt": "0", "stage": "'telemetry'", "lost": "''"},
            ),
            _node("aos", "wait_contact", 400, label="AOS", anchor="aos", offset=0),
            _node(
                "loop",
                "loop",
                600,
                label="Up to 2 attempts",
                mode="while",
                condition="not ok and attempt < 2",
                max_iterations=3,
            ),
            _node(
                "next",
                "set_var",
                800,
                label="Attempt",
                name="attempt",
                expression="attempt + 1",
                # The packets' own RECEIVED_COUNT, not COSMOS's packet count: that one moves before the packet's
                # values do, so after a reboot the counters read next were the old ones (found by cfs_faults.py)
                more={
                    "stage": "'telemetry'",
                    "rc0": "tlm('CFS ES_HK RECEIVED_COUNT') or 0",
                    "sc0": "tlm('CFS SAMPLE_HK RECEIVED_COUNT') or 0",
                },
            ),
            _node(
                "enable", "send_cmd", 1000, label="Telemetry on", command="CFS TO_LAB_ENABLE with DEST_IP '127.0.0.1'"
            ),
            _node(
                "acquire",
                "wait_tlm",
                1200,
                label="Acquire HK",
                condition="(tlm('CFS ES_HK RECEIVED_COUNT') or 0) > rc0 and (tlm('CFS SAMPLE_HK RECEIVED_COUNT') or 0) > sc0",
                timeout=acquire_s,
                poll=0.5,
            ),
            _node(
                "counters",
                "set_var",
                1400,
                label="Counters",
                name="es0",
                expression=f"tlm('{hk}')",
                more={"s0": "tlm('CFS SAMPLE_HK CMD_CNT') or 0", "stage": "'ES no-op acknowledgement'"},
            ),
            _node("es_noop", "send_cmd", 1600, label="ES no-op", command="CFS ES_NOOP"),
            _node(
                "es_ack",
                "wait_tlm",
                1800,
                label="ES counted it",
                condition=f"tlm('{hk}') == (es0 + 1) % 256",
                timeout=ack_s,
                poll=0.5,
            ),
            _node(
                "stage2", "set_var", 2000, label="Next", name="stage", expression="'sample app no-op acknowledgement'"
            ),
            _node("s_noop", "send_cmd", 2200, label="Sample no-op", command="CFS SAMPLE_NOOP"),
            _node(
                "s_ack",
                "wait_tlm",
                2400,
                label="Sample counted it",
                condition="tlm('CFS SAMPLE_HK CMD_CNT') == (s0 + 1) % 256",
                timeout=ack_s,
                poll=0.5,
            ),
            _node("lost", "set_var", 1800, 40, label="Lost: try again", name="lost", expression="stage"),
            _node("ok_now", "set_var", 2600, label="Worked", name="ok", expression="True"),
            _node("result", "if", 800, 320, label="Worked?", condition="ok"),
            _node("retried", "if", 1000, 260, label="First try?", condition="attempt == 1"),
            _node(
                "done",
                "log",
                1200,
                220,
                label="Nominal",
                message="Pass ops done: ES commands {{ tlm('CFS ES_HK CMD_CNT') }}, sample {{ tlm('CFS SAMPLE_HK CMD_CNT') }}, "
                "reset type {{ tlm('CFS ES_HK RESET_TYPE') }}, {{ round(time_to_los()) }}s of contact left",
            ),
            _node(
                "recovered",
                "log",
                1200,
                300,
                label="Recovered",
                message="Pass ops done on the second attempt (the first lost its {{ lost }})",
            ),
            _node(
                "failed",
                "notify",
                1000,
                420,
                label="Failed",
                level="alert",
                title="cFS pass ops failed",
                message="No {{ stage }} in two attempts (pass over {{ contact_station }}, AOS {{ utc(contact_aos) }})",
            ),
            _node("los", "wait_contact", 1400, 320, label="LOS", anchor="los", offset=los_quiet_s),
            _node(
                "quiet",
                "wait_packet",
                1600,
                320,
                label="Telemetry stopped?",
                target="CFS",
                packet="ES_HK",
                count=1,
                timeout=los_quiet_s,
                poll=0.5,
            ),
            _node("los_ok", "log", 1800, 260, label="LOS as predicted", message="Link closed at LOS as predicted"),
            _node(
                "late_los",
                "notify",
                1800,
                380,
                label="Link after LOS",
                message="cFS telemetry still arriving {{ round(-time_to_los()) }}s after the predicted LOS "
                "(the planner and the link disagree)",
            ),
        ],
        "edges": [
            _edge("t", "init"),
            _edge("init", "aos"),
            _edge("aos", "loop"),
            _edge("loop", "next", "body"),
            _edge("next", "enable"),
            _edge("enable", "acquire"),
            _edge("acquire", "counters", "ok"),
            _edge("acquire", "lost", "timeout"),
            _edge("counters", "es_noop"),
            _edge("es_noop", "es_ack"),
            _edge("es_ack", "stage2", "ok"),
            _edge("es_ack", "lost", "timeout"),
            _edge("stage2", "s_noop"),
            _edge("s_noop", "s_ack"),
            _edge("s_ack", "ok_now", "ok"),
            _edge("s_ack", "lost", "timeout"),
            _edge("lost", "loop"),
            _edge("ok_now", "loop"),
            _edge("loop", "result", "done"),
            _edge("result", "retried", "true"),
            _edge("result", "failed", "false"),
            _edge("retried", "done", "true"),
            _edge("retried", "recovered", "false"),
            _edge("done", "los"),
            _edge("recovered", "los"),
            _edge("failed", "los"),
            _edge("los", "quiet"),
            _edge("quiet", "late_los", "received"),
            _edge("quiet", "los_ok", "timeout"),
        ],
    }


def outcome(run):
    """(result, los) of a pass ops run: result nominal / recovered / failed (or the run's status if it got
    nowhere); los ok / late (telemetry after LOS) / None (didn't get there)."""
    reached = {step.get("node") for step in run.get("steps") or []}
    result = next((name for node, name in OUTCOMES if node in reached), run.get("status") or "unknown")
    los = "ok" if "los_ok" in reached else "late" if "late_los" in reached else None
    return result, los


def rule_payload(template_id, satellite_id, station_id):
    return {
        "name": f"{PREFIX}every pass",
        "template_id": template_id,
        "satellite_ids": [satellite_id],
        "station_ids": [station_id],
        "min_elevation": 10,
        "anchor": "aos",
        "offset": -60,
        "mode": "run",
        "stop_at_los": False,  # it checks the link after LOS
        "priority": 10,
        "skip_full_stations": True,
    }


def clear(fa):
    removed = {"rules": 0, "flows": 0, "satellites": 0, "stations": 0}
    for rule in fa.get("/rules") or []:
        if rule["name"].startswith(PREFIX):
            fa.delete(f"/rules/{rule['id']}")
            removed["rules"] += 1
    flows = fa.get("/flows")
    for flow in flows.get("flows", flows) if isinstance(flows, dict) else flows:
        if flow["name"].startswith(PREFIX):
            fa.delete(f"/flows/{flow['id']}")
            removed["flows"] += 1
    schedule = fa.get("/schedule")
    for kind, key in (("satellites", "satellites"), ("stations", "stations")):
        for item in schedule[key]:
            if item["name"].startswith(PREFIX):
                fa.delete(f"/{kind}/{item['id']}")
                removed[kind] += 1
    return removed


def setup(fa):
    print("Removing an earlier setup:", clear(fa))
    sat = fa.post(
        "/satellites",
        {"name": f"{PREFIX}NOAA 19", "catalog": str(NORAD), "target": "CFS", "tle_source": {"kind": "celestrak"}},
    )["satellite"]
    station = fa.post("/stations", {"name": f"{PREFIX}Fairbanks", "capacity": 1, **STATION})["station"]
    fa.post("/tle_sources/fetch", {"kind": "celestrak"})
    flow = fa.post("/flows", template_flow())
    flow = flow.get("flow", flow)
    rule = fa.post("/rules", rule_payload(flow["id"], sat["id"], station["id"]))
    manifest = common.load_json(common.MANIFEST) or {}
    manifest["cfs"] = {
        "satellite_id": sat["id"],
        "station_id": station["id"],
        "template_id": flow["id"],
        "rule_id": rule["rule"]["id"],
    }
    common.save_json(common.MANIFEST, manifest)
    print(f"Rule scheduled {rule['created']} passes (more once NOAA 19's TLE arrives from CelesTrak)")
    return manifest["cfs"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--clear", action="store_true")
    parser.add_argument("--url", default=os.environ.get("COSMOS_URL", "http://localhost:2900"))
    args = parser.parse_args(argv)
    fa = common.FlowAutomator(args.url, password=os.environ.get("COSMOS_PASSWORD"))
    print("Removed:", clear(fa)) if args.clear else setup(fa)
    return 0


if __name__ == "__main__":
    sys.exit(main())
