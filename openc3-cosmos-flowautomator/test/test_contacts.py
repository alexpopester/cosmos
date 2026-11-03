"""Pass prediction, the contact planner and scheduled activities."""

import time

import pytest
from conftest import edge, make_flow, node, wait_until
from flow_automator.contacts import PlannerError
from flow_automator.expressions import Evaluator, ExpressionError
from flow_automator.orbit import OrbitError, Propagator, Station, _checksum, find_passes, parse_tle


pytest.importorskip("sgp4")

ISS_1 = "1 25544U 98067A   24001.50000000  .00016717  00000-0  10270-3 0  9009"
ISS_2 = "2 25544  51.6416 247.4627 0006703 130.5360 325.0288 15.49815395432503"
EPOCH = 1704110400.0  # 2024-01-01 12:00 UTC, the TLE epoch
DC = {"name": "Washington", "latitude": 38.9, "longitude": -77.0, "altitude": 50, "min_elevation": 10}
# First ISS pass over DC after the epoch, cross-checked against skyfield to 0.1s
FIRST_AOS = EPOCH + 15768.8


def with_checksum(line):
    return line[:68] + str(_checksum(line))


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock(EPOCH)


@pytest.fixture
def planner(engine, clock):
    planner = engine.planner
    planner.clock = clock
    planner.horizon = 2 * 86400
    return planner


@pytest.fixture
def setup(planner):
    sat = planner.save_satellite({"name": "ISS", "target": "inst", "tle": f"ISS (ZARYA)\n{ISS_1}\n{ISS_2}"})
    station = planner.save_station(DC)
    return sat, station


def template(engine, nodes=None, edges=None, parameters=None, **extra):
    nodes = nodes or [node("t", "trigger_manual"), node("c", "send_cmd", command="INST COLLECT with TYPE {{ kind }}")]
    edges = edges if edges is not None else [edge("t", "c")]
    if parameters is None:
        parameters = [{"name": "kind", "type": "choice", "choices": ["NORMAL", "SPECIAL"], "default": "NORMAL"}]
    flow, problems = engine.save_flow(
        make_flow(nodes, edges, name="Pass template", template=True, parameters=parameters, **extra)
    )
    assert problems == []
    return flow


def upcoming(planner):
    return [c for c in planner.contacts if not c["partial"] and c["aos"] > planner.clock() + 120]


def first_contact(planner):
    return upcoming(planner)[0]


# ---- orbit ------------------------------------------------------------------


def test_parse_tle_checks_lines():
    tle = parse_tle(f"ISS (ZARYA)\n{ISS_1}\n{ISS_2}")
    assert tle["name"] == "ISS (ZARYA)" and tle["norad_id"] == 25544 and tle["epoch"] == pytest.approx(EPOCH)
    assert parse_tle(ISS_1, ISS_2)["line1"] == ISS_1
    with pytest.raises(OrbitError, match="checksum"):
        parse_tle(ISS_1[:68] + "0", ISS_2)
    with pytest.raises(OrbitError, match="two lines"):
        parse_tle(ISS_1)
    with pytest.raises(OrbitError, match="69 characters"):
        parse_tle(ISS_1[:60], ISS_2)
    other = with_checksum("2 25545" + ISS_2[7:])
    with pytest.raises(OrbitError, match="different satellites"):
        parse_tle(ISS_1, other)


def test_passes_match_reference():
    passes = find_passes(Propagator(ISS_1, ISS_2), Station(38.9, -77.0, 50, 10), EPOCH, EPOCH + 3 * 86400)
    assert len(passes) == 15
    first = passes[0]
    assert first["aos"] == pytest.approx(FIRST_AOS, abs=0.5)
    assert first["max_elevation"] == pytest.approx(19.68, abs=0.05)
    assert first["los"] - first["aos"] == pytest.approx(300.6, abs=1)
    assert [round(p["max_elevation"]) for p in passes[:5]] == [20, 42, 12, 23, 46]
    for found in passes:
        assert found["aos"] < found["tca"] < found["los"] and found["max_elevation"] >= 10


def test_station_under_the_satellite_sees_it_overhead():
    propagator = Propagator(ISS_1, ISS_2)
    moment = EPOCH + 5000
    latitude, longitude, altitude = propagator.subpoint(moment)
    assert 400 < altitude < 450
    passes = find_passes(propagator, Station(latitude, longitude, 0, 0), moment - 900, moment + 900)
    assert len(passes) == 1
    assert passes[0]["tca"] == pytest.approx(moment, abs=2)
    assert passes[0]["max_elevation"] > 89


def test_short_grazing_passes_are_found_between_samples():
    propagator = Propagator(ISS_1, ISS_2)
    station = Station(38.9, -77.0, 50, 10)
    fine = find_passes(propagator, station, EPOCH, EPOCH + 86400, step=2)
    coarse = find_passes(propagator, station, EPOCH, EPOCH + 86400, step=90)
    assert len(coarse) == len(fine)
    for a, b in zip(coarse, fine, strict=True):
        assert a["aos"] == pytest.approx(b["aos"], abs=0.5)


# ---- satellites, stations, contacts -------------------------------------------


def test_contacts_predicted_for_each_pair(planner, setup):
    sat, station = setup
    assert sat["tle"]["norad_id"] == 25544 and sat["target"] == "INST" and not sat["tle_stale"]
    contacts = planner.list_contacts()
    assert len(contacts) >= 9
    assert next(c for c in contacts if c["aos"] > EPOCH)["aos"] == pytest.approx(FIRST_AOS, abs=0.5)
    assert {c["station"] for c in contacts} == {"Washington"}

    planner.save_station({**DC, "name": "Houston", "latitude": 29.7, "longitude": -95.4})
    assert {c["station"] for c in planner.list_contacts()} == {"Washington", "Houston"}
    assert planner.list_contacts(station_id=station["id"])[0]["station_id"] == station["id"]
    planner.save_station({"enabled": False}, station["id"])
    assert {c["station"] for c in planner.list_contacts()} == {"Houston"}


def test_bad_inputs_are_rejected(planner, setup):
    sat, _station = setup
    with pytest.raises(PlannerError, match="checksum"):
        planner.set_tle(sat["id"], ISS_1[:68] + "0\n" + ISS_2)
    with pytest.raises(PlannerError, match="Latitude"):
        planner.save_station({**DC, "latitude": 95})
    with pytest.raises(PlannerError, match="name"):
        planner.save_satellite({"name": " "})
    with pytest.raises(PlannerError, match="Unknown TLE source"):
        planner.save_satellite({"name": "X", "tle_source": {"kind": "carrier-pigeon"}})


def test_satellite_without_tle_or_stale_tle_is_flagged(planner, clock):
    sat = planner.save_satellite({"name": "Later"})
    assert planner.problems[sat["id"]] == "No TLE yet"
    planner.set_tle(sat["id"], f"{ISS_1}\n{ISS_2}")
    clock.now = EPOCH + 5 * 86400
    schedule = planner.schedule()
    assert schedule["satellites"][0]["tle_stale"] is True
    assert schedule["satellites"][0]["tle_age_days"] == pytest.approx(5)


def test_new_tle_keeps_history(planner, setup):
    sat, _station = setup
    newer = with_checksum(ISS_2[:43] + "325.1288" + ISS_2[51:])
    updated = planner.set_tle(sat["id"], f"{ISS_1}\n{newer}", source="manual")
    assert updated["tle"]["line2"] == newer and updated["tle_history"][0]["line2"] == ISS_2


# ---- activities -------------------------------------------------------------


def test_activity_runs_at_its_contact_with_parameters(engine, backend, planner, setup, clock):
    flow = template(engine)
    contact = first_contact(planner)
    activity = planner.create_activity(
        {
            "template_id": flow["id"],
            "contact_id": contact["id"],
            "params": {"kind": "SPECIAL"},
            "anchor": "aos",
            "offset": -60,
            "mode": "run",
        }
    )
    assert activity["status"] == "scheduled" and activity["start"] == pytest.approx(contact["aos"] - 60)
    assert next(c for c in planner.list_contacts() if c["id"] == contact["id"])["activities"] == [activity["id"]]

    clock.now = contact["aos"] - 61
    planner.tick()
    assert backend.commands == []
    clock.now = contact["aos"] - 59
    planner.tick()
    assert wait_until(lambda: planner.activities[activity["id"]]["status"] == "done"), planner.activities
    assert backend.commands == ["INST COLLECT with TYPE SPECIAL"]
    record = planner.activities[activity["id"]]
    run = engine.get_run(record["run_id"])
    assert run["variables"]["contact_aos"] == contact["aos"]
    assert run["variables"]["contact_station"] == "Washington"
    assert run["variables"]["kind"] == "SPECIAL"
    assert run["activity_id"] == activity["id"]
    assert run["flow_id"] == flow["id"]  # history is kept with the template


def test_activity_checks(engine, planner, setup, clock):
    flow = template(engine)
    contact = first_contact(planner)
    base = {"template_id": flow["id"], "contact_id": contact["id"], "mode": "run"}
    with pytest.raises(PlannerError, match="not one of"):
        planner.create_activity({**base, "params": {"kind": "FAST"}})
    with pytest.raises(PlannerError, match="after LOS"):
        planner.create_activity({**base, "anchor": "los", "offset": 5})
    with pytest.raises(PlannerError, match="not predicted"):
        planner.create_activity({**base, "contact_id": "nope"})
    with pytest.raises(PlannerError, match="AOS, TCA, LOS"):
        planner.create_activity({**base, "anchor": "noon"})
    with pytest.raises(PlannerError, match="Mode"):
        planner.create_activity({**base, "mode": "sprint"})
    clock.now = contact["aos"] + 10
    with pytest.raises(PlannerError, match="already passed"):
        planner.create_activity({**base, "anchor": "aos", "offset": 0})

    required = template(engine, parameters=[{"name": "kind", "type": "string", "required": True}])
    with pytest.raises(PlannerError, match="'kind' is required"):
        planner.create_activity({**base, "template_id": required["id"], "anchor": "los", "offset": -30})


def test_late_start_and_missed(engine, backend, planner, setup, clock):
    flow = template(engine)
    contacts = upcoming(planner)
    on_time = planner.create_activity(
        {"template_id": flow["id"], "contact_id": contacts[0]["id"], "late_tolerance": 30}
    )
    late = planner.create_activity({"template_id": flow["id"], "contact_id": contacts[1]["id"], "late_tolerance": 30})
    # The engine was down: it comes back 20s after the first start and 5 minutes after the second
    clock.now = contacts[0]["aos"] + 20
    planner.tick()
    assert wait_until(lambda: planner.activities[on_time["id"]]["status"] == "done")
    assert "late" in planner.activities[on_time["id"]]["message"]
    clock.now = contacts[1]["aos"] + 300
    planner.tick()
    assert planner.activities[late["id"]]["status"] == "missed"
    assert "late tolerance" in planner.activities[late["id"]]["message"]


def test_stop_all_pauses_the_scheduler(engine, backend, planner, setup, clock):
    flow = template(engine)
    contact = first_contact(planner)
    activity = planner.create_activity({"template_id": flow["id"], "contact_id": contact["id"]})
    assert engine.stop_all()["scheduler_paused"] is True
    clock.now = contact["aos"] + 1
    planner.tick()
    assert planner.activities[activity["id"]]["status"] == "scheduled"  # held while within tolerance
    clock.now = contact["aos"] + 60
    planner.tick()
    assert planner.activities[activity["id"]]["status"] == "missed"
    assert "paused" in planner.activities[activity["id"]]["message"]
    assert backend.commands == []
    planner.set_paused(False)
    assert planner.status()["paused"] is False


def test_activity_follows_its_contact_when_the_prediction_moves(engine, planner, setup, clock):
    flow = template(engine)
    contact = first_contact(planner)
    activity = planner.create_activity({"template_id": flow["id"], "contact_id": contact["id"], "offset": -30})
    record = planner.activities[activity["id"]]

    # A new TLE moves the pass slightly: the activity moves with it
    sat_id = contact["satellite_id"]
    nudged = with_checksum(ISS_2[:43] + "325.1288" + ISS_2[51:])  # mean anomaly +0.1 deg ~ 1.5s earlier
    planner.set_tle(sat_id, f"{ISS_1}\n{nudged}")
    assert record["status"] == "scheduled"
    assert record["contact"]["aos"] != contact["aos"]
    assert abs(record["drift"]) == pytest.approx(1.5, abs=0.5)
    assert "Re-timed" in record["message"]

    # A big change (more than the tolerance) needs an operator
    record["contact"] = {**record["contact"], "tca": record["contact"]["tca"] + 600}
    planner.recompute()
    suggested = record["suggested_contact_id"]
    assert record["status"] == "needs_review" and suggested
    accepted = planner.accept_suggestion(activity["id"])
    assert accepted["status"] == "scheduled" and accepted["contact"]["id"] == suggested

    # A contact that disappears (the station is deleted) leaves it for review, and it won't run
    planner.delete_station(contact["station_id"])
    assert record_status(planner, activity) == "needs_review"
    assert "no longer predicted" in planner.activities[activity["id"]]["message"]
    clock.now = planner.activities[activity["id"]]["contact"]["aos"]
    planner.tick()
    assert record_status(planner, activity) == "missed"


def record_status(planner, activity):
    return planner.activities[activity["id"]]["status"]


def test_template_changes_are_flagged_and_can_be_pulled_in(engine, planner, setup):
    flow = template(engine)
    activity = planner.create_activity({"template_id": flow["id"], "contact_id": first_contact(planner)["id"]})
    assert activity["template_changed"] is False
    engine.save_flow({**engine.get_flow(flow["id"]), "description": "edited"}, flow["id"])
    listed = planner.list_activities()[0]
    assert listed["template_changed"] is True
    refreshed = planner.refresh_template(activity["id"])
    assert refreshed["template_changed"] is False
    engine.delete_flow(flow["id"])
    assert planner.list_activities()[0]["template_missing"] is True


def test_overlapping_activities_are_warned_not_blocked(engine, planner, setup):
    flow = template(engine)
    contact = first_contact(planner)
    a = planner.create_activity({"template_id": flow["id"], "contact_id": contact["id"], "offset": 0, "duration": 60})
    b = planner.create_activity({"template_id": flow["id"], "contact_id": contact["id"], "offset": 30, "duration": 60})
    c = planner.create_activity({"template_id": flow["id"], "contact_id": contact["id"], "offset": 200, "duration": 30})
    listed = {x["id"]: x for x in planner.list_activities()}
    assert listed[a["id"]]["overlaps"] == [b["id"]]
    assert listed[b["id"]]["overlaps"] == [a["id"]]
    assert listed[c["id"]]["overlaps"] == []


def test_stop_at_los(engine, planner, setup, clock):
    flow = template(
        engine,
        [node("t", "trigger_manual"), node("w", "wait_time", seconds=30)],
        [edge("t", "w")],
        parameters=[],
    )
    contact = first_contact(planner)
    activity = planner.create_activity({"template_id": flow["id"], "contact_id": contact["id"], "stop_at_los": True})
    clock.now = activity["start"]
    planner.tick()
    assert wait_until(lambda: engine.runs)
    clock.now = contact["los"] + 1
    planner.tick()
    assert wait_until(lambda: record_status(planner, activity) == "stopped")
    assert planner.activities[activity["id"]]["message"] == "Stopped at LOS"


def test_running_activity_fails_if_the_engine_restarts(engine, planner, setup):
    flow = template(engine)
    activity = planner.create_activity({"template_id": flow["id"], "contact_id": first_contact(planner)["id"]})
    planner.activities[activity["id"]]["status"] = "running"
    planner._save("activities", planner.activities[activity["id"]])
    planner.load()
    assert record_status(planner, activity) == "failed"
    assert "restarted" in planner.activities[activity["id"]]["message"]


def test_deleting_a_running_activity_stops_its_run(engine, planner, setup, clock):
    flow = template(engine, [node("t", "trigger_manual"), node("w", "wait_time", seconds=30)], [edge("t", "w")], [])
    activity = planner.create_activity({"template_id": flow["id"], "contact_id": first_contact(planner)["id"]})
    clock.now = activity["start"]
    planner.tick()
    assert wait_until(lambda: engine.runs)
    planner.delete_activity(activity["id"])
    assert wait_until(lambda: not engine.runs)
    assert activity["id"] not in planner.activities


# ---- contact nodes, functions and approval cut-off -----------------------------


def contact_vars(aos_in, los_in):
    now = time.time()
    return {"contact_aos": now + aos_in, "contact_tca": now + (aos_in + los_in) / 2, "contact_los": now + los_in}


def test_wait_contact_and_contact_check(engine, backend):
    flow = template(
        engine,
        [
            node("t", "trigger_manual"),
            node("w", "wait_contact", anchor="aos", offset=0.1),
            node("k", "contact_check", min_remaining=100),
            node("ok", "send_cmd", command="INST OK"),
            node("short", "send_cmd", command="INST SHORT"),
        ],
        [edge("t", "w"), edge("w", "k"), edge("k", "ok", "ok"), edge("k", "short", "short")],
        parameters=[],
    )
    started = time.monotonic()
    run = engine.start_scheduled_run(flow, "test", contact_vars(0.3, 30), "run")
    assert wait_until(lambda: run.status == "success")
    assert time.monotonic() - started >= 0.35
    assert backend.commands == ["INST SHORT"]  # 30s left, it needs 100

    run = engine.start_scheduled_run(flow, "test", contact_vars(-5, 500), "run")
    assert wait_until(lambda: run.status == "success")
    assert backend.commands[-1] == "INST OK"
    assert "ago" in run.steps[1]["message"]


def test_contact_nodes_need_a_contact(engine):
    flow, _ = engine.save_flow(
        make_flow([node("t", "trigger_manual"), node("w", "wait_contact", anchor="los")], [edge("t", "w")])
    )
    result = engine.trigger(flow["id"], manual=True)
    assert wait_until(lambda: result["run"]["id"] not in engine.runs)
    record = engine.get_run(result["run"]["id"])
    assert record["status"] == "failed" and "no contact" in record["message"]


def test_time_to_contact_functions():
    evaluator = Evaluator(contact_vars(10, 100))
    assert evaluator.evaluate("time_to_aos()") == pytest.approx(10, abs=0.1)
    assert evaluator.truthy("time_to_los() > 90")
    with pytest.raises(ExpressionError, match="no contact"):
        Evaluator({}).evaluate("time_to_los()")


def test_approval_cut_off_before_los(engine, backend):
    flow = template(engine, parameters=[{"name": "kind", "type": "string", "default": "NORMAL"}])
    run = engine.start_scheduled_run(
        flow, "test", {"kind": "NORMAL", **contact_vars(0, 10)}, "crawl", approval_deadline=time.time() + 0.3
    )
    assert wait_until(lambda: run.status == "failed", 3)
    assert "cut-off" in run.message
    assert backend.commands == []


# ---- templates ---------------------------------------------------------------


def test_template_parameters_are_validated_and_filled(engine, backend):
    with pytest.raises(AssertionError):
        template(engine, parameters=[{"name": "bad name"}])
    _flow, problems = engine.save_flow(
        make_flow(
            [node("t", "trigger_tlm", condition="1 > 0")],
            [],
            template=True,
            parameters=[{"name": "n", "type": "number", "default": "x"}, {"name": "contact_aos"}],
        )
    )
    messages = " ".join(p["message"] for p in problems)
    assert "Manual trigger" in messages and "not a number" in messages and "scheduler" in messages

    flow = template(engine)
    result = engine.trigger(flow["id"], manual=True)  # the default fills in
    assert wait_until(lambda: backend.commands == ["INST COLLECT with TYPE NORMAL"])
    assert result["result"] == "started"
    with pytest.raises(Exception, match="not one of"):
        engine.trigger(flow["id"], manual=True, variables={"kind": "FAST"})
    listed = next(f for f in engine.list_flows() if f["id"] == flow["id"])
    assert listed["template"] is True and listed["parameters"][0]["name"] == "kind"


def test_manual_run_of_a_template_explains_it_has_no_contact(engine):
    flow = template(
        engine,
        [
            node("t", "trigger_manual"),
            node("l", "log", message="AOS in {{ round(time_to_aos()) }}s at {{ contact_station }}"),
        ],
        [edge("t", "l")],
        parameters=[],
    )
    result = engine.trigger(flow["id"], manual=True)
    assert wait_until(lambda: result["run"]["id"] not in engine.runs)
    record = engine.get_run(result["run"]["id"])
    assert record["status"] == "failed" and "has no contact" in record["message"]
    assert record["variables"]["contact_station"] is None


# ---- HTTP -------------------------------------------------------------------


def test_contacts_http_api(engine, planner, clock):
    from flow_automator.http_api import FlowApi

    api = FlowApi(engine, lambda token, write: None)

    def call(method, path, body=None):
        import json

        return api.handle(method, f"/flow-automator/api{path}", {}, {}, json.dumps(body) if body else "")

    status, body = call("POST", "/satellites", {"name": "ISS", "tle": f"{ISS_1}\n{ISS_2}"})
    assert status == 200
    sat_id = body["satellite"]["id"]
    assert call("POST", "/satellites", {"name": "Bad", "tle": "1 nope"})[0] == 400
    status, body = call("POST", "/stations", DC)
    assert status == 200
    status, schedule = call("GET", "/schedule")
    assert status == 200 and schedule["contacts"] and schedule["status"]["satellites"] == 1
    contact = next(c for c in schedule["contacts"] if not c["partial"])

    flow = template(engine)
    status, body = call("POST", "/activities", {"template_id": flow["id"], "contact_id": contact["id"]})
    assert status == 200, body
    activity_id = body["activity"]["id"]
    assert call("PUT", f"/activities/{activity_id}", {"offset": -45})[1]["activity"]["offset"] == -45
    assert call("POST", f"/activities/{activity_id}/accept")[0] == 400
    assert call("POST", "/activities", {"template_id": "missing", "contact_id": contact["id"]})[0] == 404
    assert call("POST", f"/satellites/{sat_id}/tle", {"tle": f"{ISS_1}\n{ISS_2}"})[0] == 200
    assert call("POST", "/scheduler/pause", {})[1]["paused"] is True
    assert call("POST", "/scheduler/resume", {})[1]["paused"] is False
    assert call("POST", "/contacts/recompute", {})[1]["contacts"] > 0
    assert call("DELETE", f"/activities/{activity_id}")[0] == 200
    assert call("DELETE", f"/satellites/{sat_id}")[0] == 200
    assert call("GET", "/activities")[1] == []


# ---- background prediction, retention ------------------------------------------


def test_prediction_runs_in_the_background_one_satellite_at_a_time(engine, clock):
    planner = engine.planner
    planner.clock = clock
    planner.horizon = 2 * 86400
    planner.start()
    try:
        for i in range(5):
            planner.save_station({**DC, "name": f"Site {i}", "longitude": -77.0 + 10 * i})
        started = time.monotonic()
        sats = [planner.save_satellite({"name": f"SAT-{i}", "tle": f"{ISS_1}\n{ISS_2}"}) for i in range(10)]
        assert time.monotonic() - started < 1.0  # saving never waits for prediction
        assert wait_until(lambda: planner.predicting() == 0, 30)
        assert len(planner.pair_contacts) == 50  # nothing still in flight once predicting() says 0
        assert planner.status()["predicting"] == 0 and planner.computed_at is not None
        assert {c["satellite"] for c in planner.contacts} == {f"SAT-{i}" for i in range(10)}

        # A new TLE for one satellite only re-predicts that satellite
        before = {key: id(pair) for key, pair in planner.pair_contacts.items()}
        planner.set_tle(sats[3]["id"], f"{ISS_1}\n{with_checksum(ISS_2[:43] + '325.1288' + ISS_2[51:])}")
        assert wait_until(
            lambda: (
                not planner.pending
                and id(planner.pair_contacts[(sats[3]["id"], next(iter(planner.stations)))])
                != before[(sats[3]["id"], next(iter(planner.stations)))]
            ),
            10,
        )
        unchanged = [k for k in before if k[0] != sats[3]["id"] and id(planner.pair_contacts[k]) == before[k]]
        assert len(unchanged) == 45
    finally:
        planner.stop()


def test_finished_activities_are_pruned_after_retention(engine, planner, setup, clock):
    flow = template(engine)
    contact = first_contact(planner)
    activity = planner.create_activity({"template_id": flow["id"], "contact_id": contact["id"]})
    clock.now = contact["aos"] + 600
    planner.tick()
    assert record_status(planner, activity) == "missed"
    clock.now = contact["los"] + 6 * 86400
    assert planner.prune() == 0
    clock.now = contact["los"] + 8 * 86400
    assert planner.prune() == 1
    assert activity["id"] not in planner.activities
    assert activity["id"] not in planner.store.records("activities")


def test_scheduler_events_are_audited(engine, planner, setup, clock):
    flow = template(engine)
    contact = first_contact(planner)
    activity = planner.create_activity({"template_id": flow["id"], "contact_id": contact["id"]})
    clock.now = activity["start"]
    planner.tick()
    assert wait_until(lambda: record_status(planner, activity) == "done")
    actions = [(e["user"], e["action"]) for e in engine.audit.entries(kind="activity")]
    assert ("scheduler", "started") in actions and ("scheduler", "done") in actions


# ---- recurring rules ---------------------------------------------------------


def test_rule_schedules_every_matching_contact(engine, planner, setup, clock):
    sat, station = setup
    flow = template(engine)
    result = planner.save_rule(
        {
            "name": "Every good pass",
            "template_id": flow["id"],
            "satellite_ids": [sat["id"]],
            "min_elevation": 30,
            "anchor": "aos",
            "offset": -60,
            "params": {"kind": "SPECIAL"},
        },
        user="alice",
    )
    good = [c for c in upcoming(planner) if c["max_elevation"] >= 30]
    assert result["created"] == len(good) > 0
    made = [a for a in planner.activities.values() if a.get("rule_id") == result["rule"]["id"]]
    assert {a["contact"]["id"] for a in made} == {c["id"] for c in good}
    assert all(a["label"] == "Every good pass" and a["params"] == {"kind": "SPECIAL"} for a in made)
    assert made[0]["created_by"] == "rule:Every good pass"
    assert planner.list_rules()[0]["scheduled"] == len(good)

    # Applying again creates nothing new
    assert planner._apply_rules(clock.now) == 0
    # New contacts (a second station) get activities too
    planner.save_station({**DC, "name": "Houston", "latitude": 29.7, "longitude": -95.4})
    assert len([a for a in planner.activities.values() if a.get("rule_id")]) > len(good)


def test_deleting_a_rule_activity_cancels_it_so_the_rule_leaves_it(engine, planner, setup, clock):
    flow = template(engine)
    rule = planner.save_rule({"template_id": flow["id"], "min_elevation": 30})["rule"]
    victim = next(a for a in planner.activities.values() if a.get("rule_id") == rule["id"])
    planner.delete_activity(victim["id"], user="alice")
    assert victim["status"] == "cancelled" and "alice" in victim["message"]
    assert planner._apply_rules(clock.now) == 0
    assert len([a for a in planner.activities.values() if a["contact"]["id"] == victim["contact"]["id"]]) == 1


def test_editing_or_deleting_a_rule_replans_its_activities(engine, planner, setup, clock):
    flow = template(engine)
    rule = planner.save_rule({"template_id": flow["id"], "min_elevation": 10})["rule"]
    many = planner.list_rules()[0]["scheduled"]
    result = planner.save_rule({"min_elevation": 40}, rule["id"])
    assert result["removed"] == many and 0 < result["created"] < many
    assert all(a["contact"]["max_elevation"] >= 40 for a in planner.activities.values() if a["status"] == "scheduled")
    planner.save_rule({"enabled": False}, rule["id"])
    assert not [a for a in planner.activities.values() if a["status"] == "scheduled"]
    planner.save_rule({"enabled": True}, rule["id"])
    _rule, removed = planner.delete_rule(rule["id"])
    assert removed == result["created"] and not planner.activities


def test_rule_follows_a_pass_that_moves_too_far(engine, planner, setup, clock):
    flow = template(engine)
    rule = planner.save_rule({"template_id": flow["id"], "min_elevation": 10})["rule"]
    activity = next(a for a in planner.activities.values() if a.get("rule_id") == rule["id"])
    real_id = activity["contact"]["id"]
    # As if this activity was planned on an older prediction that put the pass 10 minutes later
    activity["contact"] = {**activity["contact"], "id": "old-prediction", "tca": activity["contact"]["tca"] + 600}
    planner.recompute()
    assert activity["status"] == "cancelled" and activity.get("superseded")
    replacement = [
        a for a in planner.activities.values() if a["status"] == "scheduled" and a["contact"]["id"] == real_id
    ]
    assert len(replacement) == 1 and replacement[0]["rule_id"] == rule["id"]
    assert activity["id"] not in [a["id"] for a in planner.list_activities()]  # superseded ones are hidden


def test_rule_with_problems_reports_them(engine, planner, setup):
    flow = template(engine, parameters=[{"name": "kind", "type": "string", "required": True}])
    with pytest.raises(PlannerError, match="'kind' is required"):
        planner.save_rule({"template_id": flow["id"]})
    rule = planner.save_rule({"template_id": flow["id"], "params": {"kind": "X"}})["rule"]
    engine.delete_flow(flow["id"])
    planner._apply_rules(planner.clock())
    assert planner.rules[rule["id"]]["last_error"] == "Its template was deleted"


# ---- station capacity --------------------------------------------------------


def two_satellites(planner):
    """A second satellite a few seconds behind the first, so their passes over DC overlap."""
    planner.save_satellite({"name": "ISS-B", "tle": f"{ISS_1}\n{with_checksum(ISS_2[:43] + '324.0288' + ISS_2[51:])}"})
    return list(planner.satellites.values())


def overlapping_pair(planner):
    for a in upcoming(planner):
        for b in upcoming(planner):
            if (
                a["satellite_id"] != b["satellite_id"]
                and a["station_id"] == b["station_id"]
                and a["aos"] < b["los"]
                and b["aos"] < a["los"]
            ):
                return a, b
    raise AssertionError("no overlapping passes")


def test_station_capacity_conflicts(engine, planner, setup):
    two_satellites(planner)
    a, b = overlapping_pair(planner)
    flow = template(engine)
    first = planner.create_activity({"template_id": flow["id"], "contact_id": a["id"]})
    assert first["conflicts"] == []
    listed = {c["id"]: c for c in planner.list_contacts()}
    assert listed[b["id"]]["station_full"] is True and listed[a["id"]]["station_full"] is False
    planner.create_activity({"template_id": flow["id"], "contact_id": b["id"]})  # allowed, but flagged
    activities = planner.list_activities()
    assert all(x["conflicts"] for x in activities)
    assert planner.status()["conflicts"] == 2

    station = planner.stations[a["station_id"]]
    planner.save_station({"capacity": 2}, station["id"])
    assert not any(x["conflicts"] for x in planner.list_activities())


def test_rules_skip_full_stations_by_priority(engine, planner, setup):
    sats = two_satellites(planner)
    flow = template(engine)
    high = planner.save_rule(
        {"name": "A first", "template_id": flow["id"], "satellite_ids": [sats[0]["id"]], "priority": 10}
    )
    low = planner.save_rule({"name": "B second", "template_id": flow["id"], "satellite_ids": [sats[1]["id"]]})
    assert high["created"] > 0 and planner.rules[low["rule"]["id"]]["skipped_full"] > 0
    assert not any(x["conflicts"] for x in planner.list_activities())
    planner.save_rule({"skip_full_stations": False}, low["rule"]["id"])
    assert any(x["conflicts"] for x in planner.list_activities())


def test_rules_http_api(engine, planner, setup):
    from flow_automator.http_api import FlowApi

    api = FlowApi(engine, lambda token, write: {"username": "alice"})

    def call(method, path, body=None):
        import json

        return api.handle(method, f"/flow-automator/api{path}", {}, {}, json.dumps(body) if body else "")

    flow = template(engine)
    status, body = call("POST", "/rules", {"template_id": flow["id"], "min_elevation": 30})
    assert status == 200 and body["created"] > 0
    rule_id = body["rule"]["id"]
    assert call("GET", "/rules")[1][0]["id"] == rule_id
    assert call("PUT", f"/rules/{rule_id}", {"enabled": False})[1]["removed"] == body["created"]
    assert call("POST", "/rules", {"template_id": flow["id"], "satellite_ids": ["nope"]})[0] == 404
    assert call("DELETE", f"/rules/{rule_id}")[0] == 200
    actions = [e["action"] for e in engine.audit.entries(kind="rule")]
    assert actions == ["deleted", "saved", "created"]
    assert call("GET", "/schedule")[1]["rules"] == []
