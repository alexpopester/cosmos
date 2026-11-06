"""satnogs_replay: recorded real passes played back now, faster, late, faulty or lost."""

import os
import sys

import pytest


HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lib"))
sys.path.insert(0, HERE)

import satnogs_frames as sf  # noqa: E402
import satnogs_replay as replay  # noqa: E402
from test_satnogs_frames import defined_packets  # noqa: E402


pytest.importorskip("satnogsdecoders")

AOS = 1_800_000_000.0
SATELLITES = {
    "CANVAS": sf.Satellite("REPLAY_CANVAS", 68635, "canvas", "application_process_id", "APID"),
    "AEPEX": sf.Satellite("REPLAY_AEPEX", 68506, "aepex", "application_process_id", "APID"),
    "CATSAT": sf.Satellite("REPLAY_CATSAT", 60246, "catsat", "type", "TYPE"),
}


def recording_for(target):
    name = next(n for n in replay.list_recordings() if n.startswith(f"{target}_"))
    return replay.load(name)


def play(target, **options):
    satellite = SATELLITES[target]
    known = set(defined_packets(target))
    return replay.schedule(recording_for(target), satellite, satellite.decoder_class(), known, AOS, **options)


def test_there_is_a_recording_per_satellite():
    names = replay.list_recordings()
    for target in SATELLITES:
        assert any(n.startswith(f"{target}_") for n in names), names
        recording = recording_for(target)
        assert recording["frames"] and recording["duration"] > 60
        assert all(0 <= f["offset"] <= recording["duration"] + 5 for f in recording["frames"])


def test_recording_names_cant_escape_the_folder():
    for bad in ("../plugin", "/etc/passwd", ".hidden", ""):
        with pytest.raises(ValueError):
            replay.load(bad)


@pytest.mark.parametrize("target", list(SATELLITES))
def test_a_replay_happens_now_and_delivers_after_los_like_an_upload(target):
    recording = recording_for(target)
    entries, los = play(target)
    assert los == pytest.approx(AOS + recording["duration"])
    assert len(entries) == len(recording["frames"])
    times = [doc["_frame_time"] for _when, _t, _p, doc in entries]
    assert min(times) >= AOS and max(times) <= los + 5  # the pass, moved to now
    assert {when for when, *_ in entries} == {los + 60}  # all arrive a minute after LOS
    assert {t for _w, t, _p, _d in entries} == {f"REPLAY_{target}"}
    assert any(p.startswith(("APID_", "TYPE_")) for _w, _t, p, _d in entries)  # real beacons in there
    assert entries[0][3]["_station"] == recording["station"]


def test_speed_and_live_delivery():
    recording = recording_for("CANVAS")
    entries, los = play("CANVAS", speed=10, delivery=-1)
    assert los == pytest.approx(AOS + recording["duration"] / 10)
    assert all(when == doc["_frame_time"] for when, _t, _p, doc in entries)  # live: as heard
    assert [when for when, *_ in entries] == sorted(when for when, *_ in entries)


def test_overrides_inject_a_fault_into_real_frames():
    entries, _los = play("CANVAS", overrides={"bcn_cmd_rjct_cnt": 3})
    beacons = [doc for _w, _t, p, doc in entries if p == "APID_32"]
    assert beacons and all(doc["bcn_cmd_rjct_cnt"] == 3 for doc in beacons)
    assert all(doc["bcn_cmd_recv_cnt"] == 94 for doc in beacons)  # everything else is the real value


def test_drop_loses_frames_evenly_and_one_loses_them_all():
    full, _ = play("AEPEX")
    half, _ = play("AEPEX", drop=0.5)
    assert len(half) == round(len(full) / 2)
    assert play("AEPEX", drop=1.0)[0] == []
    kept = replay.kept(list(range(10)), 0.3)
    assert len(kept) == 7 and kept == sorted(set(kept))
    assert max(b - a for a, b in zip(kept, kept[1:], strict=False)) <= 2  # spread out, not a block lost
    assert replay.kept(list(range(10)), 0.3) == kept  # the same every time: replays repeat


def test_bad_speed_is_refused():
    with pytest.raises(ValueError):
        play("CATSAT", speed=0)


def test_replayed_newest_frame_time_never_goes_back():
    entries, _ = play("CATSAT", delivery=-1)
    newest = [doc["_newest_frame_time"] for *_x, doc in entries]
    assert newest == sorted(newest) and newest[-1] == max(doc["_frame_time"] for *_x, doc in entries)
