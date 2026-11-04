"""Regression tests for scheduler fixes from docs/EVALUATION-2026-11-04.md (CON-1, CON-2, CON-13)."""

import pytest
from test_contacts import DC, EPOCH, ISS_1, ISS_2, Clock, template, with_checksum


pytest.importorskip("sgp4")


@pytest.fixture
def clock():
    return Clock(EPOCH)


@pytest.fixture
def planner(engine, clock):
    planner = engine.planner
    planner.clock = clock
    planner.horizon = 2 * 86400
    return planner


def _sat_station(planner):
    return planner.save_satellite({"name": "ISS", "tle": f"ISS\n{ISS_1}\n{ISS_2}"}), planner.save_station(DC)


def test_cancelled_rule_pass_stays_cancelled_after_tle_update(engine, planner, clock):
    sat, _station = _sat_station(planner)
    planner.save_rule({"template_id": template(engine)["id"], "name": "R"})
    victim = min(
        (a for a in planner.activities.values() if a["status"] == "scheduled"), key=lambda a: a["contact"]["aos"]
    )
    planner.delete_activity(victim["id"])
    assert victim["status"] == "cancelled"
    # A routine newer TLE moves TCA slightly, which changes the contact id
    newer = with_checksum(ISS_1[:20] + "24001.60000000" + ISS_1[32:])
    planner.set_tle(sat["id"], f"{newer}\n{ISS_2}", "manual")
    planner._apply_rules(clock())
    back = [
        a
        for a in planner.activities.values()
        if a["status"] == "scheduled" and abs(a["contact"]["tca"] - victim["contact"]["tca"]) < 120
    ]
    assert not back, "an operator-cancelled rule activity came back after a TLE update"


def test_contact_ids_are_stable_when_the_window_slides(planner, clock):
    _sat_station(planner)
    before = {c["id"] for c in planner.contacts if not c["partial"]}
    for k in range(1, 13):
        clock.now = EPOCH + k * 37.3  # slide the prediction window without a TLE change
        planner.recompute()
        after = {c["id"] for c in planner.contacts if not c["partial"]}
        tca = {i: int(i.rsplit("-", 1)[1]) for i in before | after}
        # A pass in both windows keeps its id: no new id within seconds of an old one
        moved = [i for i in after - before if any(abs(tca[i] - tca[j]) < 5 for j in before)]
        assert not moved, f"contact ids changed without a TLE change: {moved}"


def test_load_demo_keeps_a_paused_scheduler_paused(engine, backend):
    from flow_automator.demo import load_demo

    for i in range(1, 5):
        backend.values[f"INST HEALTH_STATUS TEMP{i}"] = 10.0 * i
    for item in ("Q1", "Q2"):
        backend.values[f"INST ADCS {item}"] = 0.5
    backend.values["INST HEALTH_STATUS COLLECTS"] = 3
    engine.stop_all()
    summary = load_demo(engine)
    assert engine.planner.settings["paused"] is True and summary["scheduler_paused"] is True


# CON-4 / GAP-14: a scheduled activity runs the sub-flows it was scheduled with (D11)
def test_activity_runs_pinned_subflows(engine, planner, clock, backend):
    from conftest import edge, make_flow, node, wait_until

    _sat_station(planner)
    leaf, _ = engine.save_flow(
        make_flow([node("t", "trigger_manual"), node("c", "send_cmd", command="INST LEAF_V1")], [edge("t", "c")])
    )
    middle, _ = engine.save_flow(
        make_flow([node("t", "trigger_manual"), node("s", "subflow", flow_id=leaf["id"])], [edge("t", "s")])
    )
    flow = template(
        engine,
        nodes=[node("t", "trigger_manual"), node("s", "subflow", flow_id=middle["id"])],
        edges=[edge("t", "s")],
        parameters=[],
    )
    contact = next(c for c in planner.contacts if not c["partial"] and c["aos"] > clock() + 300)
    activity = planner.create_activity(
        {"template_id": flow["id"], "contact_id": contact["id"], "anchor": "aos", "offset": 0, "mode": "run"}
    )
    assert activity["template_changed"] is False
    # Someone edits the nested sub-flow after the activity was reviewed and scheduled
    engine.save_flow({**leaf, "nodes": [leaf["nodes"][0], node("c", "send_cmd", command="INST LEAF_V2")]}, leaf["id"])
    listed = planner.activity_json(planner.activities[activity["id"]])
    assert listed["template_changed"] is True and listed["subflows_changed"] == [leaf["name"]]

    clock.now = contact["aos"] + 1
    planner.tick()
    assert wait_until(lambda: planner.activities[activity["id"]]["status"] == "done"), planner.activities
    assert backend.commands == ["INST LEAF_V1"]  # the reviewed version, not the edit
    run = engine.get_run(planner.activities[activity["id"]]["run_id"])
    assert run["trigger"]["subflow_versions"] == {leaf["name"]: 1, middle["name"]: 1}

    # A new (or refreshed) activity snapshots the current versions
    clock.now = EPOCH
    second = planner.create_activity(
        {"template_id": flow["id"], "contact_id": contact["id"], "anchor": "aos", "offset": 0, "mode": "run"}
    )
    assert planner.activities[second["id"]]["flow"]["subflow_snapshots"][leaf["id"]]["version"] == 2


def test_manual_runs_use_current_subflows(engine, backend):
    from conftest import edge, make_flow, node, run_to_end

    leaf, _ = engine.save_flow(
        make_flow([node("t", "trigger_manual"), node("c", "send_cmd", command="INST V1")], [edge("t", "c")])
    )
    parent = make_flow([node("t", "trigger_manual"), node("s", "subflow", flow_id=leaf["id"])], [edge("t", "s")])
    engine.save_flow({**leaf, "nodes": [leaf["nodes"][0], node("c", "send_cmd", command="INST V2")]}, leaf["id"])
    assert run_to_end(engine, parent)["status"] == "success"
    assert backend.commands == ["INST V2"]
