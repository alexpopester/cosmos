"""Satellite pass prediction: SGP4 from a TLE, then look angles from ground stations.

``sgp4`` gives the satellite position in the TEME frame. It's rotated into an
Earth-fixed frame with Greenwich sidereal time (IAU-82, as Vallado's SGP4
reference code does), which is accurate to a few hundredths of a degree for
look angles and needs no data files, so it works air gapped. Polar motion and
the UT1-UTC difference are ignored; for pass times that's well under a second.

Times are Unix seconds (UTC) everywhere.
"""

import datetime
import math

import numpy as np


try:
    from sgp4.api import SGP4_ERRORS, Satrec

    SGP4_IMPORT_ERROR = None
except ImportError as _error:  # the engine still runs flows without it; contacts report the problem
    Satrec = None
    SGP4_ERRORS = {}
    SGP4_IMPORT_ERROR = f"The sgp4 Python package is not installed ({_error})"


WGS84_A_KM = 6378.137
WGS84_F = 1 / 298.257223563
WGS84_E2 = WGS84_F * (2 - WGS84_F)
UNIX_EPOCH_JD = 2440587.5

COARSE_STEP_S = 20.0  # sampling step; shorter passes are found by refining near-horizon peaks
TIME_TOLERANCE_S = 0.1


class OrbitError(Exception):
    pass


# ---- TLEs --------------------------------------------------------------------


def _checksum(line):
    total = 0
    for char in line[:68]:
        if char.isdigit():
            total += int(char)
        elif char == "-":
            total += 1
    return total % 10


def parse_tle(text_or_line1, line2=None):
    """Parse a TLE (two lines, or three with a name line first) and check it.

    Returns {"name", "line1", "line2", "norad_id", "epoch"} with epoch in Unix seconds.
    Raises OrbitError with a readable message for anything SGP4 can't use.
    """
    if line2 is None:
        lines = [line.strip() for line in str(text_or_line1 or "").replace("\r", "").split("\n") if line.strip()]
    else:
        lines = [str(text_or_line1 or "").strip(), str(line2 or "").strip()]
    name = ""
    if len(lines) == 3:
        name = lines[0][2:].strip() if lines[0].startswith("0 ") else lines[0]
        lines = lines[1:]
    if len(lines) != 2:
        raise OrbitError("A TLE needs two lines starting with '1 ' and '2 ' (a name line before them is optional)")
    line1, line2 = lines
    for number, line in ((1, line1), (2, line2)):
        if not line.startswith(f"{number} "):
            raise OrbitError(f"Line {number} of the TLE must start with '{number} '")
        if len(line) != 69:
            raise OrbitError(f"Line {number} of the TLE must be 69 characters, it is {len(line)}")
        if not line[68].isdigit() or int(line[68]) != _checksum(line):
            raise OrbitError(f"Line {number} of the TLE fails its checksum (expected {_checksum(line)})")
    if line1[2:7] != line2[2:7]:
        raise OrbitError("The two TLE lines are for different satellites (catalog numbers differ)")
    if SGP4_IMPORT_ERROR:
        raise OrbitError(SGP4_IMPORT_ERROR)
    try:
        satrec = Satrec.twoline2rv(line1, line2)
    except Exception as error:  # noqa: BLE001 - sgp4 raises ValueError and friends on bad fields
        raise OrbitError(f"SGP4 can't read the TLE: {error}") from None
    if satrec.error:
        raise OrbitError(f"SGP4 can't use the TLE: {SGP4_ERRORS.get(satrec.error, satrec.error)}")
    epoch = (satrec.jdsatepoch + satrec.jdsatepochF - UNIX_EPOCH_JD) * 86400.0
    return {
        "name": name,
        "line1": line1,
        "line2": line2,
        "norad_id": int(line1[2:7].replace(" ", "0") or 0) if line1[2:7].strip().isdigit() else line1[2:7].strip(),
        "epoch": epoch,
    }


# ---- geometry ----------------------------------------------------------------


def _julian(times):
    times = np.asarray(times, dtype=float)
    days = np.floor(times / 86400.0)
    return UNIX_EPOCH_JD + days, (times - days * 86400.0) / 86400.0


def _gmst(jd, fr):
    """Greenwich mean sidereal time (radians), IAU-82."""
    tut1 = (jd - 2451545.0 + fr) / 36525.0
    seconds = 67310.54841 + (876600.0 * 3600 + 8640184.812866) * tut1 + 0.093104 * tut1**2 - 6.2e-6 * tut1**3
    return np.mod(seconds * (2 * math.pi / 86400.0), 2 * math.pi)


def station_ecef(latitude, longitude, altitude_m):
    """Geodetic (degrees, metres) to Earth-fixed km on WGS-84."""
    lat = math.radians(latitude)
    lon = math.radians(longitude)
    height = altitude_m / 1000.0
    radius = WGS84_A_KM / math.sqrt(1 - WGS84_E2 * math.sin(lat) ** 2)
    return np.array(
        [
            (radius + height) * math.cos(lat) * math.cos(lon),
            (radius + height) * math.cos(lat) * math.sin(lon),
            (radius * (1 - WGS84_E2) + height) * math.sin(lat),
        ]
    )


class Propagator:
    """SGP4 for one TLE. ``ecef(times)`` gives Earth-fixed positions (km) and a validity mask."""

    def __init__(self, line1, line2):
        if SGP4_IMPORT_ERROR:
            raise OrbitError(SGP4_IMPORT_ERROR)
        self.satrec = Satrec.twoline2rv(line1, line2)

    def ecef(self, times):
        jd, fr = _julian(np.atleast_1d(times))
        errors, positions, _velocities = self.satrec.sgp4_array(jd, fr)
        theta = _gmst(jd, fr)
        cos_t, sin_t = np.cos(theta), np.sin(theta)
        x, y, z = positions[:, 0], positions[:, 1], positions[:, 2]
        ecef = np.column_stack((cos_t * x + sin_t * y, -sin_t * x + cos_t * y, z))
        return ecef, errors == 0

    def subpoint(self, t):
        """(latitude, longitude, altitude km) under the satellite, for tests and display."""
        (x, y, z), _ok = self.ecef([t])[0][0], None
        lon = math.degrees(math.atan2(y, x))
        p = math.hypot(x, y)
        lat = math.atan2(z, p * (1 - WGS84_E2))
        for _ in range(5):
            radius = WGS84_A_KM / math.sqrt(1 - WGS84_E2 * math.sin(lat) ** 2)
            lat = math.atan2(z + radius * WGS84_E2 * math.sin(lat), p)
        radius = WGS84_A_KM / math.sqrt(1 - WGS84_E2 * math.sin(lat) ** 2)
        altitude = p / math.cos(lat) - radius
        return math.degrees(lat), lon, altitude


class Station:
    """A ground station's local frame. ``look(ecef)`` gives (azimuth, elevation, range) in degrees/km."""

    def __init__(self, latitude, longitude, altitude_m=0.0, min_elevation=0.0):
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self.altitude_m = float(altitude_m or 0)
        self.min_elevation = float(min_elevation or 0)
        self.position = station_ecef(self.latitude, self.longitude, self.altitude_m)
        lat, lon = math.radians(self.latitude), math.radians(self.longitude)
        self.east = np.array([-math.sin(lon), math.cos(lon), 0.0])
        self.north = np.array([-math.sin(lat) * math.cos(lon), -math.sin(lat) * math.sin(lon), math.cos(lat)])
        self.up = np.array([math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat)])

    def look(self, ecef):
        rho = np.atleast_2d(ecef) - self.position
        east, north, up = rho @ self.east, rho @ self.north, rho @ self.up
        elevation = np.degrees(np.arctan2(up, np.hypot(east, north)))
        azimuth = np.mod(np.degrees(np.arctan2(east, north)), 360.0)
        return azimuth, elevation, np.linalg.norm(rho, axis=1)


# ---- pass search -------------------------------------------------------------


def _elevation(propagator, station, t):
    ecef, ok = propagator.ecef([t])
    if not ok[0]:
        return -90.0
    return float(station.look(ecef)[1][0])


def _look(propagator, station, t):
    ecef, _ok = propagator.ecef([t])
    azimuth, elevation, distance = station.look(ecef)
    return float(azimuth[0]), float(elevation[0]), float(distance[0])


def _crossing(propagator, station, lo, hi, rising):
    """Bisect for the time elevation crosses the mask between lo (one side) and hi (the other)."""
    mask = station.min_elevation
    while hi - lo > TIME_TOLERANCE_S:
        mid = (lo + hi) / 2
        above = _elevation(propagator, station, mid) >= mask
        if above == rising:
            hi = mid
        else:
            lo = mid
    return hi if rising else lo


def _peak(propagator, station, lo, hi):
    """Golden-section search for the highest elevation in [lo, hi]."""
    ratio = (math.sqrt(5) - 1) / 2
    a, b = lo, hi
    c, d = b - ratio * (b - a), a + ratio * (b - a)
    fc, fd = _elevation(propagator, station, c), _elevation(propagator, station, d)
    while b - a > TIME_TOLERANCE_S:
        if fc > fd:
            b, d, fd = d, c, fc
            c = b - ratio * (b - a)
            fc = _elevation(propagator, station, c)
        else:
            a, c, fc = c, d, fd
            d = a + ratio * (b - a)
            fd = _elevation(propagator, station, d)
    t = (a + b) / 2
    return t, _elevation(propagator, station, t)


def find_passes(propagator, station, start, end, step=COARSE_STEP_S):
    """All passes of the satellite over the station's elevation mask between start and end.

    Returns a list of {"aos", "tca", "los", "max_elevation", "aos_azimuth", "los_azimuth",
    "tca_azimuth", "duration", "partial"} sorted by AOS. A pass already in progress at
    ``start`` (or not over by ``end``) is clipped to the window and marked partial.
    """
    # Sample on absolute multiples of the step, so the same TLE gives the same pass times (and contact ids)
    # however the window slides
    start = math.floor(start / step) * step
    times = np.arange(start, end + step, step)
    times[-1] = min(times[-1], end)
    ecef, ok = propagator.ecef(times)
    elevations = np.where(ok, station.look(ecef)[1], -90.0)
    return passes_from_samples(propagator, station, times, elevations)


def passes_from_samples(propagator, station, times, elevations):
    """Find passes from sampled elevations, refining each edge and peak with extra propagation."""
    mask = station.min_elevation
    above = elevations >= mask
    windows = []  # (aos, los, partial)
    index = 0
    count = len(times)
    while index < count:
        if above[index]:
            first = index
            while index < count and above[index]:
                index += 1
            last = index - 1
            partial = first == 0 or last == count - 1
            aos = times[0] if first == 0 else _crossing(propagator, station, times[first - 1], times[first], True)
            los = (
                times[-1] if last == count - 1 else _crossing(propagator, station, times[last], times[last + 1], False)
            )
            windows.append((float(aos), float(los), partial))
            continue
        # A short pass can rise and set between two samples: refine local peaks close to the mask
        if (
            0 < index < count - 1
            and elevations[index] >= elevations[index - 1]
            and elevations[index] > elevations[index + 1]
        ) and elevations[index] > mask - 5.0:
            peak_t, peak_el = _peak(propagator, station, times[index - 1], times[index + 1])
            if peak_el >= mask:
                aos = _crossing(propagator, station, times[index - 1], peak_t, True)
                los = _crossing(propagator, station, peak_t, times[index + 1], False)
                windows.append((float(aos), float(los), False))
        index += 1

    passes = []
    for aos, los, partial in windows:
        if los <= aos:
            continue
        tca, max_el = _peak(propagator, station, aos, los)
        aos_az = _look(propagator, station, aos)[0]
        los_az = _look(propagator, station, los)[0]
        tca_az = _look(propagator, station, tca)[0]
        passes.append(
            {
                "aos": aos,
                "tca": tca,
                "los": los,
                "max_elevation": round(max_el, 2),
                "aos_azimuth": round(aos_az, 1),
                "tca_azimuth": round(tca_az, 1),
                "los_azimuth": round(los_az, 1),
                "duration": los - aos,
                "partial": partial,
            }
        )
    passes.sort(key=lambda p: p["aos"])
    return passes


def find_passes_for_stations(propagator, stations, start, end, step=COARSE_STEP_S):
    """find_passes for several stations, propagating the satellite once. Returns one list per station."""
    times = np.arange(start, end + step, step)
    times[-1] = min(times[-1], end)
    ecef, ok = propagator.ecef(times)
    results = []
    for station in stations:
        elevations = np.where(ok, station.look(ecef)[1], -90.0)
        results.append(passes_from_samples(propagator, station, times, elevations))
    return results


def make_tle(
    epoch,
    inclination,
    raan,
    mean_anomaly,
    mean_motion=15.5,
    eccentricity=0.0006703,
    arg_perigee=130.0,
    catalog=99999,
    name="",
):
    """A TLE for a circular-ish orbit at an epoch (Unix seconds), e.g. for demos and tests.

    mean_motion is revolutions per day (15.5 is about 420 km). Returns 'name\\nline1\\nline2'.
    """
    moment = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
    day = (
        moment.timetuple().tm_yday
        + (moment.hour * 3600 + moment.minute * 60 + moment.second + moment.microsecond / 1e6) / 86400
    )

    def with_checksum(text):
        return text + str(_checksum(text + "0"))

    ecc = f"{eccentricity:.7f}"[2:]
    line1 = with_checksum(
        f"1 {catalog:05d}U 26001A   {moment.year % 100:02d}{day:012.8f}  .00000000  00000-0  00000-0 0  999"
    )
    line2 = with_checksum(
        f"2 {catalog:05d} {inclination:8.4f} {raan % 360:8.4f} {ecc} {arg_perigee % 360:8.4f} "
        f"{mean_anomaly % 360:8.4f} {mean_motion:11.8f}{1:5d}"
    )
    return f"{name}\n{line1}\n{line2}" if name else f"{line1}\n{line2}"
