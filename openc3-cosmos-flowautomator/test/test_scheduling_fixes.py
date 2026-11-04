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
