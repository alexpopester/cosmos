"""Arming and mode fixes from docs/EVALUATION-2026-11-05.md (backlog B4, B5, B7)."""

import pytest
from conftest import edge, make_flow, node, run_to_end, wait_until
from flow_automator.contacts import ContactPlanner, PlannerError


def _armed(engine, flow):
    saved, problems = engine.save_flow(flow)
    assert problems == [], problems
    engine.set_armed(saved["id"], True)
    return saved


def _resave(engine, saved, **changes):
    flow = engine.get_flow(saved["id"])
    flow.update(changes)
    return engine.save_flow(flow, saved["id"])[0]


def _cmd_flow(command="INST NOOP", **data):
    return make_flow(
        [node("t", "trigger_manual"), node("c", "send_cmd", command=command, **data)],
        [edge("t", "c")],
    )


# B4 (E-7, R-10, S-6): saves that relax an armed flow without disarming it
def test_turning_dry_run_off_disarms(engine):
    saved = _armed(engine, _cmd_flow(**{}) | {"dry_run": True})
    result = _resave(engine, saved, dry_run=False)
    assert result["armed"] is False
    assert "dry run turned off" in result["disarmed_reason"]


def test_editing_hazardous_command_disarms(engine):
    saved = _armed(engine, _cmd_flow("INST NOOP", allow_hazardous=True))
    flow = engine.get_flow(saved["id"])
    flow["nodes"][1]["data"]["command"] = "INST SELFDESTRUCT"
    result = engine.save_flow(flow, saved["id"])[0]
    assert result["armed"] is False
    assert "command changed" in result["disarmed_reason"]


def test_editing_an_ordinary_command_keeps_the_flow_armed(engine):
    saved = _armed(engine, _cmd_flow("INST NOOP"))
    flow = engine.get_flow(saved["id"])
    flow["nodes"][1]["data"]["command"] = "INST COLLECT"
    assert engine.save_flow(flow, saved["id"])[0]["armed"] is True


@pytest.mark.parametrize("kind,data", [("run_script", {"filename": "INST/procedures/x.py"}), ("set_tlm", {})])
def test_adding_a_node_that_acts_disarms(engine, kind, data):
    saved = _armed(engine, make_flow([node("t", "trigger_manual"), node("l", "log", message="x")], [edge("t", "l")]))
    flow = engine.get_flow(saved["id"])
    if kind == "set_tlm":
        data = {"item": "INST HEALTH_STATUS TEMP1", "value": "1"}
    flow["nodes"].append(node("n", kind, **data))
    flow["edges"].append(edge("l", "n"))
    result = engine.save_flow(flow, saved["id"])[0]
    assert result["armed"] is False
    assert f"new {kind} node" in result["disarmed_reason"]


def test_relaxing_a_subflow_disarms_armed_callers(engine):
    child = _armed(engine, _cmd_flow("INST NOOP") | {"name": "Child"})
    caller = _armed(
        engine,
        make_flow(
            [node("t", "trigger_manual"), node("s", "subflow", flow_id=child["id"])],
            [edge("t", "s")],
            name="Caller",
        ),
    )
    outer = _armed(
        engine,
        make_flow(
            [node("t", "trigger_manual"), node("s", "subflow", flow_id=caller["id"])],
            [edge("t", "s")],
            name="Outer",
        ),
    )
    flow = engine.get_flow(child["id"])
    flow["nodes"][1]["data"]["allow_hazardous"] = True
    engine.save_flow(flow, child["id"])
    assert engine.get_flow(caller["id"])["armed"] is False
    assert engine.get_flow(outer["id"])["armed"] is False
    assert "sub-flow 'Child' was relaxed" in engine.get_flow(caller["id"])["disarmed_reason"]


# B5 (E-6, R-6, DOC-1): activities and rules bypassed the flow's mode floor
def test_scheduled_run_cannot_be_less_cautious_than_the_flow(engine, backend):
    flow = _cmd_flow("INST NOOP") | {"mode": "crawl", "id": "f1"}
    run = engine.start_scheduled_run(flow, "test", {}, "run", trigger_info={"kind": "activity"})
    assert run.mode == "crawl"
    assert wait_until(lambda: run.pending_approvals())
    assert backend.commands == []
    run.stop()


def test_a_dry_run_may_still_skip_approvals(engine, backend):
    flow = _cmd_flow("INST NOOP") | {"mode": "crawl", "id": "f1"}
    run = engine.start_scheduled_run(flow, "rehearse", {}, "run", trigger_info={"kind": "rehearsal"}, dry_run=True)
    assert run.mode == "run"
    assert wait_until(lambda: run.id not in engine.runs)


@pytest.mark.parametrize("mode,ok", [("crawl", True), ("walk", True), ("run", False)])
def test_activity_and_rule_modes_are_checked_against_the_template(mode, ok):
    template = {"name": "Pass plan", "mode": "walk"}
    if ok:
        ContactPlanner._check_mode_floor({"mode": mode}, template)
    else:
        with pytest.raises(PlannerError, match="walk flow"):
            ContactPlanner._check_mode_floor({"mode": mode}, template)


# B7 (R-5, S-4): a saved flow could carry hidden sub-flow content that ran instead of the real sub-flow
def test_subflow_snapshots_cannot_be_smuggled_into_a_flow(engine, backend):
    sub, _ = engine.save_flow(_cmd_flow("INST SAFE") | {"name": "Sub"})
    evil = _cmd_flow("INST EVIL") | {"name": "Sub", "id": sub["id"]}
    parent = make_flow(
        [node("t", "trigger_manual"), node("s", "subflow", flow_id=sub["id"])],
        [edge("t", "s")],
        name="Parent",
        subflow_snapshots={sub["id"]: evil},
    )
    record = run_to_end(engine, parent)
    assert record["status"] == "success", record["message"]
    assert backend.commands == ["INST SAFE"]
    stored = next(f for f in engine.flows.values() if f["name"] == "Parent")
    assert "subflow_snapshots" not in stored


def test_scheduled_runs_still_use_their_pinned_subflows(engine, backend):
    sub, _ = engine.save_flow(_cmd_flow("INST V1") | {"name": "Sub"})
    parent, _ = engine.save_flow(
        make_flow([node("t", "trigger_manual"), node("s", "subflow", flow_id=sub["id"])], [edge("t", "s")], name="P")
    )
    snapshot = engine.snapshot_flow(parent["id"])
    flow = engine.get_flow(sub["id"])
    flow["nodes"][1]["data"]["command"] = "INST V2"
    engine.save_flow(flow, sub["id"])
    run = engine.start_scheduled_run(snapshot, "scheduled", {}, "run", trigger_info={"kind": "activity"})
    assert wait_until(lambda: run.id not in engine.runs)
    assert backend.commands == ["INST V1"]


# B6 (E-3, E-9, R-7, R-9, U-20): walk/crawl could send a command the operator never approved
def _walk(command, **data):
    return make_flow(
        [node("t", "trigger_manual"), node("c", "send_cmd", command=command, **data)],
        [edge("t", "c")],
        mode="walk",
    )


def _start(engine, flow):
    saved, _ = engine.save_flow(flow)
    return engine.trigger(saved["id"], manual=True)["run"]["id"]


def test_walk_ignores_allow_hazardous_and_asks(engine, backend):
    backend.hazardous = {"FIRE"}
    run_id = _start(engine, _walk("INST FIRE", allow_hazardous=True))
    assert wait_until(lambda: engine.runs[run_id].pending_approvals())
    approval = engine.runs[run_id].pending_approvals()[0]
    assert approval["hazardous"] is True and backend.commands == []
    engine.approve(run_id, approval["id"], "go", "alice")
    assert wait_until(lambda: run_id not in engine.runs)
    assert backend.commands == ["INST FIRE"]


def test_hazard_lookup_failure_counts_as_hazardous(engine, backend):
    def broken(command):
        raise KeyError("PARAM")

    backend.cmd_hazardous = broken
    run_id = _start(engine, _walk("INST FIRE", allow_hazardous=True))
    assert wait_until(lambda: engine.runs[run_id].pending_approvals())
    assert engine.runs[run_id].pending_approvals()[0]["hazardous"] is True
    assert backend.commands == []
    engine.stop_run(run_id)


def test_cosmos_hazard_the_check_missed_is_asked_for_not_sent(engine, backend):
    backend.cosmos_only_hazardous = {"FIRE"}  # e.g. a hazardous default the pre-check doesn't see
    run_id = _start(engine, _walk("INST FIRE", allow_hazardous=True))
    assert wait_until(lambda: engine.runs[run_id].pending_approvals())
    approval = engine.runs[run_id].pending_approvals()[0]
    assert approval["hazardous"] is True and approval["preview"] == "INST FIRE"
    assert backend.commands == []
    engine.approve(run_id, approval["id"], "go", "alice")
    assert wait_until(lambda: run_id not in engine.runs)
    assert backend.commands == ["INST FIRE"]


def test_run_mode_still_uses_allow_hazardous(engine, backend):
    backend.cosmos_only_hazardous = {"FIRE"}
    flow = _walk("INST FIRE", allow_hazardous=True) | {"mode": "run"}
    assert run_to_end(engine, flow)["status"] == "success"
    assert backend.commands == ["INST FIRE"]


def test_crawl_never_asks_for_a_command_it_cannot_show(engine, backend):
    def down(item, value_type="CONVERTED"):
        raise ConnectionError("COSMOS API down")

    backend.tlm = down
    flow = _walk("INST SET with VALUE {{ INST HEALTH_STATUS TEMP1 }}") | {"mode": "crawl"}
    record = run_to_end(engine, flow)
    assert record["status"] == "failed"
    assert "to show for approval" in record["message"]
    assert backend.commands == []


def test_approved_command_using_now_is_sent(engine, backend):
    run_id = _start(engine, _walk("INST SET with T {{ now() }}") | {"mode": "crawl"})
    assert wait_until(lambda: engine.runs[run_id].pending_approvals())
    approval = engine.runs[run_id].pending_approvals()[0]
    assert approval["type"] == "send_cmd"
    engine.approve(run_id, approval["id"], "go", "alice")
    assert wait_until(lambda: run_id not in engine.runs)
    assert backend.commands == [approval["preview"]]
