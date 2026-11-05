"""Rule planning fixes from docs/EVALUATION-2026-11-05.md (backlog B15, B19)."""

import pytest
from conftest import edge, make_flow, node
from test_contacts import DC, EPOCH, Clock


pytest.importorskip("sgp4")

from flow_automator.contacts import PlannerError  # noqa: E402
from flow_automator.orbit import Propagator, make_tle, parse_tle  # noqa: E402


HOUR = 3600


@pytest.fixture
def clock():
    return Clock(EPOCH)


@pytest.fixture
def planner(engine, clock):
    planner = engine.planner
    planner.clock = clock
    planner.horizon = 86400
    return planner


def tmpl(engine, name="T", mode="run"):
    flow, problems = engine.save_flow(
        make_flow(
            [node("t", "trigger_manual"), node("c", "send_cmd", command="INST NOOP")],
            [edge("t", "c")],
            name=name,
            template=True,
            mode=mode,
        )
    )
    assert problems == []
    return flow


def iss_like(catalog, mean_anomaly, epoch=EPOCH):
    return make_tle(epoch, 51.6416, 247.4627, mean_anomaly, mean_motion=15.49815395, catalog=catalog)


def overlapping_pairs(planner, a_id, b_id, station_id, after):
    ca = [c for c in planner.contacts if c["satellite_id"] == a_id and c["station_id"] == station_id]
    cb = [c for c in planner.contacts if c["satellite_id"] == b_id and c["station_id"] == station_id]
    return [
        (x, y)
        for x in ca
        for y in cb
        if x["aos"] < y["los"] and y["aos"] < x["los"] and x["aos"] > after and not x["partial"] and not y["partial"]
    ]


# B19 (C-2, CON-6): the low-priority rule won every new overlap after the prediction window rolled
def test_rule_priority_holds_after_the_window_rolls(engine, planner, clock):
    station = planner.save_station(DC)
    low_sat = planner.save_satellite({"name": "LOWSAT", "tle": iss_like(90001, 325.0)})
    high_sat = planner.save_satellite({"name": "HIGHSAT", "tle": iss_like(90002, 324.0)})
    t = tmpl(engine)
    planner.save_rule({"name": "high", "template_id": t["id"], "priority": 10, "satellite_ids": [high_sat["id"]]})
    planner.save_rule({"name": "low", "template_id": t["id"], "priority": 0, "satellite_ids": [low_sat["id"]]})

    def winners(after):
        result = []
        for a, b in overlapping_pairs(planner, low_sat["id"], high_sat["id"], station["id"], after):
            acts = [
                x
                for x in planner.activities.values()
                if x["status"] == "scheduled" and x["contact"]["id"] in (a["id"], b["id"])
            ]
            result.append(sorted(planner.rules[x["rule_id"]]["name"] for x in acts))
        return result

    first = winners(clock.now + 60)
    assert first and all(w == ["high"] for w in first)
    horizon_end = clock.now + planner.horizon
    clock.now += 12 * HOUR
    planner.recompute()
    later = winners(horizon_end)
    assert later and all(w == ["high"] for w in later), later


# B15a (C-8, R-17): saving a rule used to delete every lower-priority rule activity, imminent ones included
def test_saving_an_unrelated_rule_leaves_other_rules_activities_alone(engine, planner, clock):
    planner.save_station(DC)
    sat = planner.save_satellite({"name": "ISS", "tle": iss_like(90001, 325.0)})
    other = planner.save_satellite({"name": "OTHER", "tle": iss_like(90003, 100.0)})
    t = tmpl(engine)
    planner.save_rule({"name": "low", "template_id": t["id"], "priority": 0, "satellite_ids": [sat["id"]]})
    before = {a["id"]: a["flow"] for a in planner.activities.values() if a["status"] == "scheduled"}
    first = min((a for a in planner.activities.values()), key=lambda a: a["contact"]["aos"])
    clock.now = first["contact"]["aos"] - 5  # about to start
    planner.save_rule(
        {
            "name": "high",
            "template_id": t["id"],
            "priority": 5,
            "satellite_ids": [other["id"]],
            "skip_full_stations": False,
        }
    )
    assert first["id"] in planner.activities
    for activity_id, flow in before.items():
        assert planner.activities[activity_id]["flow"] == flow  # same snapshot, same id


def test_a_higher_priority_rule_never_pre_empts_an_imminent_activity(engine, planner, clock):
    station = planner.save_station(DC)
    low_sat = planner.save_satellite({"name": "LOWSAT", "tle": iss_like(90001, 325.0)})
    high_sat = planner.save_satellite({"name": "HIGHSAT", "tle": iss_like(90002, 324.0)})
    t = tmpl(engine)
    planner.save_rule({"name": "low", "template_id": t["id"], "priority": 0, "satellite_ids": [low_sat["id"]]})
    a, _b = overlapping_pairs(planner, low_sat["id"], high_sat["id"], station["id"], clock.now + 60)[0]
    held = next(x for x in planner.activities.values() if x["contact"]["id"] == a["id"])
    clock.now = held["contact"]["aos"] - 60  # a minute before it starts
    planner.save_rule({"name": "high", "template_id": t["id"], "priority": 10, "satellite_ids": [high_sat["id"]]})
    assert planner.activities[held["id"]]["status"] == "scheduled"
    pre_empted = [e for e in engine.audit.entries() if e.get("action") == "pre-empted"]
    assert held["id"] not in {e.get("target_id") for e in pre_empted}


# B15b (C-4, CON-10): rules created crawl/walk activities that started after approvals had closed
def test_rule_anchored_inside_the_approval_cutoff_is_refused(engine, planner):
    planner.save_station(DC)
    planner.save_satellite({"name": "ISS", "tle": iss_like(90001, 325.0)})
    t = tmpl(engine, mode="crawl")
    with pytest.raises(PlannerError, match="Approvals close"):
        planner.save_rule({"name": "late", "template_id": t["id"], "anchor": "los", "offset": -10, "mode": "crawl"})


def test_rule_skips_passes_too_short_for_its_approvals(engine, planner):
    planner.save_station(DC)
    planner.save_satellite({"name": "ISS", "tle": iss_like(90001, 325.0)})
    t = tmpl(engine, mode="walk")
    planner.save_rule({"name": "late", "template_id": t["id"], "anchor": "aos", "offset": 400, "approval_margin": 120})
    for activity in planner.activities.values():
        start = activity["contact"]["aos"] + 400
        assert start < activity["contact"]["los"] - 120


# B15c (R-4): a GEO activity was cancelled at the next recompute on its own day
def test_geo_activity_survives_a_recompute_later_the_same_day(engine, planner, clock):
    geo = make_tle(EPOCH, 0.05, 0.0, 0.0, 1.00273791, eccentricity=0.0001, catalog=90010)
    lines = parse_tle(geo)
    _lat, lon, _alt = Propagator(lines["line1"], lines["line2"]).subpoint(EPOCH)
    planner.horizon = 2 * 86400
    planner.save_satellite({"name": "GEO", "tle": geo})
    planner.save_station(
        {"name": "Under", "latitude": 0.0, "longitude": round(lon, 2), "altitude": 0, "min_elevation": 5}
    )
    planner.save_rule(
        {"template_id": tmpl(engine)["id"], "name": "Daily", "anchor": "aos", "offset": 20 * HOUR, "duration": 600}
    )
    first = min(
        (a for a in planner.activities.values() if a["status"] == "scheduled"), key=lambda a: a["contact"]["aos"]
    )
    clock.now = first["contact"]["aos"] + 12 * HOUR
    planner.recompute()
    assert first["status"] == "scheduled", first.get("message")


# B18 (C-11, P-1): applying rules scanned every taken pass for every contact, holding the planner lock for
# seconds to minutes at realistic sizes. Indexed now; this guards against a return of the quadratic scan.
def test_applying_rules_with_nothing_new_is_fast(engine, planner):
    import time

    planner.horizon = 3 * 86400
    t = tmpl(engine)
    for i in range(4):
        planner.save_station(
            {**DC, "name": f"GS{i}", "latitude": -40 + 25 * i, "longitude": -150 + 80 * i, "capacity": 4}
        )
    for i in range(6):
        planner.save_satellite(
            {"name": f"SAT{i}", "tle": make_tle(EPOCH, 97.5, 30 * i, 40 * i, mean_motion=15.2, catalog=91000 + i)}
        )
    for k in range(3):
        planner.save_rule({"name": f"r{k}", "template_id": t["id"], "priority": k, "skip_full_stations": False})
    assert len(planner.activities) > 300
    started = time.perf_counter()
    with planner.lock:
        assert planner._apply_rules(planner.clock()) == 0
    assert time.perf_counter() - started < 0.1


# B20 (C-3, CON-5): one short GEO activity made its station "full" for the whole 24 h day
def _geo_tle():
    from flow_automator.orbit import Station

    for mean_anomaly in range(0, 360, 10):
        tle = make_tle(EPOCH, 0.05, 0.0, mean_anomaly, mean_motion=1.00273791, eccentricity=0.0001, catalog=90010)
        line1, line2 = tle.split("\n")[-2:]
        position = Propagator(line1, line2).ecef([EPOCH])[0]
        if Station(DC["latitude"], DC["longitude"], DC["altitude"], 10).look(position)[1][0] > 30:
            return tle
    raise AssertionError("no GEO slot visible from the station")


def test_a_short_geo_activity_leaves_the_rest_of_the_day_free(engine, planner):
    planner.horizon = 3 * 86400
    planner.save_station(DC)  # capacity 1
    geo = planner.save_satellite({"name": "GEO", "tle": _geo_tle()})
    iss = planner.save_satellite({"name": "ISS", "tle": iss_like(90001, 325.0)})
    day = next(
        c
        for c in planner.contacts
        if c["satellite_id"] == geo["id"] and not c["partial"] and c["aos"] > planner.clock()
    )
    t = tmpl(engine)
    with pytest.raises(PlannerError, match="duration"):
        planner.create_activity({"contact_id": day["id"], "template_id": t["id"], "offset": 3600})
    planner.create_activity({"contact_id": day["id"], "template_id": t["id"], "offset": 3600, "duration": 600})
    result = planner.save_rule({"name": "iss", "template_id": t["id"], "satellite_ids": [iss["id"]]})
    geo_window = (day["aos"] + 3600, day["aos"] + 4200)
    iss_that_day = [
        c
        for c in planner.contacts
        if c["satellite_id"] == iss["id"]
        and not c["partial"]
        and day["aos"] <= c["aos"] < day["los"]
        and not (c["aos"] < geo_window[1] and geo_window[0] < c["los"])
        and c["aos"] - 0 > planner.clock() + 10
    ]
    scheduled = [
        a
        for a in planner.activities.values()
        if a.get("rule_id") == result["rule"]["id"] and day["aos"] <= a["contact"]["aos"] < day["los"]
    ]
    assert iss_that_day and len(scheduled) == len(iss_that_day)


# B21 (C-9): a hand-edited rule activity was cancelled and replaced when its pass moved, losing the edits
def test_a_hand_edited_rule_activity_is_flagged_not_replaced_when_its_pass_moves(engine, planner):
    planner.save_station(DC)
    sat = planner.save_satellite({"name": "ISS", "tle": iss_like(90001, 325.0)})
    planner.save_rule({"name": "r", "template_id": tmpl(engine)["id"], "satellite_ids": [sat["id"]]})
    activity = min(
        (a for a in planner.activities.values() if a["status"] == "scheduled"), key=lambda a: a["contact"]["aos"]
    )
    planner.update_activity(activity["id"], {"offset": -300, "label": "hand tuned"})
    planner.set_tle(sat["id"], iss_like(90001, 315.0))  # moves the pass well past the re-time tolerance
    edited = planner.activities[activity["id"]]
    assert edited["status"] == "needs_review" and not edited.get("superseded")
    assert edited["label"] == "hand tuned" and edited["offset"] == -300
    assert edited["suggested_contact_id"]


# B22 (C-13): a template parameter's pattern runs under the planner lock: catastrophic ones are refused
@pytest.mark.parametrize("pattern", [r"(a+)+$", r"(a*)*b", r"(a|aa)*c", r"(\w+\s?)+$", "x" * 201])
def test_dangerous_parameter_patterns_are_refused(pattern):
    from flow_automator.templates import check_parameters

    problems = check_parameters([{"name": "p", "type": "string", "pattern": pattern}])
    assert problems, pattern


@pytest.mark.parametrize("pattern", [r"[A-Z]{3}\d+", r"(NORMAL|SPECIAL)", r"\d{1,5}(\.\d+)?", r"^[a-z_]+$"])
def test_ordinary_parameter_patterns_are_fine(pattern):
    from flow_automator.templates import check_parameters

    assert check_parameters([{"name": "p", "type": "string", "pattern": pattern}]) == []


def test_a_stored_dangerous_pattern_is_not_run():
    import time

    from flow_automator.templates import fill_parameters

    started = time.monotonic()
    _values, problems = fill_parameters([{"name": "p", "type": "string", "pattern": r"(a+)+$"}], {"p": "a" * 40 + "!"})
    assert problems and time.monotonic() - started < 0.5
