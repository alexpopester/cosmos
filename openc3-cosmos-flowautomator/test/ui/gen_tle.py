"""Make a TLE with epoch now and a station under its track so a pass starts in about 160s."""

import datetime
import json
import sys
import time


sys.path.insert(0, sys.argv[1])
from flow_automator.orbit import Propagator, Station, _checksum, find_passes, parse_tle


def line(text):
    return text + str(_checksum(text + "0"))


now = time.time()
dt = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
doy = dt.timetuple().tm_yday + (dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1e6) / 86400


def tle(mean_anomaly):
    l1 = line(f"1 99999U 26001A   {dt.year % 100:02d}{doy:012.8f}  .00000000  00000-0  00000-0 0  999")
    l2 = line(f"2 99999 {51.64:8.4f} {200.0:8.4f} 0006703 {130.0:8.4f} {mean_anomaly:8.4f} {15.5:11.8f}{10000:5d}")
    return l1, l2


l1, l2 = tle(10.0)
parse_tle(l1, l2)
prop = Propagator(l1, l2)
lat, lon, _alt = prop.subpoint(now + 360)
lon += 1.5
passes = find_passes(prop, Station(lat, lon, 0, 10), now, now + 1800)
first = passes[0]
nudged = "\n".join(tle(10.05))
print(
    json.dumps(
        {
            "tle": f"FA-TEST\n{l1}\n{l2}",
            "nudged": nudged,
            "lat": round(lat, 4),
            "lon": round(lon, 4),
            "aos_in": first["aos"] - now,
            "los_in": first["los"] - now,
            "max_el": first["max_elevation"],
        }
    )
)
