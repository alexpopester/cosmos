"""Variables a flow remembers between its runs (Set variable, "Remember between runs"; DECISIONS D31).

Found by the real-data tests: runs had no memory, so a post-pass check couldn't say "not heard for three
passes" or notice a command counter that moved with no uplink.
"""

from conftest import FakeBackend, ListLogger, edge, make_flow, node, run_to_end, wait_until
from flow_automator.engine import MAX_MEMORY_BYTES, FlowEngine
from flow_automator.http_api import FlowApi
from flow_automator.store import FlowStore


def counter_flow(persist=True):
    return make_flow(
        [
            node("t", "trigger_manual"),
            node(
                "count",
                "set_var",
                name="misses",
                expression="var('misses', 0) + 1",
                persist=persist,
                more={"last": "'run ' + str(misses)"},
            ),
        ],
        [edge("t", "count")],
        name="Counter",
    )


def run_saved(engine, flow_id, **kwargs):
    """Run an already saved flow (run_to_end saves a new copy, with a new id, every time)."""
    result = engine.trigger(flow_id, manual=True, **kwargs)
    run_id = result["run"]["id"]
    assert wait_until(lambda: run_id not in engine.runs, 5.0)
    return engine.get_run(run_id)


def test_a_flow_remembers_between_runs(engine):
    saved, _ = engine.save_flow(counter_flow())
    for expected in (1, 2, 3):
        run = run_saved(engine, saved["id"])
        assert run["status"] == "success" and f"misses = {expected}" in run["steps"][-1]["message"]
        assert "(remembered)" in run["steps"][-1]["message"]
    assert engine.flow_memory(saved["id"]) == {"misses": 3, "last": "run 3"}


def test_memory_survives_an_engine_restart(engine):
    saved, _ = engine.save_flow(counter_flow())
    run_saved(engine, saved["id"])
    engine.store.flush()
    again = FlowEngine(engine.store, FakeBackend(), ListLogger(), trigger_period=0.05)
    again.start(start_triggers=False)
    try:
        assert again.flow_memory(saved["id"]) == {"misses": 1, "last": "run 1"}
    finally:
        again.shutdown()


def test_what_a_run_is_started_with_wins_over_memory(engine):
    saved, _ = engine.save_flow(counter_flow())
    run_saved(engine, saved["id"])
    run = run_saved(engine, saved["id"], variables={"misses": 10})
    assert "misses = 11" in run["steps"][-1]["message"]


def test_without_remember_nothing_is_kept(engine):
    saved, _ = engine.save_flow(counter_flow(persist=False))
    run_saved(engine, saved["id"])
    run = run_saved(engine, saved["id"])
    assert "misses = 1" in run["steps"][-1]["message"]
    assert engine.flow_memory(saved["id"]) == {}


def test_a_dry_run_remembers_nothing(engine):
    saved, _ = engine.save_flow(counter_flow())
    run = run_saved(engine, saved["id"], dry_run=True)
    assert "dry run: not remembered" in run["steps"][-1]["message"]
    assert engine.flow_memory(saved["id"]) == {}


def test_memory_is_bounded(engine):
    flow = make_flow(
        [
            node("t", "trigger_manual"),
            node("big", "set_var", name="blob", expression=f"'x' * {MAX_MEMORY_BYTES}", persist=True),
        ],
        [edge("t", "big")],
    )
    run = run_to_end(engine, flow)
    assert run["status"] == "failed" and "memory over" in (run["steps"][-1].get("message") or run.get("message"))
    assert engine.flow_memory(run["flow_id"]) == {}


def test_memory_api_and_deleting_the_flow(engine):
    api = FlowApi(engine, lambda token, write: None)
    saved, _ = engine.save_flow(counter_flow())
    run_saved(engine, saved["id"])
    assert api.get_memory(saved["id"]) == {"memory": {"misses": 1, "last": "run 1"}}
    assert api.forget_memory(saved["id"], user=None) == {"memory": {}}
    assert engine.flow_memory(saved["id"]) == {}
    run_saved(engine, saved["id"])
    engine.delete_flow(saved["id"])
    assert engine.flow_memory(saved["id"]) == {}
    assert saved["id"] not in FlowStore(engine.store.redis, "DEFAULT").records("flow_memory")
