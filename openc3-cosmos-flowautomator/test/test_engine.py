import time

import pytest
from conftest import edge, make_flow, node, run_to_end, wait_until
from flow_automator.engine import EngineError
from flow_automator.graph import validate_flow


def test_send_command_and_if_branch(engine, backend):
    backend.values["INST HEALTH_STATUS TEMP1"] = 60
    flow = make_flow(
        [
            node("t", "trigger_manual"),
            node("if", "if", condition="INST HEALTH_STATUS TEMP1 > 50"),
            node("hot", "send_cmd", command="INST COLLECT with TYPE SPECIAL"),
            node("cold", "send_cmd", command="INST COLLECT with TYPE NORMAL"),
        ],
        [edge("t", "if"), edge("if", "hot", "true"), edge("if", "cold", "false")],
    )
    run = run_to_end(engine, flow)
    assert run["status"] == "success"
    assert backend.commands == ["INST COLLECT with TYPE SPECIAL"]
    assert [s["node"] for s in run["steps"]] == ["t", "if", "hot"]


def test_variables_templates_and_loop(engine, backend):
    flow = make_flow(
        [
            node("t", "trigger_manual"),
            node("loop", "loop", mode="count", count=3, variable="i"),
            node("cmd", "send_cmd", command="INST COLLECT with DURATION {{ i + base }}"),
            node("done", "set_var", name="finished", expression="True"),
        ],
        [edge("t", "loop"), edge("loop", "cmd", "body"), edge("cmd", "loop"), edge("loop", "done", "done")],
        variables={"base": 10},
    )
    run = run_to_end(engine, flow)
    assert run["status"] == "success"
    assert backend.commands == [f"INST COLLECT with DURATION {n}" for n in (10, 11, 12)]
    assert run["variables"]["finished"] is True


def test_while_loop_with_set_tlm(engine, backend):
    backend.values["INST HEALTH_STATUS TEMP1"] = 0
    flow = make_flow(
        [
            node("t", "trigger_manual"),
            node("loop", "loop", mode="while", condition="INST HEALTH_STATUS TEMP1 < 3"),
            node("bump", "set_tlm", item="INST HEALTH_STATUS TEMP1", value="{{ INST HEALTH_STATUS TEMP1 + 1 }}"),
        ],
        [edge("t", "loop"), edge("loop", "bump", "body"), edge("bump", "loop")],
    )
    run = run_to_end(engine, flow)
    assert run["status"] == "success"
    assert backend.values["INST HEALTH_STATUS TEMP1"] == 3


def test_endless_loop_is_caught(engine):
    flow = make_flow(
        [node("t", "trigger_manual"), node("loop", "loop", mode="while", condition="True", max_iterations=50)],
        [edge("t", "loop"), edge("loop", "loop", "body")],
    )
    run = run_to_end(engine, flow)
    assert run["status"] == "failed"
    assert "exceeded 50 iterations" in run["message"]


def test_switch(engine, backend):
    backend.values["INST HEALTH_STATUS COLLECT_TYPE"] = "SPECIAL"
    flow = make_flow(
        [
            node("t", "trigger_manual"),
            node("sw", "switch", expression="INST HEALTH_STATUS COLLECT_TYPE", cases=["'NORMAL'", "'SPECIAL'"]),
            node("a", "log", message="normal"),
            node("b", "log", message="special"),
            node("c", "log", message="other"),
        ],
        [edge("t", "sw"), edge("sw", "a", "case_0"), edge("sw", "b", "case_1"), edge("sw", "c", "default")],
    )
    run = run_to_end(engine, flow)
    assert [s["node"] for s in run["steps"]][-1] == "b"


def test_error_handle_routes_failures(engine, backend):
    backend.fail_commands.add("ABORT")
    flow = make_flow(
        [
            node("t", "trigger_manual"),
            node("cmd", "send_cmd", command="INST ABORT"),
            node("recover", "log", message="recovering from {{ error }}"),
        ],
        [edge("t", "cmd"), edge("cmd", "recover", "error")],
    )
    run = run_to_end(engine, flow)
    assert run["status"] == "success"
    assert run["steps"][-1]["message"] == "recovering from Command ABORT failed"


def test_unhandled_error_fails_run(engine, backend):
    backend.fail_commands.add("ABORT")
    flow = make_flow([node("t", "trigger_manual"), node("cmd", "send_cmd", command="INST ABORT")], [edge("t", "cmd")])
    run = run_to_end(engine, flow)
    assert run["status"] == "failed"
    assert "Command ABORT failed" in run["message"]


def test_hazardous_commands_blocked_unless_allowed(engine, backend):
    backend.hazardous.add("CLEAR")
    blocked = make_flow([node("t", "trigger_manual"), node("c", "send_cmd", command="INST CLEAR")], [edge("t", "c")])
    assert run_to_end(engine, blocked)["status"] == "failed"
    allowed = make_flow(
        [node("t", "trigger_manual"), node("c", "send_cmd", command="INST CLEAR", allow_hazardous=True)],
        [edge("t", "c")],
    )
    assert run_to_end(engine, allowed)["status"] == "success"
    assert backend.commands == ["INST CLEAR"]


def test_wait_tlm_ok_and_timeout(engine, backend):
    backend.values["INST HEALTH_STATUS TEMP1"] = 0

    flow = make_flow(
        [
            node("t", "trigger_manual"),
            node("w", "wait_tlm", condition="INST HEALTH_STATUS TEMP1 == 5", timeout=2, poll=0.05),
            node("ok", "log", message="ok"),
            node("late", "log", message="late"),
        ],
        [edge("t", "w"), edge("w", "ok", "ok"), edge("w", "late", "timeout")],
    )
    saved, _ = engine.save_flow(flow)
    run_id = engine.trigger(saved["id"], manual=True)["run"]["id"]
    time.sleep(0.2)
    backend.values["INST HEALTH_STATUS TEMP1"] = 5
    assert wait_until(lambda: run_id not in engine.runs)
    assert engine.get_run(run_id)["steps"][-1]["node"] == "ok"

    flow["nodes"][1]["data"]["condition"] = "INST HEALTH_STATUS TEMP1 == 99"
    flow["nodes"][1]["data"]["timeout"] = 0.2
    run = run_to_end(engine, flow)
    assert run["steps"][-1]["node"] == "late"


def test_wait_check_failure_fails_run(engine, backend):
    backend.values["INST HEALTH_STATUS TEMP1"] = 0
    flow = make_flow(
        [node("t", "trigger_manual"), node("w", "wait_check", condition="INST HEALTH_STATUS TEMP1 > 1", timeout=0.1)],
        [edge("t", "w")],
    )
    run = run_to_end(engine, flow)
    assert run["status"] == "failed"
    assert "Wait check failed" in run["message"]


def test_parallel_and_join(engine, backend):
    flow = make_flow(
        [
            node("t", "trigger_manual"),
            node("p", "parallel"),
            node("a", "wait_time", seconds=0.2),
            node("a2", "send_cmd", command="INST A"),
            node("b", "send_cmd", command="INST B"),
            node("j", "join"),
            node("after", "send_cmd", command="INST AFTER"),
        ],
        [
            edge("t", "p"),
            edge("p", "a"),
            edge("a", "a2"),
            edge("p", "b"),
            edge("a2", "j"),
            edge("b", "j"),
            edge("j", "after"),
        ],
    )
    run = run_to_end(engine, flow)
    assert run["status"] == "success"
    assert backend.commands == ["INST B", "INST A", "INST AFTER"]


def test_parallel_branch_failure_stops_siblings(engine, backend):
    backend.fail_commands.add("BAD")
    flow = make_flow(
        [
            node("t", "trigger_manual"),
            node("p", "parallel"),
            node("slow", "wait_time", seconds=30),
            node("bad", "send_cmd", command="INST BAD"),
            node("j", "join"),
        ],
        [edge("t", "p"), edge("p", "slow"), edge("p", "bad"), edge("slow", "j"), edge("bad", "j")],
    )
    started = time.monotonic()
    run = run_to_end(engine, flow)
    assert run["status"] == "failed"
    assert time.monotonic() - started < 5


def test_end_node(engine):
    flow = make_flow(
        [node("t", "trigger_manual"), node("e", "end", status="failed", message="bad thing {{ 1 + 1 }}")],
        [edge("t", "e")],
    )
    run = run_to_end(engine, flow)
    assert run["status"] == "failed"
    assert run["message"] == "bad thing 2"


def test_run_script_waits_and_reports(engine, backend):
    flow = make_flow(
        [
            node("t", "trigger_manual"),
            node("s", "run_script", filename="INST/procedures/{{ name }}.py", environment={"N": "{{ 2 * 3 }}"}),
        ],
        [edge("t", "s")],
        variables={"name": "checkout"},
    )
    run = run_to_end(engine, flow)
    assert run["status"] == "success"
    ((filename, environment),) = backend.started_scripts
    assert filename == "INST/procedures/checkout.py" and environment["N"] == "6"
    assert environment["FLOW_RESULT_KEY"].startswith("flow_automator__") and environment["FLOW_NAME"] == "Test flow"
    assert run["variables"]["script_state"] == "completed"

    backend.scripts[2] = ["running", "crashed"]
    run = run_to_end(engine, flow)
    assert run["status"] == "failed"
    assert "crashed" in run["message"]


def test_stop_run_stops_script(engine, backend):
    backend.scripts[1] = ["running"]
    flow = make_flow([node("t", "trigger_manual"), node("s", "run_script", filename="X/a.py")], [edge("t", "s")])
    saved, _ = engine.save_flow(flow)
    run_id = engine.trigger(saved["id"], manual=True)["run"]["id"]
    assert wait_until(lambda: backend.started_scripts)
    engine.stop_run(run_id)
    assert wait_until(lambda: run_id not in engine.runs)
    assert engine.get_run(run_id)["status"] == "stopped"
    assert backend.stopped_scripts == [1]


def test_subflow_passes_variables(engine, backend):
    child, _ = engine.save_flow(
        make_flow(
            [node("t", "trigger_manual"), node("set", "set_var", name="result", expression="x * 2")],
            [edge("t", "set")],
            name="Child",
        )
    )
    parent = make_flow(
        [
            node("t", "trigger_manual"),
            node("call", "subflow", flow_id=child["id"]),
            node("cmd", "send_cmd", command="INST COLLECT with DURATION {{ result }}"),
        ],
        [edge("t", "call"), edge("call", "cmd")],
        variables={"x": 21},
    )
    run = run_to_end(engine, parent)
    assert run["status"] == "success", run["message"]
    assert backend.commands == ["INST COLLECT with DURATION 42"]
    assert len(run["children"]) == 1


def test_recursive_subflow_is_limited(engine):
    flow, _ = engine.save_flow(make_flow([node("t", "trigger_manual")], [], name="Recursive"))
    flow["nodes"].append(node("call", "subflow", flow_id=flow["id"]))
    flow["edges"].append(edge("t", "call"))
    engine.save_flow(flow, flow["id"])
    result = engine.trigger(flow["id"], manual=True)
    assert wait_until(lambda: not engine.runs)
    run = engine.get_run(result["run"]["id"])
    assert run["status"] == "failed"
    assert "nested" in run["message"]


def test_policy_skip_and_queue(engine, backend):
    flow = make_flow(
        [node("t", "trigger_manual"), node("w", "wait_time", seconds=0.3), node("c", "send_cmd", command="INST X")],
        [edge("t", "w"), edge("w", "c")],
        policy="skip",
    )
    saved, _ = engine.save_flow(flow)
    assert engine.trigger(saved["id"], manual=True)["result"] == "started"
    assert engine.trigger(saved["id"], manual=True)["result"] == "skipped"
    assert wait_until(lambda: not engine.runs)
    assert len(backend.commands) == 1

    saved["policy"] = "queue"
    engine.save_flow(saved, saved["id"])
    assert engine.trigger(saved["id"], manual=True)["result"] == "started"
    assert engine.trigger(saved["id"], manual=True)["result"] == "queued"
    assert wait_until(lambda: len(backend.commands) == 3, timeout=3)

    saved["policy"] = "parallel"
    engine.save_flow(saved, saved["id"])
    assert engine.trigger(saved["id"], manual=True)["result"] == "started"
    assert engine.trigger(saved["id"], manual=True)["result"] == "started"
    assert wait_until(lambda: len(backend.commands) == 5, timeout=3)


def test_stop_all_disarms_and_stops(engine, backend):
    flow = make_flow(
        [node("t", "trigger_manual"), node("w", "wait_time", seconds=30)],
        [edge("t", "w")],
        policy="parallel",
    )
    saved, _ = engine.save_flow(flow)
    engine.set_armed(saved["id"], True)
    engine.trigger(saved["id"], manual=True)
    engine.trigger(saved["id"], manual=True)
    assert len(engine.runs) == 2
    assert engine.stop_all() == {
        "disarmed": 1,
        "stopped": 2,
        "scripts_stopped": 0,
        "scheduler_paused": True,
        "persisted": True,
    }
    assert wait_until(lambda: not engine.runs)
    assert not engine.get_flow(saved["id"])["armed"]


def test_cannot_arm_invalid_flow_and_save_disarms(engine):
    saved, problems = engine.save_flow(make_flow([node("i", "if", condition="(")], []))
    assert problems
    with pytest.raises(EngineError):
        engine.set_armed(saved["id"], True)

    good, _ = engine.save_flow(make_flow([node("t", "trigger_manual")], []))
    engine.set_armed(good["id"], True)
    good["nodes"].append(node("bad", "if", condition="("))
    updated, problems = engine.save_flow(good, good["id"])
    assert problems and not updated["armed"]


def test_validation_problems():
    flow = make_flow(
        [
            node("t", "trigger_manual"),
            node("a", "log", message="a"),
            node("b", "log", message="b"),
            node("p", "parallel"),
            node("s", "trigger_schedule", mode="cron", cron="bad"),
        ],
        [edge("t", "a"), edge("t", "b"), edge("a", "t"), edge("b", "p")],
    )
    messages = [p["message"] for p in validate_flow(flow)]
    assert any("more than one wire" in m for m in messages)
    assert any("Triggers can't have inputs" in m for m in messages)
    assert any("Parallel needs at least two" in m for m in messages)
    assert any("Cron needs 5 fields" in m for m in messages)


def test_history_persisted(engine):
    flow = make_flow([node("t", "trigger_manual")], [])
    run = run_to_end(engine, flow)
    runs = engine.list_runs(run["flow_id"])
    assert runs[0]["id"] == run["id"]
    assert "steps" not in runs[0]
    assert engine.list_flows()[0]["last_run"]["status"] == "success"
