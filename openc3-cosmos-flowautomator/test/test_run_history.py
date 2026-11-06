"""Run history at scale (EVALUATION-2026-11-05 B44): summaries apart from steps, an index, a cache.

Before: every list read every run with all its steps (1.4 s at 20 flows x 200 runs x 100 steps, 18 s at
1,000 steps), a run not in memory was found by scanning every flow's history, and the UI asks every second.
"""

import json
import time

from conftest import FakeBackend, ListLogger
from flow_automator.engine import FlowEngine
from flow_automator.store import FlowStore, MemoryRedis


def run_record(flow_id, n, steps=3, started=None):
    return {
        "id": f"{flow_id}-run{n}",
        "flow_id": flow_id,
        "flow_name": f"Flow {flow_id}",
        "status": "success",
        "started_at": started if started is not None else 1000.0 + n,
        "ended_at": 1001.0 + n,
        "trigger_info": {"kind": "manual"},
        "steps": [{"node": f"n{i}", "status": "done", "message": f"step {i} of run {n}"} for i in range(steps)],
        "step_count": steps,
    }


def test_summaries_steps_and_index_are_kept_apart():
    store = FlowStore(MemoryRedis(), "DEFAULT", history_limit=10)
    store.add_run("f1", run_record("f1", 1, steps=5))
    raw = json.loads(store.redis.lists[store._runs_key("f1")][0])
    assert "steps" not in raw and raw["id"] == "f1-run1"  # the list holds the summary only
    assert len(store.run_steps("f1", "f1-run1")) == 5
    assert store.find_run("f1-run1")["steps"][4]["message"] == "step 4 of run 1"
    assert store.find_run("nope") is None
    assert store.runs("f1")[0]["steps"][0]["node"] == "n0"  # backups still get whole runs


def test_trimming_drops_the_old_runs_steps_and_index_entries():
    store = FlowStore(MemoryRedis(), "DEFAULT", history_limit=3)
    for n in range(5):
        store.add_run("f1", run_record("f1", n))
    assert [r["id"] for r in store.run_summaries("f1")] == ["f1-run4", "f1-run3", "f1-run2"]
    steps = store.redis.hashes[store._steps_key("f1")]
    index = store.redis.hashes[store._run_index_key]
    assert set(steps) == set(index) == {"f1-run4", "f1-run3", "f1-run2"}
    assert store.find_run("f1-run0") is None


def test_the_cache_follows_new_runs_and_a_fresh_store_reads_valkey():
    redis = MemoryRedis()
    store = FlowStore(redis, "DEFAULT", history_limit=5)
    store.add_run("f1", run_record("f1", 1))
    assert [r["id"] for r in store.run_summaries("f1")] == ["f1-run1"]  # loads the cache
    store.add_run("f1", run_record("f1", 2))
    assert [r["id"] for r in store.run_summaries("f1")] == ["f1-run2", "f1-run1"]  # updated in memory
    assert [r["id"] for r in FlowStore(redis, "DEFAULT", history_limit=5).run_summaries("f1")] == [
        "f1-run2",
        "f1-run1",
    ]


def test_old_history_is_moved_once():
    redis = MemoryRedis()
    old = FlowStore(redis, "DEFAULT", history_limit=10)
    for n in range(3):  # what earlier versions wrote: whole runs, newest first
        redis.lpush(old._runs_key("f1"), json.dumps(run_record("f1", n, steps=4)))
    store = FlowStore(redis, "DEFAULT", history_limit=10)
    assert store.migrate_run_history(["f1", "gone"]) == 3
    assert [r["id"] for r in store.run_summaries("f1")] == ["f1-run2", "f1-run1", "f1-run0"]
    assert all("steps" not in json.loads(raw) for raw in redis.lists[store._runs_key("f1")])
    assert len(store.find_run("f1-run0")["steps"]) == 4
    assert store.migrate_run_history(["f1"]) == 0  # done: marked in the meta hash


def test_an_engine_moves_old_history_when_it_starts():
    redis = MemoryRedis()
    engine = FlowEngine(FlowStore(redis, "DEFAULT"), FakeBackend(), ListLogger(), trigger_period=0.05)
    engine.start(start_triggers=False)
    flow, _ = engine.save_flow({"name": "F", "nodes": [], "edges": []})
    engine.shutdown()
    redis.lpush(engine.store._runs_key(flow["id"]), json.dumps(run_record(flow["id"], 1, steps=2)))
    redis.hdel(engine.store.meta_key, "runs_layout")
    again = FlowEngine(FlowStore(redis, "DEFAULT"), FakeBackend(), ListLogger(), trigger_period=0.05)
    again.start(start_triggers=False)
    try:
        assert len(again.get_run(f"{flow['id']}-run1")["steps"]) == 2
        assert [r["id"] for r in again.list_runs()] == [f"{flow['id']}-run1"]
    finally:
        again.shutdown()


class FlakyRedis(MemoryRedis):
    down = False

    def lpush(self, key, value):
        if self.down:
            raise ConnectionError("Valkey is away")
        super().lpush(key, value)


def test_a_run_written_during_an_outage_lands_once_valkey_is_back():
    redis = FlakyRedis()
    store = FlowStore(redis, "DEFAULT", history_limit=10)
    redis.down = True
    store.add_run("f1", run_record("f1", 1))
    assert store.health()["pending_writes"] == 1
    redis.down = False
    assert store.flush() == 0
    fresh = FlowStore(redis, "DEFAULT", history_limit=10)
    assert [r["id"] for r in fresh.run_summaries("f1")] == ["f1-run1"]
    assert len(fresh.find_run("f1-run1")["steps"]) == 3


def test_listing_runs_is_fast_at_scale(engine):
    """The evaluation's bar: list_runs over every flow < 50 ms at 20 flows x 200 runs (B44)."""
    engine.store.history_limit = 200
    flows = [engine.save_flow({"name": f"F{i}", "nodes": [], "edges": []})[0]["id"] for i in range(20)]
    for flow_id in flows:
        for n in range(200):
            engine.store.add_run(flow_id, run_record(flow_id, n, steps=20, started=1000.0 + n))
    engine.list_runs(limit=50)  # first call fills the cache, as the UI's first poll would
    started = time.perf_counter()
    runs = engine.list_runs(limit=50)
    elapsed = time.perf_counter() - started
    assert len(runs) == 50 and all("steps" not in r for r in runs)
    assert elapsed < 0.05, f"list_runs took {elapsed * 1000:.0f} ms"
    started = time.perf_counter()
    found = engine.get_run(f"{flows[7]}-run3")
    assert time.perf_counter() - started < 0.01 and len(found["steps"]) == 20


def test_get_run_since_returns_only_the_new_steps(engine):
    """B47: a polling run log asks from its first unfinished step instead of fetching every step."""
    from conftest import edge, make_flow, node, run_to_end  # noqa: PLC0415

    nodes = [node("t", "trigger_manual")] + [node(f"l{i}", "log", message=f"line {i}") for i in range(6)]
    edges = [edge("t", "l0")] + [edge(f"l{i}", f"l{i + 1}") for i in range(5)]
    run = run_to_end(engine, make_flow(nodes, edges))
    whole = engine.get_run(run["id"])
    assert len(whole["steps"]) == 7 and "steps_from" not in whole
    tail = engine.get_run(run["id"], since=4)  # from history
    assert tail["steps_from"] == 4 and [s["node"] for s in tail["steps"]] == ["l3", "l4", "l5"]
    assert engine.get_run(run["id"], since=99)["steps"] == []


def test_a_running_run_copies_only_the_steps_asked_for(engine):
    from flow_automator.runner import FlowRun  # noqa: PLC0415

    flow, _ = engine.save_flow({"name": "F", "nodes": [], "edges": []})
    run = FlowRun(engine, flow, None)
    run.steps = [{"node": f"n{i}", "ended_at": 1} for i in range(5)]
    run.steps_dropped = 10  # it already let go of 10
    sliced = run.as_json(since=12)
    assert sliced["steps_from"] == 12 and [s["node"] for s in sliced["steps"]] == ["n2", "n3", "n4"]
    assert run.as_json(since=3)["steps_from"] == 10  # older than what it keeps: from the oldest kept
