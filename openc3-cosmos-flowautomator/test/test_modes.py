"""Crawl / walk / run execution modes and operator approvals."""

import pytest
from conftest import edge, make_flow, node, wait_until
from flow_automator.engine import EngineError, NotFoundError


def three_commands(**extra):
    return make_flow(
        [
            node("t", "trigger_manual"),
            node("a", "send_cmd", command="INST A"),
            node("b", "send_cmd", command="INST B", **extra.pop("b_data", {})),
            node("c", "send_cmd", command="INST C"),
        ],
        [edge("t", "a"), edge("a", "b"), edge("b", "c")],
        **extra,
    )


def start(engine, flow, **kwargs):
    saved, problems = engine.save_flow(flow)
    assert problems == [], problems
    result = engine.trigger(saved["id"], manual=True, **kwargs)
    assert result["result"] == "started", result
    return engine.runs[result["run"]["id"]]


def next_approval(engine, run, timeout=3):
    assert wait_until(lambda: run.pending_approvals() or run.id not in engine.runs, timeout), "no approval requested"
    approvals = run.pending_approvals()
    assert approvals, f"run finished without asking: {run.status} {run.message}"
    return approvals[0]


def finish(engine, run):
    assert wait_until(lambda: run.id not in engine.runs), "run did not finish"
    return engine.get_run(run.id)


def test_run_mode_never_asks(engine, backend):
    run = start(engine, three_commands(mode="run"))
    record = finish(engine, run)
    assert record["status"] == "success"
    assert backend.commands == ["INST A", "INST B", "INST C"]


def test_crawl_asks_before_every_step(engine, backend):
    run = start(engine, three_commands(mode="crawl"))
    for expected in ("INST A", "INST B", "INST C"):
        approval = next_approval(engine, run)
        assert approval["preview"] == expected
        assert "crawl" in approval["reason"]
        # Nothing is sent until the operator says go
        assert expected not in backend.commands
        engine.approve(run.id, approval["id"], "go")
        assert wait_until(lambda e=expected: e in backend.commands)
    assert finish(engine, run)["status"] == "success"


def test_crawl_run_decision_stops_asking(engine, backend):
    run = start(engine, three_commands(mode="crawl"))
    engine.approve(run.id, next_approval(engine, run)["id"], "run")
    record = finish(engine, run)
    assert record["status"] == "success"
    assert backend.commands == ["INST A", "INST B", "INST C"]
    assert record["mode"] == "crawl" and record["auto_approve"] is True


def test_run_rest_still_asks_for_hazardous(engine, backend):
    backend.hazardous.add("C")
    run = start(engine, three_commands(mode="crawl"))
    engine.approve(run.id, next_approval(engine, run)["id"], "run")
    approval = next_approval(engine, run)
    assert approval["node"] == "c" and "HAZARDOUS" in approval["reason"]
    assert backend.commands == ["INST A", "INST B"]
    engine.approve(run.id, approval["id"], "go")
    assert finish(engine, run)["status"] == "success"
    assert backend.commands == ["INST A", "INST B", "INST C"]


def test_abort_stops_the_run(engine, backend):
    run = start(engine, three_commands(mode="crawl"))
    engine.approve(run.id, next_approval(engine, run)["id"], "go")
    engine.approve(run.id, next_approval(engine, run)["id"], "abort")
    record = finish(engine, run)
    assert record["status"] == "stopped"
    assert "Aborted by operator" in record["message"]
    assert backend.commands == ["INST A"]


def test_walk_asks_only_for_marked_steps(engine, backend):
    run = start(engine, three_commands(mode="walk", b_data={"require_approval": True}))
    approval = next_approval(engine, run)
    assert approval["node"] == "b"
    assert backend.commands == ["INST A"]
    engine.approve(run.id, approval["id"], "go")
    assert finish(engine, run)["status"] == "success"
    assert backend.commands == ["INST A", "INST B", "INST C"]


def test_walk_asks_for_hazardous_and_approval_allows_it(engine, backend):
    backend.hazardous.add("B")
    run = start(engine, three_commands(mode="walk"))
    approval = next_approval(engine, run)
    assert approval["node"] == "b" and "HAZARDOUS" in approval["reason"]
    engine.approve(run.id, approval["id"], "go")
    assert finish(engine, run)["status"] == "success"
    # Sent although the node doesn't set allow_hazardous: the operator approved it
    assert backend.commands == ["INST A", "INST B", "INST C"]


def test_run_mode_still_blocks_unflagged_hazardous(engine, backend):
    backend.hazardous.add("B")
    record = finish(engine, start(engine, three_commands(mode="run")))
    assert record["status"] == "failed"
    assert backend.commands == ["INST A"]


def test_mode_override_per_run(engine, backend):
    run = start(engine, three_commands(mode="run"), mode="crawl")
    assert run.mode == "crawl"
    engine.approve(run.id, next_approval(engine, run)["id"], "abort")
    assert finish(engine, run)["status"] == "stopped"
    with pytest.raises(EngineError):
        engine.trigger(run.flow["id"], manual=True, mode="sprint")


def test_approval_timeout_fails_run(engine, backend):
    run = start(engine, three_commands(mode="crawl", approval_timeout=0.2))
    record = finish(engine, run)
    assert record["status"] == "failed"
    assert "No operator approval" in record["message"]
    assert backend.commands == []


def test_stop_while_waiting(engine):
    run = start(engine, three_commands(mode="crawl"))
    next_approval(engine, run)
    engine.stop_run(run.id)
    assert finish(engine, run)["status"] == "stopped"


def test_parallel_branches_wait_independently(engine, backend):
    flow = make_flow(
        [
            node("t", "trigger_manual"),
            node("p", "parallel"),
            node("x", "send_cmd", command="INST X", require_approval=True),
            node("y", "send_cmd", command="INST Y", require_approval=True),
            node("j", "join"),
        ],
        [edge("t", "p"), edge("p", "x"), edge("p", "y"), edge("x", "j"), edge("y", "j")],
        mode="walk",
    )
    run = start(engine, flow)
    assert wait_until(lambda: len(run.pending_approvals()) == 2)
    statuses = engine.status()["approvals"]
    assert {a["node"] for a in statuses} == {"x", "y"}
    # "Go and run the rest" also releases the other waiting branch
    engine.approve(run.id, statuses[0]["id"], "run")
    assert finish(engine, run)["status"] == "success"
    assert sorted(backend.commands) == ["INST X", "INST Y"]


def test_subflow_runs_at_least_as_cautiously(engine, backend):
    child, _ = engine.save_flow(
        make_flow(
            [node("t", "trigger_manual"), node("c", "send_cmd", command="INST CHILD")],
            [edge("t", "c")],
            mode="run",
            name="Child",
        )
    )
    parent = make_flow(
        [node("t", "trigger_manual"), node("s", "subflow", flow_id=child["id"])],
        [edge("t", "s")],
        mode="crawl",
    )
    run = start(engine, parent)
    engine.approve(run.id, next_approval(engine, run)["id"], "go")  # the sub-flow node itself
    assert wait_until(lambda: run.children)
    child_run = engine.runs[run.children[0]]
    assert child_run.mode == "crawl"
    approval = next_approval(engine, child_run)
    assert approval["preview"] == "INST CHILD"
    engine.approve(child_run.id, approval["id"], "go")
    assert finish(engine, run)["status"] == "success"


def test_triggered_run_waits_for_operator(engine, backend):
    backend.values["INST HEALTH_STATUS TEMP1"] = 0
    flow = make_flow(
        [node("tr", "trigger_tlm", condition="INST HEALTH_STATUS TEMP1 > 50"), node("c", "send_cmd", command="INST X")],
        [edge("tr", "c")],
        mode="crawl",
    )
    saved, _ = engine.save_flow(flow)
    engine.set_armed(saved["id"], True)
    engine.triggers.tick()
    backend.values["INST HEALTH_STATUS TEMP1"] = 60
    engine.triggers.tick()
    assert wait_until(lambda: engine.status()["approvals"])
    approval = engine.status()["approvals"][0]
    assert approval["flow_id"] == saved["id"] and backend.commands == []
    engine.approve(approval["run_id"], approval["id"], "go")
    assert wait_until(lambda: backend.commands == ["INST X"])


def test_approve_errors(engine):
    run = start(engine, three_commands(mode="crawl"))
    approval = next_approval(engine, run)
    with pytest.raises(EngineError):
        engine.approve(run.id, approval["id"], "maybe")
    with pytest.raises(NotFoundError):
        engine.approve(run.id, "nope", "go")
    with pytest.raises(NotFoundError):
        engine.approve("norun", approval["id"], "go")
    engine.approve(run.id, approval["id"], "abort")
    finish(engine, run)


def test_old_flows_default_to_run_and_bad_mode_is_a_problem(engine):
    saved, problems = engine.save_flow(make_flow([node("t", "trigger_manual")], []))
    assert saved["mode"] == "run" and problems == []
    from flow_automator.graph import validate_flow

    assert any("Mode must be" in p["message"] for p in validate_flow({**saved, "mode": "sprint"}))
    assert any("Approval timeout" in p["message"] for p in validate_flow({**saved, "approval_timeout": -1}))
