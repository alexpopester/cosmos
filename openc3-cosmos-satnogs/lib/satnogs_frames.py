"""SatNOGS frames as COSMOS telemetry: no COSMOS imports here, so it's testable on its own.

SatNOGS volunteer ground stations record satellites' downlinks and publish every
decoded frame as a small file on each observation ("demoddata"), named with the
time it was received. ``SatnogsFeed`` finds new observations of the configured
satellites through the public Network API, downloads their frames, decodes them
with the ``satnogs-decoders`` package (Kaitai Struct decoders kept by the SatNOGS
community) and turns each one into a JSON document for a COSMOS packet:

- the decoder's fields, under their own names (booleans as 0/1, bytes as hex)
- ``_frame_time`` (Unix seconds, when the station received it), ``_observation``,
  ``_station``, ``_station_id``, ``_norad``, ``_frame_hex`` and ``_frame_bytes``

Each satellite's frames are told apart by one decoded field (a CCSDS APID, a
beacon type): a frame goes to packet ``<PREFIX>_<value>`` if the target defines
it, to ``OTHER`` if it decoded but has no packet of its own, and to ``UNDECODED``
if the decoder couldn't read it.
"""

import collections
import concurrent.futures
import datetime
import hashlib
import json
import re
import time


NETWORK_API = "https://network.satnogs.org/api"
USER_AGENT = "openc3-cosmos-satnogs (+https://github.com/OpenC3/cosmos)"
FRAME_NAME = re.compile(r"data_(\d+)_(\d{4})-(\d{2})-(\d{2})T(\d{2})-(\d{2})-(\d{2})")
REQUEST_GAP_S = 2.0  # SatNOGS is volunteer run: one request at a time
MAX_PAGES = 4  # observations pages per satellite per poll (25 each)
MAX_SEEN = 50000  # frame names remembered, to skip them on the next poll
DOWNLOAD_WORKERS = 8
DUPLICATE_WINDOW_S = 120  # the same bytes this close together: one beacon heard by several stations
META_KEYS = ("_frame_time", "_observation", "_station", "_station_id", "_norad", "_frame_hex", "_frame_bytes")


class Satellite:
    """One satellite: its COSMOS target, NORAD number, decoder and how its packets are told apart."""

    def __init__(self, target, norad, decoder, field, prefix):
        self.target = str(target).upper()
        self.norad = int(norad)
        self.decoder = str(decoder)
        self.field = str(field)
        self.prefix = str(prefix).upper()

    def decoder_class(self):
        from satnogsdecoders import decoder  # noqa: PLC0415 - heavy; only when decoding

        wanted = self.decoder.lower()
        for name in dir(decoder):
            if name.lower() == wanted and name[0].isupper():
                return getattr(decoder, name)
        raise ValueError(f"satnogs-decoders has no decoder '{self.decoder}'")


def frame_time(name):
    """When a frame was received (Unix seconds), from its file name, or None."""
    match = FRAME_NAME.search(str(name or ""))
    if not match:
        return None
    parts = [int(p) for p in match.groups()[1:]]
    return datetime.datetime(*parts, tzinfo=datetime.timezone.utc).timestamp()


def plain(value):
    """A decoded value as something JSON (and COSMOS) can hold."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, (int, float)) or value is None:
        return value
    if isinstance(value, str):
        return value.replace("\x00", "").strip()
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return str(value)


def decode(decoder_class, data):
    """The decoder's fields for a frame, or None if it can't read it."""
    from satnogsdecoders import decoder  # noqa: PLC0415

    try:
        fields = decoder.get_fields(decoder_class.from_bytes(bytes(data)))
    except Exception:  # noqa: BLE001 - Kaitai raises all sorts for frames it doesn't describe
        return None
    return {str(k): plain(v) for k, v in fields.items()}


def packet_name(satellite, fields, known_packets):
    if fields is None:
        return "UNDECODED"
    value = fields.get(satellite.field)
    if value is not None:
        name = f"{satellite.prefix}_{value}"
        if name in known_packets:
            return name
    return "OTHER"


def frame_document(satellite, observation, name, data, fields):
    """The JSON a COSMOS packet is made of."""
    document = dict(fields or {})
    document.update(
        _frame_time=frame_time(name),
        _observation=int(observation["id"]),
        _station=str(observation.get("station_name") or ""),
        _station_id=int(observation.get("ground_station") or 0),
        _norad=satellite.norad,
        _frame_hex=bytes(data).hex(),
        _frame_bytes=len(data),
    )
    return document


class Frame:
    __slots__ = ("target", "packet", "time", "document")

    def __init__(self, target, packet, time_, document):
        self.target, self.packet, self.time, self.document = target, packet, time_, document

    def json(self):
        return json.dumps(self.document, separators=(",", ":")).encode()


class SatnogsFeed:
    """Polls SatNOGS for new frames of some satellites (``poll_iter``: one observation at a time)."""

    def __init__(
        self,
        satellites,
        known_packets,
        session=None,
        lookback_hours=6.0,
        token=None,
        clock=time.time,
        sleep=time.sleep,
        log=None,
    ):
        import requests  # noqa: PLC0415

        self.satellites = list(satellites)
        self.known_packets = {t: set(p) for t, p in known_packets.items()}
        self.session = session or requests.Session()
        self.lookback = float(lookback_hours) * 3600
        self.token = token
        self.clock = clock
        self.sleep = sleep
        self.log = log or (lambda level, message: None)
        self.seen = collections.OrderedDict()  # frame names already turned into packets
        self.newest = {}  # target -> newest frame COSMOS had at connect: older ones aren't delivered again
        self.decoders = {}
        self._last_request = 0.0
        self.recent = {}  # (target, frame hash) -> when it was received, to drop copies from other stations
        self.stats = {"polls": 0, "observations": 0, "frames": 0, "duplicates": 0, "undecoded": 0, "errors": 0}

    def _get(self, url, params=None):
        headers = {"User-Agent": USER_AGENT}
        if self.token:
            headers["Authorization"] = f"Token {self.token}"
        wait = REQUEST_GAP_S - (self.clock() - self._last_request)
        if wait > 0:
            self.sleep(wait)
        self._last_request = self.clock()
        response = self.session.get(url, params=params, headers=headers, timeout=60)
        if response.status_code == 429:
            raise RateLimitedError(response.headers.get("Retry-After"))
        response.raise_for_status()
        return response

    def _download(self, url):
        # Frame files are on the SatNOGS object store, not the rate-limited API
        response = self.session.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
        response.raise_for_status()
        return response.content

    def observations(self, satellite):
        """Observations of this satellite with frames, ending within the lookback, newest first."""
        cutoff = self.clock() - self.lookback
        url = f"{NETWORK_API}/observations/"
        # Newest first, and SatNOGS lists observations booked days ahead: ask only for ones already over,
        # or the page budget goes on future bookings and never reaches the frames
        params = {"format": "json", "norad_cat_id": satellite.norad, "end": _iso(self.clock())}
        found = []
        for _page in range(MAX_PAGES):
            response = self._get(url, params)
            batch = response.json()
            for observation in batch:
                if _iso_time(observation.get("end")) < cutoff:
                    return found
                if observation.get("demoddata") and observation.get("status") not in ("failed", "future"):
                    found.append(observation)
            url, params = (getattr(response, "links", None) or {}).get("next", {}).get("url"), None
            if not url or not batch:
                break
        return found

    def poll(self):
        """Every new frame from one poll, oldest first (tests and tools; the interface uses poll_iter)."""
        frames = [frame for batch in self.poll_iter() for frame in batch]
        frames.sort(key=lambda f: f.time)
        return frames

    def poll_iter(self):
        """New frames, one observation at a time, so COSMOS gets them while the rest download.

        Each satellite's observations go oldest first, each one's frames in time order. A rate limit ends
        the poll (the next poll picks up the rest); trouble with one satellite or observation is logged
        and skipped.
        """
        self.stats["polls"] += 1
        for satellite in self.satellites:
            try:
                observations = self.observations(satellite)
            except RateLimitedError as limited:
                self.log("warn", f"SatNOGS rate limit reached ({limited}); trying again next poll")
                return
            except Exception as error:  # noqa: BLE001 - one satellite's trouble mustn't stop the others
                self.stats["errors"] += 1
                self.log("error", f"SatNOGS {satellite.target}: {type(error).__name__}: {error}")
                continue
            for observation in reversed(observations):
                try:
                    frames = self._observation_frames(satellite, observation)
                except Exception as error:  # noqa: BLE001
                    self.stats["errors"] += 1
                    self.log("error", f"SatNOGS observation {observation.get('id')}: {type(error).__name__}: {error}")
                    continue
                if frames:
                    yield frames

    def _observation_frames(self, satellite, observation):
        decoder_class = self.decoders.get(satellite.target)
        if decoder_class is None:
            decoder_class = self.decoders[satellite.target] = satellite.decoder_class()
        # Only for frames from before a restart: within a run, ``seen`` stops repeats. (Advancing it as frames
        # arrive would drop a station's late upload of frames older than another station's.)
        cutoff = self.newest.get(satellite.target, 0.0)
        known = self.known_packets.get(satellite.target, set())
        wanted = []
        for item in observation["demoddata"]:
            url = item.get("payload_demod") if isinstance(item, dict) else item
            name = str(url or "").rsplit("/", 1)[-1]
            received = frame_time(name)
            if received is not None and name not in self.seen and received > cutoff:
                wanted.append((received, name, url))
        if not wanted:
            return []
        self.stats["observations"] += 1
        # Frame files are on the object store, not the rate-limited API: fetch several at a time
        with concurrent.futures.ThreadPoolExecutor(DOWNLOAD_WORKERS) as pool:
            contents = list(pool.map(self._download, [url for _t, _n, url in wanted]))
        frames = []
        for (received, name, _url), data in sorted(zip(wanted, contents, strict=True), key=lambda w: w[0][0]):
            self._remember(name)
            # The same beacon heard by several stations: deliver it once
            key = (satellite.target, hashlib.sha1(data).digest())  # noqa: S324 - an identity, not security
            if abs(self.recent.get(key, -1e18) - received) <= DUPLICATE_WINDOW_S:
                self.stats["duplicates"] += 1
                continue
            self.recent[key] = received
            fields = decode(decoder_class, data)
            self.stats["frames"] += 1
            if fields is None:
                self.stats["undecoded"] += 1
            document = frame_document(satellite, observation, name, data, fields)
            frames.append(Frame(satellite.target, packet_name(satellite, fields, known), received, document))
        self._forget_old(received)
        return frames

    def _remember(self, name):
        self.seen[name] = True
        while len(self.seen) > MAX_SEEN:
            self.seen.popitem(last=False)

    def _forget_old(self, now):
        horizon = now - self.lookback - DUPLICATE_WINDOW_S
        if len(self.recent) > MAX_SEEN:
            self.recent = {k: t for k, t in self.recent.items() if t >= horizon}


class RateLimitedError(Exception):
    pass


def _iso(t):
    return datetime.datetime.fromtimestamp(t, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_time(text):
    try:
        return datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
