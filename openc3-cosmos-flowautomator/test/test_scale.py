"""Scale fixes found by the real-pass soak (docs/REAL-DATA-TESTING.md step 2; EVALUATION-2026-11-05 B43, B45).

At 16 satellites x 13 stations (4,600 activities) one scheduler tick took seconds with a few clients open,
/status 255 ms and /schedule 1 s and 6 MB. These check the indexes give the same answers as the plain
scans, and the slimmer /schedule.
"""

import gzip
import json
import random
import sys
import urllib.request

import pytest
from conftest import edge, make_flow, node
from test_contacts import DC, EPOCH, Clock


pytest.importorskip("sgp4")

from flow_automator.contacts import StationUse, _overlap  # noqa: E402
from flow_automator.http_api import FlowApi, FlowHttpServer  # noqa: E402
from flow_automator.orbit import make_tle  # noqa: E402


@pytest.fixture
def clock():
    return Clock(EPOCH)


@pytest.fixture
def planner(engine, clock):
    planner = engine.planner
    planner.clock = clock
    planner.horizon = 2 * 86400
    return planner


def template(engine):
    flow, problems = engine.save_flow(
        make_flow(
            [node("t", "trigger_manual"), node("c", "send_cmd", command="INST NOOP")],
            [edge("t", "c")],
            name="T",
            template=True,
            mode="run",
            parameters=[{"name": "level", "type": "number", "default": 1}],
        )
    )
    assert problems == []
    return flow


def busy_planner(engine, planner, satellites=6):
    """Several satellites a few degrees apart in one orbit over two stations, one rule into every pass:
    their passes overlap, so the stations are often full."""
    t = template(engine)
    sats = [
        planner.save_satellite({"name": f"S{i}", "tle": make_tle(EPOCH, 51.6, 247.0 + i, 3.0 * i, catalog=90001 + i)})
        for i in range(satellites)
    ]
    stations = [planner.save_station(DC), planner.save_station({**DC, "name": "North", "latitude": 50.0})]
    planner.save_rule(
        {
            "name": "all",
            "template_id": t["id"],
            "satellite_ids": [s["id"] for s in sats],
            "station_ids": [s["id"] for s in stations],
            "anchor": "aos",
            "offset": -60,
            "mode": "run",
        }
    )
    return t, sats, stations


# ---- StationUse gives the same answers as scanning every window ------------------------------------


def test_station_use_overlaps_match_a_plain_scan():
    rng = random.Random(7)
    use = StationUse()
    for i in range(400):
        aos = rng.uniform(0, 86400)
        use[f"c{i}"] = {"id": f"c{i}", "station_id": rng.choice("AB"), "aos": aos, "los": aos + rng.uniform(60, 900)}
    use["geo"] = {"id": "geo", "station_id": "A", "aos": 1000.0, "los": 80000.0}  # a long GEO run
    for i in range(0, 400, 3):
        use.pop(f"c{i}")
    for _ in range(300):
        aos = rng.uniform(-1000, 87000)
        window = {"station_id": rng.choice("AB"), "aos": aos, "los": aos + rng.uniform(1, 2000)}
        expected = {w["id"] for w in use.values() if w["station_id"] == window["station_id"] and _overlap(w, window)}
        assert {w["id"] for w in use.overlapping(window)} == expected


def test_station_use_copy_is_independent():
    use = StationUse()
    use["a"] = {"id": "a", "station_id": "S", "aos": 0.0, "los": 100.0}
    trial = use.copy()
    trial.pop("a")
    trial["b"] = {"id": "b", "station_id": "S", "aos": 50.0, "los": 150.0}
    assert "a" in use and "b" not in use
    assert [w["id"] for w in use.overlapping({"station_id": "S", "aos": 60.0, "los": 70.0})] == ["a"]
    use.replace_with(trial)
    assert [w["id"] for w in use.overlapping({"station_id": "S", "aos": 60.0, "los": 70.0})] == ["b"]


def test_conflicts_and_capacity_match_the_plain_scans(engine, planner, clock):
    t, _sats, _stations = busy_planner(engine, planner)
    # The rule skipped passes at a busy station; schedule some of them by hand (allowed, with a warning)
    used, _ = planner._usage()
    clashing = [c for c in planner.contacts if c["id"] not in used and planner._station_full(c, used)]
    clashing = [c for c in clashing if not c["partial"] and c["aos"] > clock() + 600][:8]
    assert clashing, "the setup should leave passes the rule skipped"
    for contact in clashing:
        planner.create_activity({"template_id": t["id"], "contact_id": contact["id"], "mode": "run", "offset": -60})
    used, conflicts = planner._usage()

    plain = {}
    windows = list(used.values())
    for w in windows:
        others = [o for o in windows if o is not w and o["station_id"] == w["station_id"] and _overlap(o, w)]
        if others:
            plain[w["id"]] = sorted(o["id"] for o in others)
    assert plain, "the setup should produce clashes"
    assert {k: sorted(v) for k, v in conflicts.items()} == plain
    for contact in planner.contacts:
        others = [o for o in windows if o["station_id"] == contact["station_id"] and _overlap(o, contact)]
        expected = contact["id"] not in used and bool(others)
        assert planner._station_full(contact, used) == expected


def test_status_follows_changes_through_the_cache(engine, planner, clock):
    t, _sats, _stations = busy_planner(engine, planner)
    before = planner.status()["scheduled"]
    assert before > 0
    activity = next(a for a in planner.activities.values() if a["status"] == "scheduled")
    planner.delete_activity(activity["id"])
    assert planner.status()["scheduled"] == before - 1
    contact = next(c for c in planner.contacts if not c["partial"] and c["aos"] > clock() + 3600)
    planner.create_activity({"template_id": t["id"], "contact_id": contact["id"], "mode": "run", "offset": -60})
    assert planner.status()["scheduled"] == before


# ---- /schedule: the window, slim activities, one in full ------------------------------------------


def test_schedule_lists_only_the_window_plus_what_needs_attention(engine, planner, clock):
    busy_planner(engine, planner)
    everything = planner.list_activities()
    assert all("parameters" in a for a in everything)
    end = clock() + 6 * 3600
    late = next(a for a in planner.activities.values() if a["contact"]["aos"] > end + 3600)
    late["status"] = "needs_review"  # far away, but needs attention
    planner._changed()

    schedule = planner.schedule(clock() - 900, end)
    listed = {a["id"] for a in schedule["activities"]}
    assert late["id"] in listed
    for a in schedule["activities"]:
        assert "parameters" not in a  # slim
        assert a["status"] == "needs_review" or a["contact"]["aos"] <= end
    waiting = [a for a in planner.activities.values() if a["status"] == "scheduled"]
    assert schedule["activities_outside"] == sum(1 for a in waiting if a["id"] not in listed) > 0

    full = planner.get_activity(late["id"])
    assert full["parameters"][0]["name"] == "level"


def test_http_responses_are_gzipped_when_asked(engine, planner):
    busy_planner(engine, planner)
    server = FlowHttpServer(FlowApi(engine, lambda token, write: None), 0, host="127.0.0.1")
    server.start()
    try:
        url = f"http://127.0.0.1:{server.port}/flow-automator/api/schedule"
        request = urllib.request.Request(url, headers={"Authorization": "x", "Accept-Encoding": "gzip"})
        with urllib.request.urlopen(request) as response:  # noqa: S310 - local test server
            assert response.headers["Content-Encoding"] == "gzip"
            zipped = response.read()
        plain_request = urllib.request.Request(url, headers={"Authorization": "x"})
        with urllib.request.urlopen(plain_request) as response:  # noqa: S310 - local test server
            assert response.headers["Content-Encoding"] is None
            plain = response.read()
        assert len(zipped) * 4 < len(plain)
        assert json.loads(gzip.decompress(zipped))["activities"] == json.loads(plain)["activities"]
        # Small answers aren't worth it
        health = urllib.request.Request(url.replace("schedule", "health"), headers={"Accept-Encoding": "gzip"})
        with urllib.request.urlopen(health) as response:  # noqa: S310 - local test server
            assert response.headers["Content-Encoding"] is None
    finally:
        server.stop()


# ---- packet counts (found by realdata/cfs_faults.py) -----------------------------------------------


def test_packet_counts_come_from_each_packets_own_received_count(monkeypatch):
    """COSMOS's global counter (get_tlm_cnts) is synced about once a second in batches: packets that arrived
    just before a wait started were counted a moment later as new, and the values read next were the old
    ones. Each packet's RECEIVED_COUNT moves with its values; the global counter is only the fallback."""
    import types

    from flow_automator.cosmos_backend import CosmosBackend

    calls = {}

    def get_tlm_values(items, scope):
        calls["values"] = items
        table = {"CFS__ES_HK__RECEIVED_COUNT__RAW": [1462, None], "INST__NEVER__RECEIVED_COUNT__RAW": [None, None]}
        return [table[i] for i in items]

    def get_tlm_cnts(packets, scope):
        calls["cnts"] = packets
        return [0 for _ in packets]

    api = types.ModuleType("openc3.api.tlm_api")
    api.get_tlm_values, api.get_tlm_cnts = get_tlm_values, get_tlm_cnts
    for name in ("openc3", "openc3.api"):
        monkeypatch.setitem(sys.modules, name, sys.modules.get(name) or types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "openc3.api.tlm_api", api)

    backend = CosmosBackend.__new__(CosmosBackend)
    backend.scope = "DEFAULT"
    assert backend.tlm_counts([("CFS", "ES_HK"), ("INST", "NEVER")]) == [1462, 0]
    assert calls["values"] == ["CFS__ES_HK__RECEIVED_COUNT__RAW", "INST__NEVER__RECEIVED_COUNT__RAW"]
    assert calls["cnts"] == [["INST", "NEVER"]]  # only the never-received one falls back
