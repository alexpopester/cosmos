"""Hooks for other apps to provide TLEs: bulk API, stash, files, telemetry, and the safeguards."""

import json

import pytest
from flow_automator.http_api import FlowApi
from flow_automator.orbit import make_tle
from flow_automator.tle_inputs import split_tles


pytest.importorskip("sgp4")

T0 = 1793700000.0  # 2026-11-03
REVS = 15.5


def tle(catalog, epoch, raan=200.0, name="", inclination=51.6):
    # Same orbit at a later epoch: advance the mean anomaly by the time passed, so it's a consistent update
    mean_anomaly = 10.0 + 360.0 * REVS * (epoch - T0) / 86400
    return make_tle(epoch, inclination, raan, mean_anomaly, REVS, catalog=catalog, name=name)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def planner(engine):
    planner = engine.planner
    planner.clock = Clock(T0 + 3600)
    planner.horizon = 86400
    return planner


@pytest.fixture
def sats(planner):
    a = planner.save_satellite({"name": "ALPHA", "tle": tle(90011, T0)})
    b = planner.save_satellite({"name": "BRAVO", "catalog": 90012})
    return a, b


def results(outcome):
    return [(r["satellite"], r["result"]) for r in outcome["results"]]


def test_split_tles():
    two = tle(1, T0)
    three = tle(2, T0, name="SAT TWO")
    named0 = "0 SAT THREE\n" + tle(3, T0)
    entries, problems = split_tles(f"{two}\n\n{three}\r\n{named0}\njunk line")
    assert [e["name"] for e in entries] == ["", "SAT TWO", "SAT THREE"]
    assert problems == ["'junk line' isn't followed by a TLE"]
    assert split_tles("1 only a first line")[1] == ["Line 1: a TLE line without its pair"]


def test_bulk_ingest_matches_by_catalog_then_name(planner, sats):
    outcome = planner.intake.ingest(
        "\n".join([tle(90011, T0 + 1800), tle(99999, T0, name="BRAVO"), tle(90013, T0, name="NOBODY"), "1 bad\n2 bad"]),
        "fds",
        user="alice",
    )
    assert results(outcome) == [("ALPHA", "applied"), ("BRAVO", "rejected"), (None, "unknown"), (None, "rejected")]
    assert "doesn't match the satellite's 90012" in outcome["results"][1]["reason"]  # BRAVO's catalog is set
    assert planner.satellites[sats[0]["id"]]["tle"]["source"] == "fds"
    assert planner.satellites[sats[0]["id"]]["tle"]["applied_by"] == "alice"
    assert outcome["counts"]["applied"] == 1 and outcome["counts"]["unknown"] == 1

    outcome = planner.intake.ingest(tle(90012, T0, name="whatever"), "fds")
    assert results(outcome) == [("BRAVO", "applied")]  # catalog number, not name
    assert any(e["action"] == "ingested TLEs" for e in planner.engine.audit.entries())


def test_older_and_same_tles_are_skipped(planner, sats):
    alpha = sats[0]["id"]
    assert results(planner.intake.ingest(tle(90011, T0), "fds")) == [("ALPHA", "unchanged")]
    assert results(planner.intake.ingest(tle(90011, T0 - 3600), "fds")) == [("ALPHA", "skipped")]
    assert planner.satellites[alpha]["tle"]["epoch"] == pytest.approx(T0)
    # Pasting in the tool is the operator's call: an older TLE applies
    planner.set_tle(alpha, tle(90011, T0 - 3600), "manual")
    assert planner.satellites[alpha]["tle"]["epoch"] == pytest.approx(T0 - 3600)


def test_big_jumps_are_held_for_review(planner, sats, engine):
    alpha = sats[0]["id"]
    sent = []
    engine.alerts.notify = lambda level, message, url: sent.append((level, message))
    outcome = planner.intake.ingest(tle(90011, T0 + 1800, raan=230.0), "fds")
    assert results(outcome) == [("ALPHA", "held")]
    assert "km from where the current TLE has it" in outcome["results"][0]["reason"]
    sat = planner.satellites[alpha]
    assert sat["held_tle"]["source"] == "fds" and sat["tle"]["epoch"] == pytest.approx(T0)
    assert sent and sent[0][0] == "alert" and "held for review" in sent[0][1]

    planner.intake.reject_held(alpha)
    assert "held_tle" not in planner.satellites[alpha]
    planner.intake.ingest(tle(90011, T0 + 1800, raan=230.0), "fds")
    held = planner.intake.accept_held(alpha, user="alice")
    assert planner.satellites[alpha]["tle"]["epoch"] == pytest.approx(held["epoch"])
    assert planner.satellites[alpha]["tle"]["applied_by"] == "alice"


def test_preferred_source_wins_while_fresh(planner, sats):
    alpha = sats[0]["id"]
    planner.save_satellite({"preferred_source": "fds"}, alpha)
    planner.intake.ingest(tle(90011, T0 + 600), "fds")
    outcome = planner.intake.ingest(tle(90011, T0 + 1200), "celestrak")
    assert results(outcome) == [("ALPHA", "skipped")] and "preferred source (fds)" in outcome["results"][0]["reason"]
    planner.clock.now = T0 + 5 * 86400  # the fds TLE has gone stale: the fallback applies
    # (the synthetic 4-days-later TLE ignores the plane's drift, so it would also trip the jump check)
    planner.intake.settings["max_jump_km"] = 100000
    outcome = planner.intake.ingest(tle(90011, T0 + 4 * 86400), "celestrak")
    assert results(outcome) == [("ALPHA", "applied")]


def test_stash_input(planner, sats, backend):
    backend.stash["flow_automator_tles"] = {"tle": tle(90011, T0 + 1800), "source": "my-pipeline"}
    planner.intake.poll(planner.clock())
    assert "flow_automator_tles" not in backend.stash
    result = backend.stash["flow_automator_tles_result"]
    assert result["counts"]["applied"] == 1 and result["results"][0]["satellite"] == "ALPHA"
    assert planner.satellites[sats[0]["id"]]["tle"]["source"] == "my-pipeline"
    # A list, and plain text
    backend.stash["flow_automator_tles"] = [tle(90012, T0), tle(90011, T0 + 3000)]
    planner.intake.last_poll = 0
    planner.intake.poll(planner.clock())
    assert backend.stash["flow_automator_tles_result"]["counts"]["applied"] == 2
    assert planner.intake.status["stash"]["counts"]["applied"] == 2


def test_watched_file_input(planner, sats, backend):
    planner.intake.save_settings({"files": ["INST/tle/latest.tle"], "poll_seconds": 5})
    planner.intake.poll(planner.clock())
    assert planner.intake.status["files"]["INST/tle/latest.tle"]["error"] == "No such file yet"
    backend.files["INST/tle/latest.tle"] = tle(90011, T0 + 1800, name="ALPHA") + "\n" + tle(90012, T0)
    planner.clock.now += 10
    planner.intake.poll(planner.clock())
    assert planner.intake.status["files"]["INST/tle/latest.tle"]["counts"]["applied"] == 2
    assert planner.satellites[sats[0]["id"]]["tle"]["source"] == "file:INST/tle/latest.tle"
    # Unchanged file: not read again
    before = planner.intake.status["files"]["INST/tle/latest.tle"]["time"]
    planner.clock.now += 10
    planner.intake.poll(planner.clock())
    assert planner.intake.status["files"]["INST/tle/latest.tle"]["time"] == before


def test_telemetry_input(planner, sats, backend):
    lines = tle(90011, T0 + 1800).split("\n")
    backend.values.update({"GND ORBIT LINE1": lines[0], "GND ORBIT LINE2": lines[1]})
    backend.counts[("GND", "ORBIT")] = 4
    planner.intake.save_settings(
        {"packets": [{"target": "gnd", "packet": "orbit", "line1": "line1", "line2": "line2"}]}
    )
    planner.intake.poll(planner.clock())
    assert planner.satellites[sats[0]["id"]]["tle"]["source"] == "tlm:GND ORBIT"
    # Same count: not read again; a new packet with the same TLE is just unchanged
    planner.clock.now += 60
    planner.intake.poll(planner.clock())
    backend.counts[("GND", "ORBIT")] = 5
    planner.clock.now += 60
    planner.intake.poll(planner.clock())
    assert planner.intake.status["packets"]["GND ORBIT"]["counts"]["unchanged"] == 1


def test_settings_are_checked(planner):
    from flow_automator.tle_inputs import TleInputError

    with pytest.raises(TleInputError, match="target paths"):
        planner.intake.save_settings({"files": ["../etc/passwd"]})
    with pytest.raises(TleInputError, match="needs a target"):
        planner.intake.save_settings({"packets": [{"target": "GND"}]})
    with pytest.raises(TleInputError, match="max_jump_km"):
        planner.intake.save_settings({"max_jump_km": 0})
    saved = planner.intake.save_settings({"max_jump_km": 250, "stash_key": "tles_in"})
    assert saved["max_jump_km"] == 250 and saved["stash_key"] == "tles_in"
    planner.intake.settings = {}
    planner.intake.load()
    assert planner.intake.settings["stash_key"] == "tles_in"


def test_http_hooks(engine, planner, sats):
    api = FlowApi(engine, lambda token, write: {"username": "pipeline"})

    def call(method, path, body=None):
        return api.handle(method, f"/flow-automator/api{path}", {}, {}, json.dumps(body) if body else "")

    status, body = call("POST", "/tles", {"tle": tle(90011, T0 + 1800) + "\n" + tle(90012, T0), "source": "fds"})
    assert status == 200 and body["counts"]["applied"] == 2
    assert call("POST", "/tles", {"tle": ""})[0] == 400
    # The per-satellite endpoint: non-manual sources get the safeguards and say what happened
    status, body = call("POST", f"/satellites/{sats[0]['id']}/tle", {"tle": tle(90011, T0), "source": "fds"})
    assert status == 200 and body["result"]["result"] == "skipped"
    status, body = call(
        "POST", f"/satellites/{sats[0]['id']}/tle", {"tle": tle(90011, T0 + 3000, raan=240), "source": "fds"}
    )
    assert body["result"]["result"] == "held" and body["satellite"]["held_tle"]
    assert call("POST", f"/satellites/{sats[0]['id']}/tle/accept", {})[0] == 200
    assert call("POST", f"/satellites/{sats[0]['id']}/tle/reject", {})[0] == 400  # nothing held now
    assert call("PUT", "/tle_inputs", {"max_jump_km": -5})[0] == 400
    assert call("GET", "/tle_inputs")[1]["stash_key"] == "flow_automator_tles"
