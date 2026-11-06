"""Record a real SatNOGS pass for replay (lib/recordings/<TARGET>_<observation>.json).

Developer tool (not part of the gem's runtime). A recording is one observation:
its frames (hex) with their offsets from the observation's start, the station,
the window and the TLE SatNOGS used. The replay interface plays it back at any
time, at any speed, with faults injected (satnogs_replay.py).

    python tools/record_pass.py CANVAS                 # the recent good pass with the most frames
    python tools/record_pass.py CANVAS --observation 15370483
"""

import argparse
import json
import os
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "lib"))

import satnogs_frames as sf  # noqa: E402
from generate_definitions import SATELLITES  # noqa: E402


MIN_FRAMES = 20
MAX_FRAMES = 300  # keep recordings small: a long pass from a busy station is plenty


def pick_observation(feed, norad, observation_id=None):
    if observation_id:
        return feed._get(f"{sf.NETWORK_API}/observations/{observation_id}/", {"format": "json"}).json()
    response = feed._get(f"{sf.NETWORK_API}/observations/", {"format": "json", "norad_cat_id": norad, "status": "good"})
    candidates = [o for o in response.json() if MIN_FRAMES <= len(o.get("demoddata") or []) <= MAX_FRAMES]
    if not candidates:
        raise SystemExit(f"No recent good observation of {norad} with {MIN_FRAMES}-{MAX_FRAMES} frames")
    return max(candidates, key=lambda o: len(o["demoddata"]))


def record(target, observation_id=None):
    norad = next(n for t, n, *_ in SATELLITES if t == target)
    feed = sf.SatnogsFeed([], {})
    observation = pick_observation(feed, norad, observation_id)
    start = sf._iso_time(observation["start"])
    frames = []
    for item in observation["demoddata"]:
        url = item["payload_demod"]
        received = sf.frame_time(url)
        if received is None:
            continue
        frames.append({"offset": round(received - start, 3), "hex": feed._download(url).hex()})
    frames.sort(key=lambda f: f["offset"])
    recording = {
        "target": target,
        "norad": norad,
        "observation": observation["id"],
        "station": observation.get("station_name"),
        "station_id": observation.get("ground_station"),
        "start": observation["start"],
        "end": observation["end"],
        "duration": round(sf._iso_time(observation["end"]) - start, 3),
        "tle": [observation.get("tle1"), observation.get("tle2")],
        "frames": frames,
    }
    folder = os.path.join(ROOT, "lib", "recordings")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{target}_{observation['id']}.json")
    with open(path, "w") as file:
        json.dump(recording, file, indent=1)
    print(f"{path}: {len(frames)} frames over {recording['duration']:.0f} s, heard by {recording['station']}")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("target", choices=[t for t, *_ in SATELLITES])
    parser.add_argument("--observation", type=int)
    args = parser.parse_args()
    record(args.target, args.observation)


if __name__ == "__main__":
    main()
