"""Storage format versions, Valkey outages, and backup/restore (EVALUATION addendum A-1 to A-3)."""

import json

import pytest
from conftest import FakeBackend, ListLogger, edge, make_flow, node
from flow_automator.backup import KIND, make_backup, restore_backup
from flow_automator.engine import EngineError, FlowEngine
from flow_automator.http_api import FlowApi
from flow_automator.migrations import FORMAT_VERSION, NewerFormatError, upgrade
from flow_automator.store import FlowStore, MemoryRedis
from test_contacts import DC, EPOCH, ISS_1, ISS_2, Clock
from test_triggers import fake_authorize


FLOWS_KEY = "flow_automator__DEFAULT__flows"


def simple_flow(name="Flow"):
    return make_flow([node("t", "trigger_manual"), node("l", "log", message="hi")], [edge("t", "l")], name=name)


class FlakyRedis(MemoryRedis):
    """MemoryRedis that refuses every call while ``down``."""

    def __init__(self):
        super().__init__()
        self.down = False

    def _check(self):
        if self.down:
            raise ConnectionError("Connection refused")

    def hgetall(self, key):
        self._check()
        return super().hgetall(key)

    def hset(self, key, field, value):
        self._check()
        super().hset(key, field, value)

    def lpush(self, key, value):
        self._check()
        super().lpush(key, value)

    def ltrim(self, key, start, end):
        self._check()
        super().ltrim(key, start, end)

    def lrange(self, key, start, end):
        self._check()
        return super().lrange(key, start, end)


def new_engine(redis=None):
    engine = FlowEngine(FlowStore(redis or MemoryRedis(), "DEFAULT"), FakeBackend(), ListLogger(), trigger_period=0.05)
    engine.start(start_triggers=False)
    return engine


# ---- format versions -----------------------------------------------------------


def test_records_are_stamped_in_valkey_but_not_in_memory(engine):
    flow, _ = engine.save_flow(simple_flow())
    stored = json.loads(engine.store.redis.hgetall(FLOWS_KEY)[flow["id"]])
    assert stored["format"] == FORMAT_VERSION
    assert "format" not in engine.flows[flow["id"]]
    assert "format" not in engine.store.all_flows()[flow["id"]]
    assert engine.store.stored_format() == FORMAT_VERSION  # noted at start


def test_flows_from_before_formats_still_load():
    redis = MemoryRedis()
    old = {**simple_flow("Old"), "id": "old1", "armed": False, "version": 3}
    redis.hset(FLOWS_KEY, "old1", json.dumps(old))  # no "format": written by 1.9.0 or earlier
    engine = new_engine(redis)
    try:
        assert engine.flows["old1"]["name"] == "Old"
        assert engine.store.unreadable == {}
    finally:
        engine.shutdown()


def test_records_in_a_newer_format_are_left_alone_and_reported():
    redis = MemoryRedis()
    newer = {**simple_flow("From the future"), "id": "new1", "format": FORMAT_VERSION + 1}
    redis.hset(FLOWS_KEY, "new1", json.dumps(newer))
    redis.hset("flow_automator__DEFAULT__meta", "format", str(FORMAT_VERSION + 1))
    engine = new_engine(redis)
    try:
        assert "new1" not in engine.flows
        assert engine.store.health()["unreadable"] == [{"kind": "flow", "id": "new1", "format": FORMAT_VERSION + 1}]
        assert any(s["event"] == "storage_format" for s in engine.alerts.sent)
        # It's still there, untouched, and the meta key isn't lowered
        assert json.loads(redis.hgetall(FLOWS_KEY)["new1"])["format"] == FORMAT_VERSION + 1
        assert engine.store.stored_format() == FORMAT_VERSION + 1
    finally:
        engine.shutdown()


def test_importing_a_flow_from_a_newer_version_is_refused(engine):
    with pytest.raises(EngineError, match="newer Flow Automator"):
        engine.save_flow({**simple_flow(), "format": FORMAT_VERSION + 1})
    flow, _ = engine.save_flow({**simple_flow(), "format": 0})  # older exports upgrade
    assert "format" not in flow


def test_upgrade_runs_each_step_in_order(monkeypatch):
    from flow_automator import migrations

    monkeypatch.setattr(migrations, "FORMAT_VERSION", 3)
    monkeypatch.setitem(migrations.STEPS, "thing", {1: lambda r: {"b": r["a"]}, 2: lambda r: {**r, "c": r["b"] * 2}})
    assert migrations.upgrade("thing", {"a": 2, "format": 1}) == {"b": 2, "c": 4}
    with pytest.raises(NewerFormatError):
        migrations.upgrade("thing", {"format": 4})


def test_saved_versions_upgrade_the_flow_inside_them(monkeypatch):
    from flow_automator import migrations

    monkeypatch.setattr(migrations, "FORMAT_VERSION", 2)
    monkeypatch.setitem(migrations.STEPS, "flow", {1: lambda f: {**f, "renamed": f.get("old")}})
    entry = migrations.upgrade("version", {"version": 4, "format": 1, "flow": {"old": "x"}})
    assert entry == {"version": 4, "flow": {"old": "x", "renamed": "x"}}


def test_upgrade_leaves_current_records_unchanged():
    record = {"id": "x", "nodes": [], "format": FORMAT_VERSION}
    assert upgrade("flow", record) == {"id": "x", "nodes": []}


# ---- Valkey outages ------------------------------------------------------------------


def test_history_and_audit_survive_an_outage_and_land_in_order():
    redis = FlakyRedis()
    engine = new_engine(redis)
    try:
        redis.down = True
        for n in range(3):
            engine.store.add_run("f1", {"id": f"r{n}"})
        engine.audit.record(None, "did", "thing")
        health = engine.store.health()
        assert not health["ok"] and health["pending_writes"] == 4 and health["error"]
        engine._note_store_health()
        assert any(s["event"] == "storage_unavailable" for s in engine.alerts.sent)

        redis.down = False
        assert engine.store.flush() == 0
        assert [r["id"] for r in engine.store.runs("f1")] == ["r2", "r1", "r0"]
        assert engine.store.audit_entries()[0]["action"] == "did"
        engine._note_store_health()
        assert any(s["event"] == "storage_recovered" for s in engine.alerts.sent)
        assert engine.store.health()["ok"]
    finally:
        engine.shutdown()


def test_planner_records_are_retried_but_flow_saves_fail_visibly():
    redis = FlakyRedis()
    engine = new_engine(redis)
    try:
        redis.down = True
        station = engine.planner.save_station(DC)  # in memory, written later
        assert engine.store.health()["pending_writes"] == 1
        with pytest.raises(ConnectionError):
            engine.save_flow(simple_flow())
        redis.down = False
        engine.store.flush()
        assert station["id"] in engine.store.records("stations")
    finally:
        engine.shutdown()


def test_a_full_buffer_drops_the_oldest_and_counts_them(monkeypatch):
    from flow_automator import store as store_module

    monkeypatch.setattr(store_module, "MAX_PENDING_WRITES", 3)
    redis = FlakyRedis()
    store = FlowStore(redis, "DEFAULT")
    redis.down = True
    for n in range(5):
        store.add_run("f", {"id": n})
    assert store.health()["pending_writes"] == 3 and store.health()["dropped_writes"] == 2
    redis.down = False
    store.flush()
    assert [r["id"] for r in store.runs("f")] == [4, 3, 2]


def test_health_endpoint_says_when_storage_is_degraded():
    redis = FlakyRedis()
    engine = new_engine(redis)
    try:
        api = FlowApi(engine, fake_authorize)
        assert api.handle("GET", "/flow-automator/api/health", {}, {}, "") == (200, {"ok": True, "store": "ok"})
        redis.down = True
        engine.store.add_run("f", {"id": 1})
        assert api.handle("GET", "/flow-automator/api/health", {}, {}, "")[1]["store"] == "degraded"
    finally:
        engine.shutdown()


# ---- backup and restore ------------------------------------------------------------------


def populated(engine):
    engine.planner.clock = Clock(EPOCH)
    flow, _ = engine.save_flow(simple_flow("Plain"))
    engine.save_flow({**engine.flows[flow["id"]], "description": "second version"}, flow["id"])
    engine.set_armed(flow["id"], True)
    template, _ = engine.save_flow(
        make_flow(
            [node("t", "trigger_manual"), node("c", "send_cmd", command="INST COLLECT with TYPE NORMAL")],
            [edge("t", "c")],
            name="Pass template",
            template=True,
        )
    )
    sat = engine.planner.save_satellite({"name": "ISS", "target": "inst", "tle": f"ISS (ZARYA)\n{ISS_1}\n{ISS_2}"})
    station = engine.planner.save_station(DC)
    engine.planner.save_rule(
        {
            "name": "Every ISS pass",
            "template_id": template["id"],
            "satellite_ids": [sat["id"]],
            "station_ids": [station["id"]],
        }
    )
    engine.alerts.save_settings(
        {"webhooks": [{"id": "hook1", "name": "Ops chat", "url": "https://chat.example.com/hooks/SECRET"}]}
    )
    return flow, template, sat, station


def test_backup_round_trip_into_an_empty_scope(engine):
    flow, template, sat, station = populated(engine)
    backup = json.loads(json.dumps(make_backup(engine)))  # it must survive being a file
    assert backup["kind"] == KIND and backup["format"] == FORMAT_VERSION
    assert "SECRET" not in json.dumps(backup)
    assert all(not f["armed"] for f in backup["flows"])

    target = new_engine()
    try:
        target.planner.clock = Clock(EPOCH)
        result = restore_backup(target, backup, user={"username": "ops"})
        assert sorted(result["flows"]["created"]) == ["Pass template", "Plain"]
        assert result["satellites"]["created"] == ["ISS"] and result["stations"]["created"] == ["Washington"]
        assert result["rules"]["created"] == ["Every ISS pass"] and not result["rules"]["failed"]
        assert result["scheduler_paused"] and target.planner.settings["paused"]
        assert any("Webhooks aren't restored" in s and "Ops chat" in s for s in result["skipped"])

        restored = target.flows[flow["id"]]  # same id, so rules and sub-flow nodes still point at it
        assert restored["description"] == "second version"
        assert not restored["armed"] and "backup" in restored["disarmed_reason"]
        versions = [v["version"] for v in target.versions.list(flow["id"])]
        assert versions == [3, 2, 1]  # its history, plus the restore
        restored_sat = next(iter(target.planner.satellites.values()))
        assert restored_sat["tle"]["line1"] == ISS_1
        rule = next(iter(target.planner.rules.values()))
        assert rule["template_id"] == template["id"]
        assert rule["satellite_ids"] == [restored_sat["id"]]
    finally:
        target.shutdown()


def test_restoring_over_existing_flows_disarms_them_and_matches_by_name(engine):
    flow, _template, _sat, station = populated(engine)
    backup = make_backup(engine)
    backup["flows"][0]["description"] = "from the backup"
    backup["stations"][0]["id"] = "someoneelse"  # same name, different id: matched by name
    engine.set_armed(flow["id"], True)
    result = restore_backup(engine, backup)
    assert not engine.flows[flow["id"]]["armed"]
    assert result["stations"]["updated"] == ["Washington"] and len(engine.planner.stations) == 1
    assert len(engine.planner.rules) == 1  # matched, not duplicated


def test_restore_refuses_what_it_cannot_read(engine):
    api = FlowApi(engine, fake_authorize)
    path = "/flow-automator/api/restore"
    status, body = api.handle("POST", path, {}, {"Authorization": "good"}, json.dumps({"backup": {"flows": []}}))
    assert status == 400 and "isn't a Flow Automator backup" in body["error"]
    newer = {"kind": KIND, "format": FORMAT_VERSION + 1}
    status, body = api.handle("POST", path, {}, {"Authorization": "good"}, json.dumps({"backup": newer}))
    assert status == 400 and "newer" in body["error"]


def test_a_bad_item_is_reported_and_the_rest_is_restored(engine):
    backup = {
        "kind": KIND,
        "format": FORMAT_VERSION,
        "flows": [simple_flow("No id"), {**simple_flow("Good"), "id": "good1"}],
        "stations": [{**DC, "latitude": 200}],
        "tle_inputs": {"stash_key": "someone_elses_key"},
    }
    result = restore_backup(engine, backup)
    assert result["flows"]["created"] == ["Good"]
    assert result["flows"]["failed"][0]["name"] == "No id"
    assert result["stations"]["failed"]
    assert any("stash key" in s for s in result["skipped"])  # same checks as the API


def test_backup_and_restore_through_the_api(engine):
    populated(engine)
    api = FlowApi(engine, fake_authorize)
    status, backup = api.handle("GET", "/flow-automator/api/backup", {"history": ["1"]}, {"Authorization": "good"}, "")
    assert status == 200 and "audit" in backup and "runs" in backup
    status, result = api.handle(
        "POST", "/flow-automator/api/restore", {}, {"Authorization": "good"}, json.dumps({"backup": backup})
    )
    assert status == 200, result
    assert engine.audit.entries(kind="backup")[0]["action"] == "restored"
