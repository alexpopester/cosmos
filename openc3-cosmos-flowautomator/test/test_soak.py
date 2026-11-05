"""realdata/soak_*.py: the real-pass soak's setup payloads, recorder and report."""

import os
import sys

import pytest
from test_contacts import EPOCH, Clock


pytest.importorskip("sgp4")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "realdata"))

import satnogs_check as sc  # noqa: E402
import soak_recorder  # noqa: E402
import soak_report  # noqa: E402
import soak_setup  # noqa: E402
from flow_automator.graph import validate_flow  # noqa: E402
from flow_automator.orbit import make_tle  # noqa: E402


@pytest.fixture
def clock():
    return Clock(EPOCH)


@pytest.fixture
def planner(engine, clock):
    planner = engine.planner
    planner.clock = clock
    planner.horizon = 86400
    return planner


def iss_like(catalog, mean_anomaly, epoch=EPOCH):
    return make_tle(epoch, 51.6416, 247.4627, mean_anomaly, mean_motion=15.49815395, catalog=catalog)


def soak(engine, planner):
    """The soak as soak_setup.py builds it, on the in-memory engine. Returns the manifest."""
    template, problems = engine.save_flow(soak_setup.template_flow())
    assert problems == []
    sats = [
        planner.save_satellite({"name": "Soak: A", "tle": iss_like(90001, 325.0)}),
        planner.save_satellite({"name": "Soak: B", "tle": iss_like(90002, 100.0)}),
    ]
    station = planner.save_station(
        soak_setup.station_payload(
            {"id": 91, "name": "M0EYT", "lat": 38.9, "lng": -77.0, "altitude": 50, "min_horizon": 10}
        )
    )
    result = planner.save_rule(soak_setup.rule_payload(template["id"], [s["id"] for s in sats], [station["id"]]))
    assert result["created"] > 0
    return {
        "started": EPOCH,
        "started_iso": sc.iso(EPOCH),
        "rule_id": result["rule"]["id"],
        "satellites": {sats[0]["id"]: {"norad": 90001, "name": "A"}, sats[1]["id"]: {"norad": 90002, "name": "B"}},
        "stations": {station["id"]: {"satnogs_id": 91, "name": "M0EYT", "min_horizon": 10}},
    }


def test_the_template_is_valid_and_sends_nothing():
    flow = soak_setup.template_flow()
    assert validate_flow(flow) == []
    assert not [n for n in flow["nodes"] if n["type"] in ("send_cmd", "run_script", "set_tlm", "notify", "ask")]


def test_station_payload_keeps_the_satnogs_horizon():
    payload = soak_setup.station_payload(
        {"id": 7, "name": "X", "lat": 1.5, "lng": 2.5, "altitude": None, "min_horizon": 95}
    )
    assert payload["name"] == "Soak: X #7" and payload["altitude"] == 0
    assert payload["min_elevation"] == 89 and payload["capacity"] == 1


def test_recorder_logs_tles_contacts_and_activities(engine, planner, clock):
    manifest = soak(engine, planner)
    events, state = soak_recorder.diff(planner.schedule(), {}, manifest, clock())
    kinds = [e["type"] for e in events]
    assert kinds.count("tle") == 2
    assert kinds.count("contact_new") == len(state["contacts"]) > 0
    assert kinds.count("activity") == len(state["activities"]) > 0
    assert "status" in kinds

    # Nothing changed: nothing logged
    again, state = soak_recorder.diff(planner.schedule(), state, manifest, clock())
    assert again == []

    # A newer TLE moves the passes ~9 s (enough for the planner to give them new ids): the recorder
    # pairs them with their earlier prediction, and the activities follow
    sat_id = next(iter(manifest["satellites"]))
    planner.set_tle(sat_id, iss_like(90001, 325.6))
    moved, state = soak_recorder.diff(planner.schedule(), state, manifest, clock())
    kinds = [e["type"] for e in moved]
    assert kinds.count("tle") == 1
    shifted = [e for e in moved if e["type"] == "contact_moved"]
    assert shifted and all(e.get("previous_id") and 5 < abs(e["aos_shift"]) < 15 for e in shifted)
    assert not [e for e in moved if e["type"] in ("contact_new", "contact_gone") and e["satellite_id"] == sat_id]
    drifted = [e for e in moved if e["type"] == "activity" and e["drift"]]
    assert drifted and all(e["previous_drift"] == 0 for e in drifted)

    # Another satellite deleted: its future passes are reported gone
    other = list(manifest["satellites"])[1]
    planner.delete_satellite(other)
    gone, state = soak_recorder.diff(planner.schedule(), state, manifest, clock())
    assert any(e["type"] == "contact_gone" and e["satellite_id"] == other for e in gone)


def test_report_scores_a_soak_against_a_satnogs_observation():
    sys.path.insert(0, os.path.dirname(__file__))
    from test_satnogs_check import synthetic

    observation, station, p = synthetic()
    observation.update(ground_station=91, norad_cat_id=99999)
    manifest = {
        "started": EPOCH,
        "started_iso": sc.iso(EPOCH),
        "rule_id": "r",
        "satellites": {"s1": {"norad": 99999, "name": "TESTSAT"}},
        "stations": {"g1": {"satnogs_id": 91, "name": "TEST"}},
    }
    received = p["aos"] - 7200
    events = [
        {
            "time": received,
            "type": "tle",
            "norad": 99999,
            "satellite_id": "s1",
            "received": received,
            "epoch": sc.orbit.parse_tle(observation["tle1"], observation["tle2"])["epoch"],
            "line1": observation["tle1"],
            "line2": observation["tle2"],
        },
        {
            "time": received,
            "type": "contact_new",
            "id": "c1",
            "satellite_id": "s1",
            "station_id": "g1",
            "aos": p["aos"] + 3,
            "los": p["los"],
            "tca": p["tca"],
            "max_elevation": 50,
            "partial": False,
        },
        {
            "time": received + 600,
            "type": "contact_moved",
            "id": "c1",
            "satellite_id": "s1",
            "station_id": "g1",
            "aos": p["aos"],
            "los": p["los"],
            "tca": p["tca"],
            "max_elevation": 50,
            "partial": False,
        },
        {
            "time": p["los"] + 10,
            "type": "activity",
            "id": "a1",
            "rule_id": "r",
            "satellite_id": "s1",
            "station_id": "g1",
            "contact_id": "c1",
            "aos": p["aos"],
            "tca": p["tca"],
            "los": p["los"],
            "status": "done",
            "anchor": "aos",
            "offset": -60,
            "started_at": p["aos"] - 59,
            "drift": -3.0,
            "previous_status": "running",
            "previous_drift": -3.0,
        },
    ]
    report = soak_report.build_report(manifest, events, [observation], {91: station})

    assert report["accuracy_total"] == 1 and report["frames_below_total"] == 0
    filled = [row for row in report["accuracy_by_age"] if row["observations"]]
    assert len(filled) == 1 and filled[0]["frames"] == 3 and filled[0]["frames_below_horizon"] == 0
    age = report["accuracy"][0]["tle_age_h"]
    assert filled[0]["tle_age"] == soak_report._bin(age, soak_report.AGE_BINS_H)
    assert report["coverage"] == [
        {
            "observation": 1,
            "satellite": "TESTSAT",
            "station": "TEST",
            "start": observation["start"],
            "frames": 3,
            "predicted": True,
            "outcome": "done",
        }
    ]
    behaviour = report["scheduler"]
    assert behaviour["statuses"] == {"done": 1}
    assert behaviour["start_lateness_s"]["max_abs"] == pytest.approx(1.0)
    assert report["churn"][0]["contacts"] == 1  # first predicted 2 h ahead, moved 3 s
    assert report["churn"][0]["aos_shift_s"]["max_abs"] == pytest.approx(3.0)
    assert "Operational accuracy" in soak_report.markdown(report)


def test_tle_timeline_picks_the_tle_held_at_the_time():
    events = [
        {"type": "tle", "norad": 1, "received": 100, "epoch": 50, "line1": "a1", "line2": "a2"},
        {"type": "tle", "norad": 1, "received": 200, "epoch": 150, "line1": "b1", "line2": "b2"},
    ]
    timeline = soak_report.TleTimeline(events)
    assert timeline.held_at(1, 99) is None
    assert timeline.held_at(1, 150) == (50.0, "a1", "a2")
    assert timeline.held_at(1, 200) == (150.0, "b1", "b2")
    assert timeline.held_at(2, 500) is None
