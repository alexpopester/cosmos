"""Runner integrity fixes from docs/EVALUATION-2026-11-05.md (backlog B1, B2, B12)."""

import pytest
from conftest import edge, make_flow, node, run_to_end, wait_until
from flow_automator import runner
from flow_automator.graph import Graph, validate_flow


def _two_branch_loops():
    return make_flow(
        [
            node("t", "trigger_manual"),
            node("p", "parallel"),
            node("la", "loop", mode="count", count=3, variable="a"),
            node("ca", "send_cmd", command="INST A"),
            node("lb", "loop", mode="count", count=3, variable="b"),
            node("cb", "send_cmd", command="INST B"),
            node("j", "join"),
        ],
        [
            edge("t", "p"),
            edge("p", "la"),
            edge("p", "lb"),
            edge("la", "ca", "body"),
            edge("ca", "la"),
            edge("la", "j", "done"),
            edge("lb", "cb", "body"),
            edge("cb", "lb"),
            edge("lb", "j", "done"),
        ],
    )


# B1 (R-1, E-4): loops in sibling branches used to reset each other and send extra commands
def test_loops_in_parallel_branches_count_independently(engine, backend):
    for _ in range(20):
        backend.commands.clear()
        record = run_to_end(engine, _two_branch_loops())
        assert record["status"] == "success", record["message"]
        assert backend.commands.count("INST A") == 3
        assert backend.commands.count("INST B") == 3


def test_loop_inside_a_branch_inside_an_outer_loop_restarts_each_outer_iteration(engine, backend):
    flow = make_flow(
        [
            node("t", "trigger_manual"),
            node("outer", "loop", mode="count", count=2, variable="o"),
            node("p", "parallel"),
            node("la", "loop", mode="count", count=2, variable="a"),
            node("ca", "send_cmd", command="INST A"),
            node("x", "log", message="x"),
            node("j", "join"),
        ],
        [
            edge("t", "outer"),
            edge("outer", "p", "body"),
            edge("p", "la"),
            edge("p", "x"),
            edge("la", "ca", "body"),
            edge("ca", "la"),
            edge("la", "j", "done"),
            edge("x", "j"),
            edge("j", "outer"),
        ],
    )
    record = run_to_end(engine, flow)
    assert record["status"] == "success", record["message"]
    assert backend.commands == ["INST A"] * 4


# B2 (E-2): sibling Parallels sharing a Join, with no enclosing Parallel that owns it
def _siblings_share_join():
    return make_flow(
        [
            node("t", "trigger_manual"),
            node("p1", "parallel"),
            node("p2", "parallel"),
            node("p3", "parallel"),
            node("x1", "log", message="x"),
            node("x2", "log", message="x"),
            node("y1", "log", message="y"),
            node("y2", "log", message="y"),
            node("j", "join"),
            node("after", "send_cmd", command="INST AFTER"),
        ],
        [
            edge("t", "p1"),
            edge("p1", "p2"),
            edge("p1", "p3"),
            edge("p2", "x1"),
            edge("p2", "x2"),
            edge("p3", "y1"),
            edge("p3", "y2"),
            edge("x1", "j"),
            edge("x2", "j"),
            edge("y1", "j"),
            edge("y2", "j"),
            edge("j", "after"),
        ],
    )


def test_nested_parallels_sharing_a_join_without_an_owner_are_refused(engine, backend):
    problems = validate_flow(_siblings_share_join())
    assert [p["node"] for p in problems] == ["j"]
    assert "more than once" in problems[0]["message"]
    result = engine.save_flow(_siblings_share_join())[1]
    assert result and result[0]["node"] == "j"


def _owned_shared_join(padding=0):
    """p2 and p3 hand the Join back to p1, which also reaches it through z: valid, tail runs once."""
    nodes = [node("px", "parallel"), node("jx", "join")]
    edges = []
    prev = "px"
    for i in range(padding):
        nodes.append(node(f"c{i}", "log", message="x"))
        edges.append(edge(prev, f"c{i}"))
        prev = f"c{i}"
    if padding:
        edges += [edge("px", "jx"), edge(prev, "jx")]
    else:
        nodes = []
    nodes += [
        node("t", "trigger_manual"),
        node("p1", "parallel"),
        node("p2", "parallel"),
        node("p3", "parallel"),
        node("x1", "log", message="x"),
        node("x2", "log", message="x"),
        node("y1", "log", message="y"),
        node("y2", "log", message="y"),
        node("z", "log", message="z"),
        node("j", "join"),
        node("after", "send_cmd", command="INST AFTER"),
    ]
    edges += [
        edge("t", "p1"),
        edge("p1", "p2"),
        edge("p1", "p3"),
        edge("p1", "z"),
        edge("p2", "x1"),
        edge("p2", "x2"),
        edge("p3", "y1"),
        edge("p3", "y2"),
        edge("x1", "j"),
        edge("x2", "j"),
        edge("y1", "j"),
        edge("y2", "j"),
        edge("z", "j"),
        edge("j", "after"),
    ]
    return make_flow(nodes, edges)


def test_join_pairing_is_computed_at_graph_build():
    graph = Graph(_owned_shared_join())
    assert graph._join_owners["j"] == {"p1", "p2", "p3"}
    assert graph.shared_join_problems() == []


@pytest.mark.parametrize("padding", [0, 1500])
def test_owned_shared_join_runs_the_tail_once(engine, backend, padding):
    for _ in range(5):
        backend.commands.clear()
        record = run_to_end(engine, _owned_shared_join(padding), timeout=30)
        assert record["status"] == "success", record["message"]
        assert backend.commands.count("INST AFTER") == 1


# B12 (E-8): a Script Runner error while waiting used to orphan the script
def _script_flow():
    return make_flow(
        [node("t", "trigger_manual"), node("s", "run_script", filename="INST/procedures/x.py", wait=True)],
        [edge("t", "s")],
    )


@pytest.fixture
def fast_poll(monkeypatch):
    monkeypatch.setattr(runner, "SCRIPT_POLL_SECONDS", 0.01)


def test_script_survives_a_few_poll_errors(engine, backend, fast_poll):
    calls = {"n": 0}
    real = backend.script_state

    def flaky(script_id):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise ConnectionError("502 Bad Gateway")
        return real(script_id)

    backend.script_state = flaky
    record = run_to_end(engine, _script_flow())
    assert record["status"] == "success", record["message"]
    assert backend.stopped_scripts == []


def test_script_is_stopped_when_polling_keeps_failing(engine, backend, fast_poll):
    def down(script_id):
        raise ConnectionError("502 Bad Gateway")

    backend.script_state = down
    record = run_to_end(engine, _script_flow())
    assert record["status"] == "failed"
    assert "Lost track" in record["message"]
    assert backend.stopped_scripts == [1]


def test_script_is_stopped_when_the_run_is_stopped(engine, backend, fast_poll):
    backend.scripts[1] = ["running"]
    saved, _ = engine.save_flow(_script_flow())
    run_id = engine.trigger(saved["id"], manual=True)["run"]["id"]
    assert wait_until(lambda: backend.started_scripts)
    engine.stop_run(run_id)
    assert wait_until(lambda: run_id not in engine.runs)
    assert backend.stopped_scripts == [1]


# S-14: a node can't point the engine at another stash key
def test_flow_result_key_is_not_a_nodes_choice(engine, backend, fast_poll):
    flow = _script_flow()
    flow["nodes"][1]["data"]["environment"] = {"FLOW_RESULT_KEY": "someone_elses_key"}
    run_to_end(engine, flow)
    env = backend.started_scripts[0][1]
    assert env["FLOW_RESULT_KEY"] != "someone_elses_key"
    assert env["FLOW_RESULT_KEY"].startswith("flow_automator__")
