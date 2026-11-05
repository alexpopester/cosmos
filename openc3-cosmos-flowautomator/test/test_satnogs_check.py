"""realdata/satnogs_check.py: comparing our pass predictions with SatNOGS observations."""

import copy
import datetime
import json
import os
import sys

import pytest


HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "realdata"))

import satnogs_check as sc  # noqa: E402
from flow_automator import orbit  # noqa: E402


EPOCH = 1793836800.0  # 2026-11-05T00:00:00Z
LAT, LON, ALT = 47.9, 19.9, 900  # a station under the synthetic orbit below


def _name(t):
    return datetime.datetime.fromtimestamp(t, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")


def synthetic(mask=10.0, shift=0.0, frames=None):
    """A SatNOGS-shaped observation of one of our own predicted passes (a perfect record)."""
    tle = orbit.make_tle(EPOCH, inclination=97.5, raan=60.0, mean_anomaly=10.0).split("\n")
    propagator = orbit.Propagator(*tle)
    station = orbit.Station(LAT, LON, ALT, mask)
    found = [p for p in orbit.find_passes(propagator, station, EPOCH, EPOCH + 86400) if p["max_elevation"] > 30]
    assert found, "the synthetic orbit should pass high over the station within a day"
    p = found[0]
    start, end = round(p["aos"]) + shift, round(p["los"]) + shift
    frame_times = frames if frames is not None else [p["tca"] - 60, p["tca"], p["tca"] + 60]
    observation = {
        "id": 1,
        "norad_cat_id": 99999,
        "ground_station": 7,
        "station_name": "TEST",
        "station_lat": LAT,
        "station_lng": LON,
        "station_alt": ALT,
        "start": sc.iso(start),
        "end": sc.iso(end),
        "tle0": "0 TESTSAT",
        "tle1": tle[0],
        "tle2": tle[1],
        "max_altitude": round(p["max_elevation"]),
        "rise_azimuth": round(p["aos_azimuth"]),
        "set_azimuth": round(p["los_azimuth"]),
        "demoddata": [{"payload_demod": f"https://example/data_1_{_name(t)}"} for t in frame_times],
    }
    return observation, {"id": 7, "min_horizon": mask}, p


def test_parse_time_and_frame_times():
    assert sc.parse_time("2026-11-05T00:00:00Z") == EPOCH
    observation = {
        "id": 15370483,
        "demoddata": [
            {"payload_demod": "https://s3/x/data_15370483_2026-11-05T18-58-47"},
            {"payload_demod": "https://s3/x/data_15370483_2026-11-05T18-56-01"},
            {"payload_demod": "https://s3/x/data_99_2026-11-05T18-56-02"},  # another observation's frame
            {"payload_demod": "https://s3/x/something_else"},
        ],
    }
    times = sc.frame_times(observation)
    assert [sc.iso(t) for t in times] == ["2026-11-05T18:56:01Z", "2026-11-05T18:58:47Z"]


def test_azimuth_difference_wraps():
    assert sc.azimuth_difference(359.0, 1.0) == pytest.approx(-2.0)
    assert sc.azimuth_difference(1.0, 359.0) == pytest.approx(2.0)


def test_scheduled_horizon():
    assert sc.scheduled_horizon(10.05, 9.92) == (10.0, False)  # full pass on 10 deg
    assert sc.scheduled_horizon(0.01, 19.3) == (0.0, False)  # cut short after rising at 0
    assert sc.scheduled_horizon(10.88, 9.18) == (None, False)  # a shifted window, not a 9 deg horizon
    horizon, refracted = sc.scheduled_horizon(-0.6, -0.5)  # apparent 0 deg: refracted
    assert refracted and horizon == pytest.approx(-0.48, abs=0.02)
    assert sc.refraction(0.0) == pytest.approx(0.48, abs=0.02)


def test_a_perfect_record_has_no_flags():
    observation, station, _p = synthetic()
    result = sc.check_observation(observation, station)
    assert result["flags"] == [] and result["notes"] == []
    assert result["full_pass"]
    assert abs(result["aos_diff"]) <= 1 and abs(result["los_diff"]) <= 1
    assert abs(result["max_elevation_diff"]) <= 0.5
    assert result["frames"] == 3 and result["frames_below_horizon"] == 0


def test_a_frame_heard_below_the_horizon_is_flagged():
    observation, station, p = synthetic()
    observation = copy.deepcopy(observation)
    observation["end"] = sc.iso(p["los"] + 600)
    observation["demoddata"].append({"payload_demod": f"https://example/data_1_{_name(p['los'] + 400)}"})
    result = sc.check_observation(observation, station)
    assert "frame_below_horizon" in result["flags"]
    assert result["frames_below_horizon"] == 1


def test_a_window_planned_from_another_tle_is_a_note_not_a_flag():
    observation, station, _p = synthetic(shift=8.0)
    result = sc.check_observation(observation, station)
    assert result["notes"] == ["window_shifted"]
    assert result["window_shift"] == pytest.approx(8.0, abs=1.0)
    assert result["flags"] == []
    summary = sc.summarize([result])
    assert summary["metrics"]["aos_diff_s (full passes)"] is None  # kept out of the timing statistics


def test_a_window_scheduled_on_another_horizon():
    observation, _station, _p = synthetic(mask=5.0)
    result = sc.check_observation(observation, {"id": 7, "min_horizon": 20.0})
    assert result["scheduled_horizon"] == 5.0
    assert result["flags"] == [] and result["full_pass"]


def test_unusable_observations_are_reported_not_raised():
    observation, station, _p = synthetic()
    broken = dict(observation, tle2="2 garbage")
    assert "error" in sc.check_observation(broken, station)
    backwards = dict(observation, end=observation["start"])
    assert "error" in sc.check_observation(backwards, station)


def test_a_real_satnogs_observation():
    """KOSTKA over OM4ATC, 2026-09-30, 8 decoded frames: our prediction matches the real pass."""
    with open(os.path.join(HERE, "fixtures", "satnogs_observation.json")) as file:
        fixture = json.load(file)
    result = sc.check_observation(fixture["observation"], fixture["station"])
    assert result["flags"] == [] and result["notes"] == []
    assert result["full_pass"]
    assert abs(result["aos_diff"]) <= 2 and abs(result["los_diff"]) <= 2
    assert abs(result["max_elevation_diff"]) <= 1
    assert result["frames"] == 8 and result["frames_below_horizon"] == 0
    assert result["min_frame_elevation"] > 20


def test_offline_client_reads_the_cache(tmp_path):
    observation, _station, _p = synthetic()
    folder = tmp_path / "observations"
    folder.mkdir()
    (folder / "1.json").write_text(json.dumps(observation))
    client = sc.SatnogsClient(str(tmp_path), offline=True)
    assert [o["id"] for o in client.observations(3)] == [1]
    assert client.observations(3, norad_ids=[12345]) == []
    assert client.stations() == {}
