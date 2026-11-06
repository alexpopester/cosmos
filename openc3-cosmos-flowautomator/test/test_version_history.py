"""Version history at scale (EVALUATION-2026-11-05 B46): metadata apart from flow bodies; deleted flows purged.

Before: a 2,000-node flow with 200 versions was one 92 MB list, listing versions took 2.2 s and an old
version or a diff 2.4 s, and a deleted flow's history was kept for ever.
"""

import json
import time

from conftest import FakeBackend, ListLogger
from flow_automator.engine import DELETED_HISTORY_DAYS, FlowEngine
from flow_automator.store import FlowStore, MemoryRedis


def big_flow(nodes=2000):
    return {
        "id": "big",
        "name": "Big",
        "nodes": [
            {"id": f"n{i}", "type": "log", "position": {"x": i, "y": 0}, "data": {"message": f"m{i}"}}
            for i in range(nodes)
        ],
        "edges": [],
    }


def entry(version, flow):
    return {
        "version": version,
        "saved_at": 1000.0 + version,
        "saved_by": "me",
        "note": "",
        "summary": "changed",
        "restored_from": None,
        "flow": {**flow, "version": version},
    }


def test_metadata_and_bodies_are_kept_apart():
    store = FlowStore(MemoryRedis(), "DEFAULT")
    store.add_version("f1", entry(1, {"id": "f1", "nodes": [1]}), 10)
    store.add_version("f1", entry(2, {"id": "f1", "nodes": [1, 2]}), 10)
    assert all("flow" not in json.loads(raw) for raw in store.redis.lists[store._versions_key("f1")])
    assert [v["version"] for v in store.version_list("f1")] == [2, 1]
    assert store.version_body("f1", 1)["nodes"] == [1]
    assert store.versions("f1", 1)[0]["flow"]["nodes"] == [1, 2]  # the latest, with its body


def test_trimming_drops_old_bodies():
    store = FlowStore(MemoryRedis(), "DEFAULT")
    for v in range(1, 6):
        store.add_version("f1", entry(v, {"id": "f1"}), 3)
    assert [v["version"] for v in store.version_list("f1")] == [5, 4, 3]
    assert set(store.redis.hashes[store._version_bodies_key("f1")]) == {"5", "4", "3"}


def test_old_versions_are_moved_once():
    redis = MemoryRedis()
    store = FlowStore(redis, "DEFAULT")
    for v in (1, 2):  # earlier layout: whole entries, newest first
        redis.lpush(store._versions_key("f1"), json.dumps(entry(v, {"id": "f1", "name": f"v{v}"})))
    assert store.migrate_version_history(["f1"]) == 2
    assert [v["version"] for v in store.version_list("f1")] == [2, 1]
    assert store.version_body("f1", 1)["name"] == "v1"
    assert store.migrate_version_history(["f1"]) == 0


def test_engine_versions_still_work_end_to_end(engine):
    flow, _ = engine.save_flow({"name": "F", "nodes": [], "edges": []})
    flow["description"] = "second"
    engine.save_flow(flow, flow_id=flow["id"])  # without flow_id it would create another flow
    versions = engine.list_versions(flow["id"])
    assert [v["version"] for v in versions] == [2, 1] and all("flow" not in v for v in versions)
    assert engine.get_version(flow["id"], 1)["flow"]["description"] == ""
    restored, _problems = engine.restore_version(flow["id"], 1)
    assert restored["version"] == 3


def test_listing_versions_is_fast_for_a_big_flow():
    """The bar: list_versions < 50 ms for a 2,000-node flow with 200 versions."""
    store = FlowStore(MemoryRedis(), "DEFAULT")
    flow = big_flow()
    for v in range(1, 201):
        store.add_version("big", entry(v, flow), 200)
    started = time.perf_counter()
    listed = store.version_list("big")
    elapsed = time.perf_counter() - started
    assert len(listed) == 200 and elapsed < 0.05, f"{elapsed * 1000:.0f} ms"
    started = time.perf_counter()
    oldest = store.version_body("big", 1)
    assert len(oldest["nodes"]) == 2000 and time.perf_counter() - started < 0.5


def test_a_deleted_flows_history_is_purged_after_the_grace_period(engine):
    flow, _ = engine.save_flow({"name": "Gone", "nodes": [], "edges": []})
    engine.store.add_run(flow["id"], {"id": "r1", "flow_id": flow["id"], "status": "success", "steps": []})
    engine.delete_flow(flow["id"])
    assert engine.purge_deleted() == 0  # within the grace period: still restorable
    assert engine.store.version_list(flow["id"])
    later = time.time() + DELETED_HISTORY_DAYS * 86400 + 60
    assert engine.purge_deleted(now=later) == 1
    assert engine.store.version_list(flow["id"]) == [] and engine.store.find_run("r1") is None
    assert engine.list_deleted() == []


def test_a_restored_flow_keeps_its_history(engine):
    flow, _ = engine.save_flow({"name": "Back", "nodes": [], "edges": []})
    engine.delete_flow(flow["id"])
    engine.restore_deleted(flow["id"])
    engine.purge_deleted(now=time.time() + DELETED_HISTORY_DAYS * 86400 + 60)
    assert engine.store.version_list(flow["id"])


def test_an_engine_moves_old_versions_when_it_starts():
    redis = MemoryRedis()
    store = FlowStore(redis, "DEFAULT")
    store.save_flow({"id": "f1", "name": "F", "nodes": [], "edges": [], "version": 1, "updated_at": 1.0})
    redis.lpush(store._versions_key("f1"), json.dumps(entry(1, {"id": "f1", "name": "F", "nodes": [], "edges": []})))
    engine = FlowEngine(FlowStore(redis, "DEFAULT"), FakeBackend(), ListLogger(), trigger_period=0.05)
    engine.start(start_triggers=False)
    try:
        assert engine.get_version("f1", 1)["flow"]["name"] == "F"
    finally:
        engine.shutdown()
