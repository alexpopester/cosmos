"""satnogs_frames: real SatNOGS frames (test/fixtures) to COSMOS packet documents, and the polling."""

import json
import os
import re
import sys

import pytest


HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lib"))

import satnogs_frames as sf  # noqa: E402


pytest.importorskip("satnogsdecoders")

FIXTURES = os.path.join(HERE, "fixtures")
SATELLITES = {
    "CANVAS": sf.Satellite("CANVAS", 68635, "canvas", "application_process_id", "APID"),
    "AEPEX": sf.Satellite("AEPEX", 68506, "aepex", "application_process_id", "APID"),
    "CATSAT": sf.Satellite("CATSAT", 60246, "catsat", "type", "TYPE"),
}


def read(path, mode="r"):
    with open(path, mode) as file:
        return file.read()


def defined_packets(target):
    """{packet: {item: json key}} from the generated targets/<target>/cmd_tlm/tlm.txt."""
    path = os.path.join(HERE, "..", "targets", target, "cmd_tlm", "tlm.txt")
    packets, current, item = {}, None, None
    for line in read(path).splitlines():
        words = line.split()
        if not words:
            continue
        if words[0] == "TELEMETRY":
            current = packets.setdefault(words[4] if words[1] == "<%=" else words[2], {})
        elif words[0] == "APPEND_ITEM":
            item = words[1]
        elif words[0] == "KEY":
            current[item] = words[1][2:]
    return packets


def fixture_frames():
    for target in sorted(os.listdir(FIXTURES)):
        for name in sorted(os.listdir(os.path.join(FIXTURES, target))):
            yield target, name[:-4], read(os.path.join(FIXTURES, target, name), "rb")


def test_frame_time_from_the_file_name():
    assert sf.frame_time("https://x/data_15370483_2026-11-05T18-58-47") == 1793905127.0
    assert sf.frame_time("data_1_2026-11-05T18-58-47_g0") == 1793905127.0
    assert sf.frame_time("waterfall.png") is None


def test_plain_values_are_json_friendly():
    assert sf.plain(True) == 1 and sf.plain(b"\x01\xff") == "01ff"
    assert sf.plain("LASP \x00\x00") == "LASP" and sf.plain([True, 2]) == [1, 2]
    json.dumps({k: sf.plain(v) for k, v in {"a": object()}.items()})


@pytest.mark.parametrize(("target", "expected", "data"), list(fixture_frames()))
def test_every_real_fixture_frame_lands_in_its_packet(target, expected, data):
    satellite = SATELLITES[target]
    packets = defined_packets(target)
    fields = sf.decode(satellite.decoder_class(), data)
    packet = sf.packet_name(satellite, fields, set(packets))
    assert packet == ("OTHER" if expected.startswith("OTHER_") else expected)

    observation = {"id": 15370483, "station_name": "dm43", "ground_station": 5078}
    document = sf.frame_document(satellite, observation, "data_15370483_2026-11-05T18-58-47", data, fields)
    json.loads(json.dumps(document))  # all JSON
    assert document["_frame_time"] == 1793905127.0 and document["_frame_hex"] == data.hex()
    # Every item the packet defines either is in this frame or is a field this packet type sometimes lacks;
    # and every field in the frame has an item (nothing decoded is dropped)
    keys = set(packets[packet].values())
    if packet != "UNDECODED":
        assert set(document) <= keys, set(document) - keys


def test_undecodable_frames_go_to_undecoded():
    satellite = SATELLITES["CANVAS"]
    fields = sf.decode(satellite.decoder_class(), b"\x00\x01garbage")
    assert fields is None
    assert sf.packet_name(satellite, fields, {"APID_32"}) == "UNDECODED"


def test_definitions_look_like_cosmos_wants():
    for target in SATELLITES:
        text = read(os.path.join(HERE, "..", "targets", target, "cmd_tlm", "tlm.txt"))
        assert text.count("TELEMETRY ") == text.count("ACCESSOR JsonAccessor")
        assert text.count("READ_CONVERSION openc3/conversions/unix_time_conversion.py FRAME_TIME") == text.count(
            "TELEMETRY "
        )
        names = re.findall(r"APPEND_ITEM (\S+) ", text)
        assert all(re.fullmatch(r"[A-Z][A-Z0-9_]*", n) for n in names)


# ---- polling against a fake SatNOGS -------------------------------------------------------------


class FakeResponse:
    def __init__(self, status, body=None, content=b"", links=None, headers=None):
        self.status_code, self._body, self.content = status, body, content
        self.links, self.headers = links or {}, headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSatnogs:
    """Observation pages and frame files, like network.satnogs.org and its object store."""

    def __init__(self, pages, frames):
        self.pages, self.frames, self.calls = pages, frames, []
        self.limited = False

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(url)
        if "/observations/" in url:
            if self.limited:
                return FakeResponse(429, headers={"Retry-After": "60"})
            page = 0 if params else int(url.rsplit("=", 1)[1])
            links = {"next": {"url": f"{url.split('?')[0]}?page={page + 1}"}} if page + 1 < len(self.pages) else {}
            return FakeResponse(200, self.pages[page], links=links)
        return FakeResponse(200, content=self.frames[url])


def observation(obs_id, end, frame_names):
    return {
        "id": obs_id,
        "end": end,
        "status": "good",
        "station_name": "dm43",
        "ground_station": 5078,
        "demoddata": [{"payload_demod": f"https://s3/{n}"} for n in frame_names],
    }


@pytest.fixture
def canvas_frame():
    return read(os.path.join(FIXTURES, "CANVAS", "APID_32.bin"), "rb")


def canvas_frames():
    """Three different real CANVAS frames (the same bytes twice would be one beacon heard twice)."""
    return [read(os.path.join(FIXTURES, "CANVAS", f), "rb") for f in ("APID_32.bin", "OTHER_5.bin", "OTHER_202.bin")]


def feed_for(service, now):
    return sf.SatnogsFeed(
        [SATELLITES["CANVAS"]],
        {"CANVAS": set(defined_packets("CANVAS"))},
        session=service,
        lookback_hours=6,
        clock=lambda: now,
        sleep=lambda s: None,
    )


def test_poll_returns_new_frames_oldest_first_and_only_once(canvas_frame):
    now = 1793905127.0 + 3600  # an hour after the frames
    names = ["data_2_2026-11-05T18-58-50", "data_2_2026-11-05T18-58-47", "data_1_2026-11-05T17-00-00"]
    pages = [
        [observation(2, "2026-11-05T19:00:00Z", names[:2])],
        [observation(1, "2026-11-05T17:05:00Z", names[2:]), observation(0, "2026-11-04T00:00:00Z", ["data_0_x"])],
    ]
    service = FakeSatnogs(pages, {f"https://s3/{n}": data for n, data in zip(names, canvas_frames(), strict=True)})
    feed = feed_for(service, now)
    frames = feed.poll()
    assert [f.document["_frame_time"] for f in frames] == sorted(sf.frame_time(n) for n in names)
    assert {f.packet for f in frames} == {"APID_32", "OTHER"} and {f.target for f in frames} == {"CANVAS"}
    assert "https://s3/data_0_x" not in service.calls  # older than the lookback: never fetched
    assert feed.poll() == []  # nothing new


def test_frames_cosmos_already_has_are_skipped(canvas_frame):
    names = ["data_2_2026-11-05T18-58-47", "data_2_2026-11-05T18-58-50"]
    service = FakeSatnogs(
        [[observation(2, "2026-11-05T19:00:00Z", names)]], {f"https://s3/{n}": canvas_frame for n in names}
    )
    feed = feed_for(service, 1793905127.0 + 600)
    feed.newest["CANVAS"] = sf.frame_time(names[0])  # what the interface found in COSMOS at connect
    assert [f.time for f in feed.poll()] == [sf.frame_time(names[1])]


def test_a_rate_limit_ends_the_poll_without_losing_anything(canvas_frame):
    messages = []
    service = FakeSatnogs([[]], {})
    service.limited = True
    feed = feed_for(service, 1793905127.0)
    feed.log = lambda level, message: messages.append((level, message))
    assert feed.poll() == []
    assert messages and messages[0][0] == "warn" and "rate limit" in messages[0][1]


def test_only_observations_already_over_are_asked_for(canvas_frame):
    # Found live: CANVAS had ~2 days of observations booked ahead, newest first, so the page budget went on
    # future bookings and no frame was ever reached
    now = 1793905127.0 + 3600
    service = FakeSatnogs(
        [[observation(2, "2026-11-05T19:00:00Z", ["data_2_2026-11-05T18-58-47"])]],
        {"https://s3/data_2_2026-11-05T18-58-47": canvas_frame},
    )
    params_seen = []
    original = service.get

    def get(url, params=None, **kwargs):
        if params:
            params_seen.append(params)
        return original(url, params, **kwargs)

    service.get = get
    feed = feed_for(service, now)
    assert len(feed.poll()) == 1
    assert params_seen[0]["end"] == "2026-11-05T19:58:47Z"  # ending before now
    future = observation(3, "2026-11-07T19:00:00Z", ["data_3_2026-11-07T18-58-47"])
    future["status"] = "future"
    service.pages = [[future]]
    feed.seen.clear()
    assert feed.poll() == []


def test_the_same_beacon_heard_by_several_stations_is_delivered_once():
    data = canvas_frames()
    a = observation(2, "2026-11-05T19:00:00Z", ["data_2_2026-11-05T18-58-47", "data_2_2026-11-05T18-58-57"])
    b = observation(3, "2026-11-05T19:00:00Z", ["data_3_2026-11-05T18-58-48", "data_3_2026-11-05T18-59-30"])
    files = {
        "https://s3/data_2_2026-11-05T18-58-47": data[0],
        "https://s3/data_2_2026-11-05T18-58-57": data[1],
        "https://s3/data_3_2026-11-05T18-58-48": data[0],  # station 3 heard the first beacon too
        "https://s3/data_3_2026-11-05T18-59-30": data[2],  # and one station 2 missed
    }
    feed = feed_for(FakeSatnogs([[b, a]], files), 1793905127.0 + 3600)
    frames = feed.poll()
    assert len(frames) == 3 and feed.stats["duplicates"] == 1
    assert [f.document["_observation"] for f in frames] == [2, 2, 3]


def test_a_late_upload_of_older_frames_is_still_delivered():
    # Station A uploads first; station B's frames from earlier in the same pass arrive on a later poll
    data = canvas_frames()
    early = observation(2, "2026-11-05T19:00:00Z", ["data_2_2026-11-05T18-58-47"])
    late = observation(3, "2026-11-05T19:00:00Z", ["data_3_2026-11-05T18-55-00"])
    files = {"https://s3/data_2_2026-11-05T18-58-47": data[0], "https://s3/data_3_2026-11-05T18-55-00": data[1]}
    service = FakeSatnogs([[early]], files)
    feed = feed_for(service, 1793905127.0 + 600)
    assert len(feed.poll()) == 1
    service.pages = [[late, early]]
    late_frames = feed.poll()
    assert [f.document["_observation"] for f in late_frames] == [3]
    # Older than what COSMOS already has: FRAME_TIME goes back, NEWEST_FRAME_TIME doesn't
    assert late_frames[0].document["_frame_time"] < sf.frame_time("data_2_2026-11-05T18-58-47")
    assert late_frames[0].document["_newest_frame_time"] == sf.frame_time("data_2_2026-11-05T18-58-47")


def test_poll_iter_hands_over_one_observation_at_a_time_oldest_first():
    data = canvas_frames()
    newer = observation(2, "2026-11-05T19:00:00Z", ["data_2_2026-11-05T18-58-47"])
    older = observation(1, "2026-11-05T17:05:00Z", ["data_1_2026-11-05T17-00-00", "data_1_2026-11-05T17-00-30"])
    files = {
        "https://s3/data_2_2026-11-05T18-58-47": data[0],
        "https://s3/data_1_2026-11-05T17-00-00": data[1],
        "https://s3/data_1_2026-11-05T17-00-30": data[2],
    }
    feed = feed_for(FakeSatnogs([[newer, older]], files), 1793905127.0 + 3600)
    batches = list(feed.poll_iter())
    assert [[f.document["_observation"] for f in batch] for batch in batches] == [[1, 1], [2]]
