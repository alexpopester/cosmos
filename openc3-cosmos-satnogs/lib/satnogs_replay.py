"""Replay a recorded real pass as if it were happening now (no COSMOS imports: testable on its own).

A recording (tools/record_pass.py) is one SatNOGS observation: its frames with
their offsets from the start, the station and the TLE. ``schedule`` turns it
into (deliver at, target, packet, document) entries for a pass whose AOS is
``aos``:

- ``speed``: 10 plays a 10-minute pass in 1 minute (frame times and LOS scale)
- ``delivery``: seconds after LOS when all frames arrive, like a station's
  upload (the default, 60); below 0 delivers each frame live, at its own time
- ``overrides``: decoded fields to replace, ``{"bcn_cmd_rjct_cnt": 3}``: a fault
  injected into real data
- ``drop``: the fraction of frames lost, 0 to 1 (1: nobody heard the pass)

Every document's ``_frame_time`` is the replayed time, so automation sees the
pass now.
"""

import json
import os


RECORDINGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recordings")


def list_recordings(folder=RECORDINGS):
    if not os.path.isdir(folder):
        return []
    return sorted(name[:-5] for name in os.listdir(folder) if name.endswith(".json"))


def load(name, folder=RECORDINGS):
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError(f"Not a recording name: {name!r}")
    path = os.path.join(folder, f"{name}.json")
    if not os.path.exists(path):
        raise ValueError(f"No recording '{name}' (have: {', '.join(list_recordings(folder)) or 'none'})")
    with open(path) as file:
        return json.load(file)


def kept(frames, drop):
    """Frames left after losing ``drop`` of them, spread evenly (deterministic, so a replay repeats)."""
    drop = min(1.0, max(0.0, float(drop)))
    if drop <= 0:
        return list(frames)
    keep = round(len(frames) * (1 - drop))
    if keep <= 0:
        return []
    step = len(frames) / keep
    return [frames[int(i * step)] for i in range(keep)]


def schedule(
    recording, satellite, decoder_class, known_packets, aos, speed=1.0, delivery=60.0, overrides=None, drop=0.0
):
    """[(deliver at, target, packet, document)] in delivery order, and the replayed LOS."""
    import satnogs_frames as sf  # noqa: PLC0415

    speed = float(speed)
    if speed <= 0:
        raise ValueError("SPEED must be more than 0")
    los = aos + float(recording["duration"]) / speed
    observation = {
        "id": recording["observation"],
        "station_name": recording.get("station"),
        "ground_station": recording.get("station_id"),
    }
    entries = []
    for frame in kept(recording["frames"], drop):
        data = bytes.fromhex(frame["hex"])
        fields = sf.decode(decoder_class, data)
        if fields is not None and overrides:
            fields.update({k: sf.plain(v) for k, v in overrides.items()})
        when = aos + float(frame["offset"]) / speed
        packet = sf.packet_name(satellite, fields, known_packets)
        document = sf.frame_document(satellite, observation, "replay", data, fields)
        document["_frame_time"] = when
        deliver = when if float(delivery) < 0 else los + float(delivery)
        entries.append((deliver, satellite.target, packet, document))
    entries.sort(key=lambda e: e[0])
    newest = 0.0
    for _deliver, _target, _packet, document in entries:  # never goes back, as live (satnogs_frames)
        newest = max(newest, document["_frame_time"])
        document["_newest_frame_time"] = newest
    return entries, los
