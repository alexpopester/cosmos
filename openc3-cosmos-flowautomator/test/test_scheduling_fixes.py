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


# ---- section 2.2 scheduler fixes ---------------------------------------------------------

from flow_automator.orbit import make_tle  # noqa: E402


DC5 = {"name": "DC", "latitude": 38.9, "longitude": -77.0, "altitude": 0, "min_elevation": 5}


def _two_sats_one_antenna(planner):
    a = planner.save_satellite({"name": "A", "tle": make_tle(EPOCH, 51.6, 200, 10.0, catalog=90101)})
    b = planner.save_satellite({"name": "B", "tle": make_tle(EPOCH, 51.6, 200, 11.0, catalog=90102)})
    planner.save_station({**DC5, "capacity": 1})
    return a, b


# CON-6: a higher-priority rule takes the antenna from lower-priority rules, even when added later
def test_higher_priority_rule_preempts(engine, planner):
    a, b = _two_sats_one_antenna(planner)
    flow = template(engine)
    low = planner.save_rule({"template_id": flow["id"], "name": "low", "priority": 0, "satellite_ids": [a["id"]]})
    assert low["created"] > 0
    high = planner.save_rule({"template_id": flow["id"], "name": "high", "priority": 10, "satellite_ids": [b["id"]]})
    assert high["created"] > 0 and planner.rules[high["rule"]["id"]]["skipped_full"] == 0


# CON-17: renaming a rule keeps its activities; activities edited by hand survive a re-plan
def test_rule_rename_keeps_activities_and_edits_survive(engine, planner):
    _sat_station(planner)
    flow = template(engine)
    rule = planner.save_rule({"template_id": flow["id"], "name": "R"})["rule"]
    before = {a["id"] for a in planner.activities.values() if a.get("rule_id") == rule["id"]}
    renamed = planner.save_rule({"name": "Renamed"}, rule["id"])
    assert renamed["removed"] == 0
    after = [a for a in planner.activities.values() if a.get("rule_id") == rule["id"]]
    assert {a["id"] for a in after} == before and all(a["label"] == "Renamed" for a in after)
    edited = min(after, key=lambda a: a["contact"]["aos"])
    planner.update_activity(edited["id"], {"offset": -30})
    planner.save_rule({"offset": 60}, rule["id"])  # matching changed: the rule re-plans
    kept = planner.activities[edited["id"]]
    assert kept["offset"] == -30 and kept["detached"] is True
    # It still holds that pass for the rule: no duplicate activity on the same contact
    same = [
        x
        for x in planner.activities.values()
        if x.get("rule_id") == rule["id"] and x["contact"]["id"] == kept["contact"]["id"] and x["status"] == "scheduled"
    ]
    assert len(same) == 1


# CON-5: a satellite that never sets gets daily contacts, which rules can schedule
def test_geo_gets_daily_contacts(engine, planner):
    from flow_automator.orbit import Propagator, parse_tle

    geo = make_tle(EPOCH, 0.05, 0.0, 0.0, 1.00273791, eccentricity=0.0001, catalog=90010)
    lines = parse_tle(geo)
    _lat, lon, _alt = Propagator(lines["line1"], lines["line2"]).subpoint(EPOCH)
    planner.save_satellite({"name": "GEO", "tle": geo})
    planner.save_station(
        {"name": "Under", "latitude": 0.0, "longitude": round(lon, 2), "altitude": 0, "min_elevation": 5}
    )
    full = [c for c in planner.contacts if not c["partial"]]
    assert full and all(c["continuous"] and c["duration"] == 86400 and c["aos"] % 86400 == 0 for c in full)
    created = planner.save_rule(
        {"template_id": template(engine)["id"], "name": "Daily", "anchor": "aos", "offset": 3600}
    )
    assert created["created"] >= 1


# CON-10: approvals can't close before the activity starts
def test_approval_margin_must_leave_time_to_approve(engine, planner, clock):
    from flow_automator.contacts import PlannerError

    _sat_station(planner)
    flow = template(engine)
    contact = next(c for c in planner.contacts if not c["partial"] and c["aos"] > clock() + 300)
    with pytest.raises(PlannerError, match="Approvals close"):
        planner.create_activity(
            {
                "template_id": flow["id"],
                "contact_id": contact["id"],
                "anchor": "los",
                "offset": -20,
                "mode": "walk",
                "approval_margin": 600,
            }
        )
    # In run mode nothing waits for approval, so it's fine
    planner.create_activity(
        {
            "template_id": flow["id"],
            "contact_id": contact["id"],
            "anchor": "los",
            "offset": -20,
            "mode": "run",
            "approval_margin": 600,
        }
    )


# CON-11: a TLE SGP4 can't propagate is reported, not just silent
def test_decayed_tle_is_reported(planner):
    decayed = make_tle(EPOCH, 51.6, 200, 10.0, mean_motion=16.9, eccentricity=0.0, catalog=90200)
    lines = decayed.split("\n")
    # A huge drag term makes the orbit decay within the window
    line1 = lines[0][:53] + "99999-0" + lines[0][60:]
    line1 = line1[:68] + str(sum(int(c) if c.isdigit() else (1 if c == "-" else 0) for c in line1[:68]) % 10)
    sat = planner.save_satellite({"name": "Doomed", "tle": f"{line1}\n{lines[1]}"})
    planner.save_station(DC5)
    problem = planner.problems.get(sat["id"]) or ""
    assert problem.startswith("Prediction failed: SGP4"), problem


# CON-7: many activities missed in one tick give one alert, and a dead webhook is skipped for a while
def test_missed_activities_alert_together(engine, planner, clock):
    _sat_station(planner)
    flow = template(engine)
    contacts = [c for c in planner.contacts if not c["partial"] and c["aos"] > clock() + 300][:5]
    for contact in contacts:
        planner.create_activity(
            {"template_id": flow["id"], "contact_id": contact["id"], "anchor": "aos", "offset": 0, "mode": "run"}
        )
    sent = engine.alerts.sent[:]
    clock.now = contacts[-1]["los"] + 10  # the engine was down through all of them
    planner.tick()
    new = [r for r in engine.alerts.sent if r not in sent and r["event"] == "activity_missed"]
    assert len(new) == 1 and new[0]["title"] == "5 activities missed"


def test_failing_webhook_is_suspended(engine):
    from flow_automator import alerts as alerts_module

    calls = []

    def post(url, payload):
        calls.append(url)
        raise RuntimeError("HTTP 503")

    alerter = alerts_module.Alerter(engine.store, post=post, clock=lambda: 1000.0)
    alerter.settings["webhooks"] = [
        {
            "id": "h",
            "name": "dead",
            "url": "http://10.0.0.9/x",
            "format": "generic",
            "events": ["run_failed"],
            "enabled": True,
        }
    ]
    for i in range(6):
        alerter.emit("run_failed", f"failed {i}", "", key=f"k{i}")
    assert len(calls) == alerts_module.SUSPEND_AFTER_FAILURES
    assert alerter.status["h"]["skipped"] == 6 - alerts_module.SUSPEND_AFTER_FAILURES


# CON-8: re-ingesting the TLE already in use isn't audited
def test_unchanged_tle_ingest_is_not_audited(engine, planner):
    sat, _ = _sat_station(planner)
    before = len(engine.audit.entries())
    planner.intake.ingest(f"ISS\n{ISS_1}\n{ISS_2}", "tlm:GND ORBIT", user="tlm:GND ORBIT")
    assert len(engine.audit.entries()) == before


# CON-9: clearing the demo leaves a user's own stations alone, even with a name earlier demos used
def test_clear_demo_keeps_user_stations(engine, planner):
    from flow_automator.demo import clear_demo

    mine = planner.save_station({**DC5, "name": "Wallops"}, user="alice")
    old_demo = planner.save_station({**DC5, "name": "Svalbard"}, user="demo")
    clear_demo(engine)
    assert mine["id"] in planner.stations and old_demo["id"] not in planner.stations
