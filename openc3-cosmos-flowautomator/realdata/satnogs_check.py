"""Compare Flow Automator pass predictions with real SatNOGS observations.

Step 1 of docs/REAL-DATA-TESTING.md. For each observation it uses the exact TLE
SatNOGS used and the station's real position, so the only thing under test is
our propagation and look-angle code (lib/flow_automator/orbit.py). It checks:

- window: our AOS/LOS above the station's min_horizon against the observation's
  scheduled start/end (full-pass observations)
- geometry: our max elevation and start/end azimuths against SatNOGS's
- real signal: our elevation at the receive time of every decoded frame. A
  frame heard while we say the satellite is below the horizon is a prediction
  error that no amount of modelling difference explains.

Usage (from the plugin folder):

    python realdata/satnogs_check.py --pages 10            # ~250 recent good observations
    python realdata/satnogs_check.py --norad 25544 43017   # only these satellites
    python realdata/satnogs_check.py --offline             # re-check what's cached

API responses are cached in realdata/cache/ so reruns cost nothing; reports go
to realdata/reports/. Both are git-ignored.
"""

import argparse
import datetime
import json
import math
import os
import re
import sys
import time

import numpy as np


HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lib"))

from flow_automator import orbit  # noqa: E402


NETWORK_API = "https://network.satnogs.org/api"
CACHE_DIR = os.path.join(HERE, "cache")
REPORT_DIR = os.path.join(HERE, "reports")
USER_AGENT = "openc3-flowautomator-realdata-check (+https://github.com/OpenC3/cosmos)"

REQUEST_GAP_S = 2.0  # be polite to a volunteer-run service
STATIONS_MAX_AGE_S = 86400

# A frame heard below this geometric elevation can't be refraction (about 0.5 deg at the horizon)
FRAME_TOLERANCE_DEG = -1.0
# The scheduled window may start/end this far below the station mask before we flag it:
# refraction (~0.5 deg) plus SatNOGS rounding plus a station that changed its mask since
MASK_TOLERANCE_DEG = 1.5
# An observation counts as a full pass when both edges are within this of our AOS/LOS,
# and our elevation at both is within FULL_PASS_DEG of the horizon
FULL_PASS_S = 60.0
FULL_PASS_DEG = 2.5
MAX_ELEVATION_TOLERANCE_DEG = 2.0  # max_altitude is a whole degree
AZIMUTH_TOLERANCE_DEG = 3.0  # rise/set azimuths are whole degrees
# Both window edges within this of one whole degree: that's the horizon it was scheduled at
HORIZON_FIT_DEG = 0.3
# A full-pass window moved (or stretched) by more than this is reported
SHIFT_S = 5.0

FRAME_NAME = re.compile(r"data_(\d+)_(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})")


# ---- helpers -----------------------------------------------------------------


def parse_time(text):
    """ISO-8601 UTC ('2026-11-05T18:55:50Z') to Unix seconds."""
    text = str(text).replace("Z", "+00:00")
    moment = datetime.datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.timestamp()


def iso(t):
    return datetime.datetime.fromtimestamp(t, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def frame_times(observation):
    """Receive times (Unix seconds, sorted) of the observation's decoded frames, from their file names."""
    times = []
    for item in observation.get("demoddata") or []:
        url = item.get("payload_demod") if isinstance(item, dict) else item
        match = FRAME_NAME.search(str(url or ""))
        if not match or int(match.group(1)) != int(observation["id"]):
            continue
        year, month, day, hour, minute, second = (int(part) for part in match.groups()[1:])
        moment = datetime.datetime(year, month, day, hour, minute, second, tzinfo=datetime.timezone.utc)
        times.append(moment.timestamp())
    return sorted(times)


def azimuth_difference(ours, theirs):
    """ours - theirs, wrapped to [-180, 180)."""
    return (ours - theirs + 180.0) % 360.0 - 180.0


def scheduled_horizon(start_elevation, end_elevation):
    """The whole-degree horizon a window was scheduled against, if both edges sit on it.

    SatNOGS windows start and end where the satellite crosses an integer elevation,
    unless someone scheduled only part of the pass. So: both edges on one whole degree
    (a full pass), or one edge on it and the other higher up (a pass cut short).

    Returns the geometric elevation of that horizon (what our Station mask should be),
    and whether it was refracted: some schedulers (PyEphem with its default pressure)
    put "0 deg" where the refracted, apparent elevation is 0, which is about 0.57 deg
    below the geometric horizon. Returns (None, False) when the edges fit no horizon.
    """

    def fit(edge, refracted):
        nearest = round(edge + (refraction(edge) if refracted else 0.0))
        geometric = nearest - refraction(nearest) if refracted else float(nearest)
        return geometric if nearest >= 0 and abs(edge - geometric) <= HORIZON_FIT_DEG else None

    for refracted in (False, True):  # full pass: both edges on the same horizon
        start_fit, end_fit = fit(start_elevation, refracted), fit(end_elevation, refracted)
        if start_fit is not None and start_fit == end_fit:
            return round(start_fit, 3), refracted
    for refracted in (False, True):  # cut short: one edge on it, the other clearly higher
        for edge, other in ((start_elevation, end_elevation), (end_elevation, start_elevation)):
            geometric = fit(edge, refracted)
            if geometric is not None and other > geometric + FULL_PASS_DEG:
                return round(geometric, 3), refracted
    return None, False


def refraction(apparent_elevation):
    """Standard atmospheric refraction (degrees) at an apparent elevation (Bennett's formula)."""
    elevation = max(apparent_elevation, -1.0)
    return 1.02 / math.tan(math.radians(elevation + 10.3 / (elevation + 5.11))) / 60.0


# ---- the check ---------------------------------------------------------------


def check_observation(observation, station_info=None):
    """Compare one SatNOGS observation with our prediction. Returns a result dict with 'flags'.

    station_info is the station's entry from the SatNOGS station list (for min_horizon);
    without it the mask is taken as 0 deg.
    """
    result = {
        "id": observation["id"],
        "norad_cat_id": observation.get("norad_cat_id"),
        "satellite": (observation.get("tle0") or "").removeprefix("0 ").strip(),
        "station_id": observation.get("ground_station"),
        "station": observation.get("station_name"),
        "start": observation.get("start"),
        "end": observation.get("end"),
        "tle_source": observation.get("tle_source"),
        "flags": [],  # disagreements with our prediction
        "notes": [],  # quirks of the SatNOGS record, not prediction errors
    }
    try:
        line1, line2 = observation["tle1"].strip(), observation["tle2"].strip()
        propagator = orbit.Propagator(line1, line2)
        start, end = parse_time(observation["start"]), parse_time(observation["end"])
        mask = float((station_info or {}).get("min_horizon") or 0.0)
        station = orbit.Station(
            observation["station_lat"], observation["station_lng"], observation.get("station_alt") or 0, mask
        )
    except (KeyError, TypeError, ValueError, orbit.OrbitError) as error:
        result["error"] = f"unusable observation: {error}"
        return result
    if end <= start:
        result["error"] = "observation ends before it starts"
        return result

    result["mask"] = mask
    result["duration"] = end - start

    times = np.arange(start, end + 1.0, 1.0)
    times[-1] = end
    ecef, ok = propagator.ecef(times)
    if not ok.all():
        result["error"] = "SGP4 failed inside the observation window (decayed or bad elements)"
        return result
    azimuths, elevations, _ranges = station.look(ecef)

    # Geometry inside the scheduled window
    result["start_elevation"] = round(float(elevations[0]), 2)
    result["end_elevation"] = round(float(elevations[-1]), 2)
    result["max_elevation"] = round(float(elevations.max()), 2)
    if observation.get("max_altitude") is not None:
        result["max_elevation_diff"] = round(result["max_elevation"] - float(observation["max_altitude"]), 2)
        if abs(result["max_elevation_diff"]) > MAX_ELEVATION_TOLERANCE_DEG:
            result["flags"].append("max_elevation_off")
    if observation.get("rise_azimuth") is not None:
        result["rise_azimuth_diff"] = round(azimuth_difference(float(azimuths[0]), observation["rise_azimuth"]), 1)
    if observation.get("set_azimuth") is not None:
        result["set_azimuth_diff"] = round(azimuth_difference(float(azimuths[-1]), observation["set_azimuth"]), 1)
    for key in ("rise_azimuth_diff", "set_azimuth_diff"):
        if key in result and abs(result[key]) > AZIMUTH_TOLERANCE_DEG:
            result["flags"].append("azimuth_off")
            break
    horizon, refracted = scheduled_horizon(result["start_elevation"], result["end_elevation"])
    if refracted:
        result["notes"].append("refracted_horizon")
    if horizon is not None and horizon != mask:
        # Scheduled against another horizon than the station's mask today (a manual
        # schedule, or the mask changed since). Matching it exactly is itself a pass.
        result["scheduled_horizon"] = horizon
        station = orbit.Station(
            observation["station_lat"], observation["station_lng"], observation.get("station_alt") or 0, horizon
        )
    else:
        horizon = mask
        if result["start_elevation"] < mask - MASK_TOLERANCE_DEG:
            result["flags"].append("starts_below_mask")
        if result["end_elevation"] < mask - MASK_TOLERANCE_DEG:
            result["flags"].append("ends_below_mask")

    # Our predicted pass for the same window
    passes = orbit.find_passes(propagator, station, start - 1800, end + 1800)
    best, best_overlap = None, 0.0
    for candidate in passes:
        overlap = min(end, candidate["los"]) - max(start, candidate["aos"])
        if overlap > best_overlap:
            best, best_overlap = candidate, overlap
    if best is None:
        result["flags"].append("no_predicted_pass")
    else:
        result["predicted_aos"] = iso(best["aos"])
        result["predicted_los"] = iso(best["los"])
        result["aos_diff"] = round(start - best["aos"], 1)  # positive: SatNOGS starts after our AOS
        result["los_diff"] = round(end - best["los"], 1)  # positive: SatNOGS ends after our LOS
        # Full pass: both edges at the horizon (loosely, so a shifted window still counts)
        result["full_pass"] = (
            abs(result["aos_diff"]) <= FULL_PASS_S
            and abs(result["los_diff"]) <= FULL_PASS_S
            and abs(result["start_elevation"] - horizon) <= FULL_PASS_DEG
            and abs(result["end_elevation"] - horizon) <= FULL_PASS_DEG
        )
        shift = (result["aos_diff"] + result["los_diff"]) / 2
        stretch = (result["los_diff"] - result["aos_diff"]) / 2
        if result["full_pass"] and abs(shift) > SHIFT_S and abs(stretch) < abs(shift) / 4:
            # Both edges moved the same way and the pass length didn't change: the
            # window was planned from another TLE than the one the record carries.
            result["notes"].append("window_shifted")
            result["window_shift"] = round(shift, 1)
            # its edges are off the mask for the same reason
            result["flags"] = [f for f in result["flags"] if f not in ("starts_below_mask", "ends_below_mask")]
        if result["full_pass"] and abs(stretch) > SHIFT_S and abs(shift) < abs(stretch) / 4:
            result["flags"].append("window_length_off")

    # Real signal: where did we think the satellite was when each frame arrived?
    heard = frame_times(observation)
    result["frames"] = len(heard)
    if heard:
        frame_ecef, frame_ok = propagator.ecef(np.array(heard))
        frame_elevations = np.where(frame_ok, station.look(frame_ecef)[1], -90.0)
        result["min_frame_elevation"] = round(float(frame_elevations.min()), 2)
        below = [iso(t) for t, el in zip(heard, frame_elevations, strict=True) if el < FRAME_TOLERANCE_DEG]
        result["frames_below_horizon"] = len(below)
        if below:
            result["flags"].append("frame_below_horizon")
            result["frames_below_horizon_at"] = below[:10]
        outside = [t for t in heard if t < start - 1 or t > end + 1]
        result["frames_outside_window"] = len(outside)
    return result


# ---- statistics --------------------------------------------------------------


def _stats(values):
    if not values:
        return None
    values = np.asarray(values, dtype=float)
    magnitude = np.abs(values)
    return {
        "count": int(values.size),
        "mean": round(float(values.mean()), 3),
        "median": round(float(np.median(values)), 3),
        "p95_abs": round(float(np.percentile(magnitude, 95)), 3),
        "max_abs": round(float(magnitude.max()), 3),
    }


def summarize(results):
    checked = [r for r in results if "error" not in r]
    # Windows planned from another TLE than the recorded one say nothing about our timing
    full = [r for r in checked if r.get("full_pass") and "window_shifted" not in r["notes"]]
    with_frames = [r for r in checked if r.get("frames")]
    flags, notes = {}, {}
    for r in checked:
        for flag in r["flags"]:
            flags[flag] = flags.get(flag, 0) + 1
        for note in r["notes"]:
            notes[note] = notes.get(note, 0) + 1
    notes["scheduled_horizon_differs"] = sum(1 for r in checked if "scheduled_horizon" in r)
    return {
        "notes": notes,
        "observations": len(results),
        "checked": len(checked),
        "errors": len(results) - len(checked),
        "satellites": len({r["norad_cat_id"] for r in checked}),
        "stations": len({r["station_id"] for r in checked}),
        "full_passes": len(full),
        "observations_with_frames": len(with_frames),
        "frames": sum(r["frames"] for r in with_frames),
        "frames_below_horizon": sum(r.get("frames_below_horizon", 0) for r in with_frames),
        "flags": flags,
        "metrics": {
            "aos_diff_s (full passes)": _stats([r["aos_diff"] for r in full]),
            "los_diff_s (full passes)": _stats([r["los_diff"] for r in full]),
            "max_elevation_diff_deg": _stats([r["max_elevation_diff"] for r in checked if "max_elevation_diff" in r]),
            "rise_azimuth_diff_deg (full passes)": _stats(
                [r["rise_azimuth_diff"] for r in full if "rise_azimuth_diff" in r]
            ),
            "set_azimuth_diff_deg (full passes)": _stats(
                [r["set_azimuth_diff"] for r in full if "set_azimuth_diff" in r]
            ),
            "min_frame_elevation_deg": _stats([r["min_frame_elevation"] for r in with_frames]),
        },
    }


def format_summary(summary, results):
    lines = [
        f"Observations: {summary['observations']} ({summary['checked']} checked, {summary['errors']} unusable)",
        f"Satellites: {summary['satellites']}   Stations: {summary['stations']}   Full passes: {summary['full_passes']}",
        f"Frames: {summary['frames']} in {summary['observations_with_frames']} observations, "
        f"{summary['frames_below_horizon']} heard below {FRAME_TOLERANCE_DEG} deg by our prediction",
        "",
        f"{'metric':42} {'n':>5} {'median':>9} {'p95 |x|':>9} {'max |x|':>9}",
    ]
    for name, stats in summary["metrics"].items():
        if stats:
            lines.append(
                f"{name:42} {stats['count']:5d} {stats['median']:9.2f} {stats['p95_abs']:9.2f} {stats['max_abs']:9.2f}"
            )
    lines.append("")
    lines.append("Flags: " + (", ".join(f"{k}={v}" for k, v in sorted(summary["flags"].items())) or "none"))
    lines.append(
        "SatNOGS record notes (not prediction errors): "
        + (", ".join(f"{k}={v}" for k, v in sorted(summary["notes"].items()) if v) or "none")
    )
    flagged = [r for r in results if r.get("flags")]
    for r in flagged[:40]:
        detail = []
        for key in ("aos_diff", "los_diff", "max_elevation_diff", "start_elevation", "end_elevation"):
            if key in r:
                detail.append(f"{key}={r[key]}")
        if r.get("frames_below_horizon"):
            detail.append(f"frames_below={r['frames_below_horizon']}/{r['frames']} min_el={r['min_frame_elevation']}")
        lines.append(
            f"  obs {r['id']} {r['satellite'] or r['norad_cat_id']} @ {r['station']} (mask {r.get('mask')}): "
            f"{','.join(r['flags'])}  {' '.join(detail)}"
        )
    if len(flagged) > 40:
        lines.append(f"  ... and {len(flagged) - 40} more in the JSON report")
    for r in results:
        if "error" in r:
            lines.append(f"  obs {r['id']}: {r['error']}")
    return "\n".join(lines)


# ---- SatNOGS access (cached) -------------------------------------------------


class SatnogsClient:
    def __init__(self, cache_dir=CACHE_DIR, token=None, offline=False):
        self.cache_dir = cache_dir
        self.offline = offline
        self.token = token
        self._last_request = 0.0
        os.makedirs(os.path.join(cache_dir, "observations"), exist_ok=True)

    def _get(self, url, params=None):
        import requests  # only needed online

        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Token {self.token}"
        for _attempt in range(5):
            wait = REQUEST_GAP_S - (time.time() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.time()
            response = requests.get(url, params=params, headers=headers, timeout=60)
            if response.status_code == 429:
                delay = float(response.headers.get("Retry-After") or 60)
                print(f"SatNOGS rate limit: waiting {delay:.0f} s", file=sys.stderr)
                time.sleep(min(delay, 900))
                continue
            response.raise_for_status()
            return response
        raise RuntimeError(f"SatNOGS kept rate limiting {url}")

    def stations(self):
        path = os.path.join(self.cache_dir, "stations.json")
        fresh = os.path.exists(path) and time.time() - os.path.getmtime(path) < STATIONS_MAX_AGE_S
        if not fresh and not self.offline:
            data = self._get(f"{NETWORK_API}/stations/", {"format": "json"}).json()
            with open(path, "w") as file:
                json.dump(data, file)
        if not os.path.exists(path):
            return {}
        with open(path) as file:
            return {station["id"]: station for station in json.load(file)}

    def observations(self, pages, norad_ids=None, before=None):
        """Recent 'good' observations, newest first; each is cached as cache/observations/<id>.json."""
        if self.offline:
            return self.cached_observations(norad_ids)
        found = []
        for norad in norad_ids or [None]:
            params = {"format": "json", "status": "good"}
            if norad:
                params["norad_cat_id"] = norad
            if before:
                params["end"] = before
            url = f"{NETWORK_API}/observations/"
            for _page in range(pages):
                response = self._get(url, params)
                batch = response.json()
                for observation in batch:
                    with open(os.path.join(self.cache_dir, "observations", f"{observation['id']}.json"), "w") as file:
                        json.dump(observation, file)
                found.extend(batch)
                url = response.links.get("next", {}).get("url")
                params = None  # the next link carries them
                if not url or not batch:
                    break
        return found

    def cached_observations(self, norad_ids=None):
        folder = os.path.join(self.cache_dir, "observations")
        found = []
        for name in sorted(os.listdir(folder)):
            if name.endswith(".json"):
                with open(os.path.join(folder, name)) as file:
                    observation = json.load(file)
                if not norad_ids or observation.get("norad_cat_id") in norad_ids:
                    found.append(observation)
        return found


# ---- command line ------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pages", type=int, default=10, help="pages of 25 observations per query (default 10)")
    parser.add_argument("--norad", type=int, nargs="*", help="only these NORAD catalog numbers")
    parser.add_argument("--before", help="only observations ending before this ISO time (SatNOGS 'end' filter)")
    parser.add_argument("--offline", action="store_true", help="use only what's already in the cache")
    parser.add_argument("--min-frames", type=int, default=0, help="skip observations with fewer decoded frames")
    parser.add_argument("--token", default=os.environ.get("SATNOGS_TOKEN"), help="SatNOGS API token (optional)")
    parser.add_argument("--cache", default=CACHE_DIR)
    args = parser.parse_args(argv)

    client = SatnogsClient(args.cache, token=args.token, offline=args.offline)
    stations = client.stations()
    observations = client.observations(args.pages, args.norad, args.before)
    unique = {o["id"]: o for o in observations}
    results = [
        check_observation(o, stations.get(o.get("ground_station")))
        for o in unique.values()
        if len(o.get("demoddata") or []) >= args.min_frames
    ]
    if not stations:
        print("No station list: masks taken as 0 deg", file=sys.stderr)

    summary = summarize(results)
    print(format_summary(summary, results))
    os.makedirs(REPORT_DIR, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(REPORT_DIR, f"satnogs_check_{stamp}.json")
    with open(path, "w") as file:
        json.dump({"summary": summary, "results": results}, file, indent=1)
    print(f"\nReport: {path}")
    return 1 if summary["frames_below_horizon"] else 0


if __name__ == "__main__":
    sys.exit(main())
