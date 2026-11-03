"""A ready-to-demo setup: satellites, ground stations, flows, templates, a rule and scheduled passes.

``load_demo(engine)`` replaces everything named for the demo (flows and rules
starting "Demo: ", satellites DEMOSAT-*, the demo stations) and leaves the
rest alone, so it can be run again at any time to reset the demo. It needs
the COSMOS demo plugin's INST target.

The satellites get TLEs with epoch now, and the "Demo Pass Site" station is
put under DEMOSAT-1's ground track so a real pass starts a few minutes after
loading: the Pass downlink activity the rule schedules into it runs live.
"""

import time

from .orbit import SGP4_IMPORT_ERROR, Propagator, make_tle, parse_tle


PREFIX = "Demo: "
SATELLITES = ("DEMOSAT-1", "DEMOSAT-2")
STATIONS = ("Demo Pass Site", "Wallops", "Svalbard")
SCRIPT = "INST/procedures/flow_demo_measure.py"
SCRIPT_TEXT = """# Flow Automator demo: measure something and hand the result back to the flow
import os

temps = [tlm(f"INST HEALTH_STATUS TEMP{i}") for i in range(1, 5)]
result = {"average": round(sum(temps) / len(temps), 2), "hottest": round(max(temps), 2), "samples": len(temps)}
print(f"Measured {result}")
stash_set(os.environ["FLOW_RESULT_KEY"], result)
"""
FIRST_PASS_IN = 480  # seconds from now to DEMOSAT-1's closest approach over the Demo Pass Site


def _node(node_id, node_type, x, y, **data):
    return {"id": node_id, "type": node_type, "position": {"x": x, "y": y}, "data": data}


def _edge(source, target, handle="out"):
    return {"id": f"{source}-{handle}-{target}", "source": source, "sourceHandle": handle, "target": target}


def demo_flows():
    """The demo's flows, as the engine saves them."""
    return [
        {
            "name": f"{PREFIX}Pass downlink",
            "description": "Template: set up before AOS, start when telemetry arrives, collect, report. "
            "The rule schedules it into every good DEMOSAT-1 pass.",
            "mode": "walk",
            "template": True,
            "parameters": [
                {
                    "name": "collect_type",
                    "type": "choice",
                    "choices": ["NORMAL", "SPECIAL"],
                    "default": "NORMAL",
                    "description": "SPECIAL is hazardous: walk mode asks first",
                },
                {
                    "name": "duration",
                    "type": "number",
                    "default": 2,
                    "min": 0.1,
                    "max": 10,
                    "description": "Collect duration (s)",
                },
                {
                    "name": "sensor",
                    "type": "item",
                    "default": "INST HEALTH_STATUS TEMP1",
                    "description": "Telemetry to report after the collect",
                },
            ],
            "nodes": [
                _node("t", "trigger_manual", 0, 160),
                _node(
                    "pre",
                    "log",
                    260,
                    160,
                    label="Pre-pass setup",
                    message="Pre-pass setup for {{ contact_satellite }} over {{ contact_station }}, "
                    "AOS in {{ round(time_to_aos()) }}s",
                ),
                _node("aos", "wait_contact", 520, 160, label="Wait for AOS", anchor="aos", offset=0),
                _node(
                    "tlm",
                    "wait_packet",
                    780,
                    160,
                    label="First telemetry",
                    target="INST",
                    packet="HEALTH_STATUS",
                    count=1,
                    timeout=30,
                ),
                _node(
                    "cmd",
                    "send_cmd",
                    1040,
                    120,
                    label="Collect",
                    command="INST COLLECT with TYPE '{{ collect_type }}', DURATION {{ duration }}",
                ),
                _node(
                    "chk",
                    "wait_check",
                    1300,
                    120,
                    label="Collect started",
                    condition="INST HEALTH_STATUS COLLECT_TYPE == collect_type",
                    timeout=10,
                ),
                _node("read", "set_var", 1560, 120, label="Read sensor", name="reading", expression="tlm(sensor)"),
                _node("left", "contact_check", 1820, 120, label="Time left?", min_remaining=60),
                _node(
                    "done",
                    "notify",
                    2080,
                    60,
                    label="Report",
                    message="Downlink done for {{ contact_satellite }}: {{ sensor }} = {{ format(reading, '.2f') }}, "
                    "{{ round(time_to_los()) }}s of contact left",
                ),
                _node(
                    "short",
                    "notify",
                    2080,
                    200,
                    label="Running out",
                    level="alert",
                    message="{{ contact_satellite }} pass nearly over after the collect",
                ),
                _node(
                    "none",
                    "notify",
                    1040,
                    300,
                    label="No telemetry",
                    level="alert",
                    message="No INST telemetry within 30s of AOS for {{ contact_satellite }} over {{ contact_station }}",
                ),
            ],
            "edges": [
                _edge("t", "pre"),
                _edge("pre", "aos"),
                _edge("aos", "tlm"),
                _edge("tlm", "cmd", "received"),
                _edge("tlm", "none", "timeout"),
                _edge("cmd", "chk"),
                _edge("chk", "read"),
                _edge("read", "left"),
                _edge("left", "done", "ok"),
                _edge("left", "short", "short"),
            ],
        },
        {
            "name": f"{PREFIX}Health check",
            "description": "Reads a whole packet, computes with functions, branches, and asks before acting.",
            "mode": "walk",
            "nodes": [
                _node("t", "trigger_manual", 0, 120),
                _node(
                    "p",
                    "read_packet",
                    260,
                    120,
                    label="Read HEALTH_STATUS",
                    target="INST",
                    packet="HEALTH_STATUS",
                    variable="hs",
                ),
                _node(
                    "v",
                    "set_var",
                    520,
                    120,
                    label="Summarize",
                    name="avg",
                    expression="round((hs['TEMP1'] + hs['TEMP2'] + hs['TEMP3'] + hs['TEMP4']) / 4, 2)",
                    more={
                        "hottest": "max(hs['TEMP1'], hs['TEMP2'], hs['TEMP3'], hs['TEMP4'])",
                        "summary": "'avg ' + format(avg, '.1f') + ', hottest ' + format(hottest, '.1f')",
                    },
                ),
                _node("i", "if", 780, 120, label="Too hot?", condition="hottest > 30"),
                _node(
                    "c",
                    "send_cmd",
                    1040,
                    40,
                    label="Cooling collect",
                    require_approval=True,
                    command="INST COLLECT with TYPE 'NORMAL', DURATION {{ min(max(round(abs(avg) / 20, 1), 0.1), 10) }}",
                ),
                _node(
                    "n1", "notify", 1300, 40, label="Report hot", message="Health check: {{ summary }}, collect started"
                ),
                _node("n2", "log", 1040, 200, label="All nominal", message="Health check nominal: {{ summary }}"),
            ],
            "edges": [
                _edge("t", "p"),
                _edge("p", "v"),
                _edge("v", "i"),
                _edge("i", "c", "true"),
                _edge("c", "n1"),
                _edge("i", "n2", "false"),
            ],
        },
        {
            "name": f"{PREFIX}Collect with approvals",
            "description": "Crawl mode: every step waits for Go. Includes a hazardous state and a hazardous command.",
            "mode": "crawl",
            "nodes": [
                _node("t", "trigger_manual", 0, 120),
                _node(
                    "a",
                    "send_cmd",
                    260,
                    120,
                    label="Normal collect",
                    command="INST COLLECT with TYPE 'NORMAL', DURATION 1",
                ),
                _node(
                    "w",
                    "wait_check",
                    520,
                    120,
                    label="Collect type is NORMAL",
                    condition="INST HEALTH_STATUS COLLECT_TYPE == 'NORMAL'",
                    timeout=5,
                ),
                _node(
                    "b",
                    "send_cmd",
                    780,
                    120,
                    label="Special collect (hazardous)",
                    command="INST COLLECT with TYPE 'SPECIAL', DURATION 1",
                ),
                _node(
                    "q",
                    "ask",
                    1040,
                    120,
                    label="Clear counters?",
                    prompt="Clear the collect counters now?",
                    kind="confirm",
                    variable="clear",
                    default="no",
                    timeout=0,
                ),
                _node("i", "if", 1300, 120, label="Cleared?", condition="clear"),
                _node("c", "send_cmd", 1560, 60, label="Clear (hazardous)", command="INST CLEAR"),
                _node("l", "log", 1560, 200, label="Kept", message="Counters kept: {{ INST HEALTH_STATUS COLLECTS }}"),
            ],
            "edges": [
                _edge("t", "a"),
                _edge("a", "w"),
                _edge("w", "b"),
                _edge("b", "q"),
                _edge("q", "i", "answered"),
                _edge("i", "c", "true"),
                _edge("i", "l", "false"),
            ],
        },
        {
            "name": f"{PREFIX}Parallel checks",
            "description": "Three branches with their own variables, merged at the Join.",
            "mode": "run",
            "nodes": [
                _node("t", "trigger_manual", 0, 160),
                _node("p", "parallel", 260, 160, isolate=True),
                _node("a", "read_packet", 520, 40, label="Attitude", target="INST", packet="ADCS", variable="adcs"),
                _node(
                    "a2",
                    "set_var",
                    780,
                    40,
                    label="Attitude ok",
                    name="attitude",
                    expression="'q1 ' + format(adcs['Q1'], '.3f')",
                ),
                _node(
                    "b",
                    "set_var",
                    520,
                    160,
                    label="Temperatures",
                    name="temps",
                    expression="[round(INST HEALTH_STATUS TEMP1, 1), round(INST HEALTH_STATUS TEMP2, 1)]",
                ),
                _node("c", "wait_time", 520, 280, label="Settle", seconds=1),
                _node("c2", "set_var", 780, 280, label="Settled", name="settled", expression="True"),
                _node("j", "join", 1040, 160),
                _node(
                    "l",
                    "log",
                    1300,
                    160,
                    label="Summary",
                    message="{{ len(branch_results) }} branches: attitude {{ attitude }}, temps {{ temps }}, "
                    "settled {{ settled }}",
                ),
            ],
            "edges": [
                _edge("t", "p"),
                _edge("p", "a"),
                _edge("p", "b"),
                _edge("p", "c"),
                _edge("a", "a2"),
                _edge("c", "c2"),
                _edge("a2", "j"),
                _edge("b", "j"),
                _edge("c2", "j"),
                _edge("j", "l"),
            ],
        },
        {
            "name": f"{PREFIX}Script result",
            "description": "Runs a Script Runner script that hands a value back with stash_set.",
            "mode": "run",
            "nodes": [
                _node("t", "trigger_manual", 0, 80),
                _node(
                    "s",
                    "run_script",
                    260,
                    80,
                    label="Measure",
                    filename=SCRIPT,
                    wait=True,
                    timeout=60,
                    result_variable="measured",
                ),
                _node(
                    "l",
                    "log",
                    520,
                    80,
                    label="Report",
                    message="Script measured average {{ measured['average'] }}, hottest {{ measured['hottest'] }}",
                ),
            ],
            "edges": [_edge("t", "s"), _edge("s", "l")],
        },
        {
            "name": f"{PREFIX}Hot temperature watch",
            "description": "Armed in dry-run mode: fires on live telemetry and shows what it would do, sends nothing.",
            "mode": "run",
            "dry_run": True,
            "nodes": [
                _node(
                    "t",
                    "trigger_tlm",
                    0,
                    80,
                    label="TEMP1 over 40",
                    condition="INST HEALTH_STATUS TEMP1 > 40",
                    cooldown=60,
                ),
                _node("c", "send_cmd", 260, 80, label="Abort collect", command="INST ABORT"),
                _node(
                    "n",
                    "notify",
                    520,
                    80,
                    label="Tell operators",
                    level="alert",
                    message="TEMP1 is {{ round(INST HEALTH_STATUS TEMP1, 1) }}",
                ),
            ],
            "edges": [_edge("t", "c"), _edge("c", "n")],
        },
    ]


def clear_demo(engine):
    """Remove everything the demo created (and nothing else)."""
    planner = engine.planner
    removed = {"flows": 0, "rules": 0, "satellites": 0, "stations": 0, "activities": 0}
    for rule in list(planner.rules.values()):
        if rule["name"].startswith(PREFIX):
            planner.delete_rule(rule["id"])
            removed["rules"] += 1
    demo_flow_ids = {f["id"] for f in engine.flows.values() if str(f.get("name", "")).startswith(PREFIX)}
    for activity in list(planner.activities.values()):
        if activity["template_id"] in demo_flow_ids or activity["contact"]["satellite"] in SATELLITES:
            planner.activities.pop(activity["id"], None)
            planner.store.delete_record("activities", activity["id"])
            removed["activities"] += 1
    for flow_id in demo_flow_ids:
        engine.delete_flow(flow_id, "demo")
        engine.store.delete_record("deleted_flows", flow_id)  # don't clutter Restore Deleted Flow
        removed["flows"] += 1
    for sat in list(planner.satellites.values()):
        if sat["name"] in SATELLITES:
            planner.delete_satellite(sat["id"])
            removed["satellites"] += 1
    for station in list(planner.stations.values()):
        if station["name"] in STATIONS:
            planner.delete_station(station["id"])
            removed["stations"] += 1
    return removed


def _wait_for_prediction(planner, timeout=60):
    deadline = time.monotonic() + timeout
    while planner.predicting() and time.monotonic() < deadline:
        time.sleep(0.2)


def load_demo(engine, user="demo", now=None):
    """Reset and load the demo. Returns a summary of what's there."""
    if SGP4_IMPORT_ERROR:
        raise RuntimeError(SGP4_IMPORT_ERROR)
    now = time.time() if now is None else now
    planner = engine.planner
    cleared = clear_demo(engine)
    planner.set_paused(False)

    # Satellites with TLEs from now, so their orbits are current
    sat1_tle = make_tle(now, 51.64, 200.0, 10.0, 15.5, catalog=90001, name="DEMOSAT-1")
    sat2_tle = make_tle(now, 97.6, 120.0, 200.0, 15.2, catalog=90002, name="DEMOSAT-2")
    sat1 = planner.save_satellite({"name": "DEMOSAT-1", "target": "INST", "tle": sat1_tle}, user=user)
    sat2 = planner.save_satellite({"name": "DEMOSAT-2", "target": "INST", "tle": sat2_tle}, user=user)

    # A station under DEMOSAT-1's track, so its first pass is minutes away; plus two real sites
    parsed = parse_tle(sat1_tle)
    lat, lon, _alt = Propagator(parsed["line1"], parsed["line2"]).subpoint(now + FIRST_PASS_IN)
    site = planner.save_station(
        {
            "name": "Demo Pass Site",
            "latitude": round(lat, 4),
            "longitude": round(((lon + 1.5 + 180) % 360) - 180, 4),
            "altitude": 0,
            "min_elevation": 10,
            "capacity": 1,
        },
        user=user,
    )
    planner.save_station(
        {
            "name": "Wallops",
            "latitude": 37.9402,
            "longitude": -75.4664,
            "altitude": 10,
            "min_elevation": 5,
            "capacity": 1,
        },
        user=user,
    )
    svalbard = planner.save_station(
        {
            "name": "Svalbard",
            "latitude": 78.2298,
            "longitude": 15.4078,
            "altitude": 500,
            "min_elevation": 5,
            "capacity": 2,
        },
        user=user,
    )

    # Flows
    flows = {}
    for flow in demo_flows():
        saved, problems = engine.save_flow(flow, user=user, note="Loaded by the demo")
        if problems:
            raise RuntimeError(f"Demo flow '{flow['name']}' has problems: {problems[0]['message']}")
        flows[flow["name"][len(PREFIX) :]] = saved
    engine.set_armed(flows["Hot temperature watch"]["id"], True)

    # The script for the Script result flow (best effort: needs the Script Runner)
    script_problem = None
    try:
        engine.backend.put_script(SCRIPT, SCRIPT_TEXT)
    except Exception as error:  # noqa: BLE001
        script_problem = f"Could not save {SCRIPT}: {error}"

    _wait_for_prediction(planner)

    # A rule: the Pass downlink template into every good DEMOSAT-1 pass
    rule = planner.save_rule(
        {
            "name": f"{PREFIX}DEMOSAT-1 downlinks",
            "template_id": flows["Pass downlink"]["id"],
            "satellite_ids": [sat1["id"]],
            "min_elevation": 20,
            "anchor": "aos",
            "offset": -60,
            "mode": "walk",
            "params": {"collect_type": "NORMAL", "duration": 2},
            "priority": 10,
            "stop_at_los": True,
        },
        user=user,
    )

    # A hand-scheduled activity on DEMOSAT-2's next Svalbard pass
    later = next(
        (
            c
            for c in planner.contacts
            if c["satellite_id"] == sat2["id"]
            and c["station_id"] == svalbard["id"]
            and c["aos"] > now + 300
            and not c["partial"]
        ),
        None,
    )
    if later:
        planner.create_activity(
            {
                "template_id": flows["Health check"]["id"],
                "contact_id": later["id"],
                "anchor": "aos",
                "offset": 30,
                "mode": "walk",
                "label": "Health check over Svalbard",
            },
            user=user,
        )

    # Some history to look at
    engine.trigger(
        flows["Health check"]["id"],
        manual=True,
        dry_run=True,
        mode="run",
        skip_waits=True,
        trigger_info={"kind": "manual", "user": "demo"},
        reason="Demo dry run",
    )

    first = next(
        (
            c
            for c in planner.contacts
            if c["station_id"] == site["id"] and c["satellite_id"] == sat1["id"] and c["los"] > now and not c["partial"]
        ),
        None,
    )
    activities = [a for a in planner.activities.values() if a.get("rule_id") == rule["rule"]["id"]]
    summary = {
        "cleared": cleared,
        "flows": len(flows),
        "satellites": 2,
        "stations": 3,
        "rule_activities": rule["created"],
        "first_pass": {
            "station": "Demo Pass Site",
            "aos": first["aos"] if first else None,
            "aos_in": round(first["aos"] - now) if first else None,
            "max_elevation": first["max_elevation"] if first else None,
        },
        "first_activity_start": min((a["contact"]["aos"] - 60 for a in activities), default=None),
        "script_problem": script_problem,
    }
    engine.log("info", f"Flow Automator demo loaded: {summary}")
    return summary
