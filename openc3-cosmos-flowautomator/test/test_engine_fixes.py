"""Regression tests for engine bugs from docs/EVALUATION-2026-11-04.md section 2.2."""

import threading

from conftest import FakeBackend, ListLogger, edge, make_flow, node, run_to_end, wait_until
from flow_automator.engine import FlowEngine
from flow_automator.store import FlowStore, MemoryRedis
from test_triggers import armed_flow, finished


# ENG-7: a loop left early starts again from 0 the next time it's entered
def test_inner_loop_restarts_after_break(engine, backend):
    flow = make_flow(
        [
            node("t", "trigger_manual"),
            node("outer", "loop", mode="count", count=2, variable="o"),
            node("inner", "loop", mode="count", count=3, variable="i"),
            node("cmd", "send_cmd", command="INST C with O {{o}} I {{i}}"),
            node("brk", "if", condition="i == 1"),
        ],
        [
            edge("t", "outer"),
            edge("outer", "inner", "body"),
            edge("inner", "cmd", "body"),
            edge("cmd", "brk"),
            edge("brk", "outer", "true"),  # break out of the inner loop
            edge("brk", "inner", "false"),
            edge("inner", "outer", "done"),
        ],
    )
    assert run_to_end(engine, flow)["status"] == "success"
    assert backend.commands == [
        "INST C with O 0 I 0",
        "INST C with O 0 I 1",
        "INST C with O 1 I 0",
        "INST C with O 1 I 1",
    ]


def test_nested_loops_that_finish_normally_still_work(engine, backend):
    flow = make_flow(
        [
            node("t", "trigger_manual"),
            node("outer", "loop", mode="count", count=2, variable="o"),
            node("inner", "loop", mode="count", count=2, variable="i"),
            node("cmd", "send_cmd", command="INST C with O {{o}} I {{i}}"),
        ],
        [
            edge("t", "outer"),
            edge("outer", "inner", "body"),
            edge("inner", "cmd", "body"),
            edge("cmd", "inner"),
            edge("inner", "outer", "done"),
        ],
    )
    assert run_to_end(engine, flow)["status"] == "success"
    assert len(backend.commands) == 4 and backend.commands[-1] == "INST C with O 1 I 1"


# ENG-10: serializing a run while its threads add variables doesn't fail
def test_as_json_while_variables_change(engine):
    from flow_automator.runner import FlowRun

    run = FlowRun(engine, make_flow([node("t", "trigger_manual")], []), "t", "test", {})
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            run.variables[f"k{i % 5000}"] = i
            if i % 5000 == 4999:
                run.variables.clear()
            i += 1

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        for _ in range(300):
            run.as_json()
    finally:
        stop.set()
        thread.join()


# ENG-13: an Ask with no timeout of its own follows 'timeout' after the flow's approval timeout
def test_ask_follows_timeout_after_flow_approval_timeout(engine, backend):
    flow = make_flow(
        [
            node("t", "trigger_manual"),
            node("a", "ask", prompt="Proceed?", kind="text", default="later"),
            node("ok", "send_cmd", command="INST ANSWERED"),
            node("late", "send_cmd", command="INST TIMEOUT"),
        ],
        [edge("t", "a"), edge("a", "ok", "answered"), edge("a", "late", "timeout")],
        approval_timeout=0.3,
    )
    record = run_to_end(engine, flow, timeout=5)
    assert record["status"] == "success" and backend.commands == ["INST TIMEOUT"]


# ENG-14: an answer that arrives after the approval timed out is refused, not reported as accepted
def test_late_answer_is_refused(engine):
    flow = make_flow(
        [node("t", "trigger_manual"), node("c", "send_cmd", command="INST X", require_approval=True)],
        [edge("t", "c")],
        mode="walk",
        approval_timeout=0.2,
    )
    saved, _ = engine.save_flow(flow)
    run_id = engine.trigger(saved["id"], manual=True)["run"]["id"]
    run = engine.runs[run_id]
    assert wait_until(lambda: run.approvals)
    approval_id = next(iter(run.approvals))
    assert wait_until(lambda: run_id not in engine.runs)
    assert run.approve(approval_id, "go", "alice") is False
    assert engine.get_run(run_id)["status"] == "failed"


# ENG-15: aborting a sub-flow stops its caller with the reason, and the step shows stopped
def test_subflow_abort_reason_reaches_the_caller(engine):
    child, _ = engine.save_flow(
        make_flow(
            [node("t", "trigger_manual"), node("c", "send_cmd", command="INST X")],
            [edge("t", "c")],
            mode="crawl",
        )
    )
    parent, _ = engine.save_flow(
        make_flow([node("t", "trigger_manual"), node("s", "subflow", flow_id=child["id"])], [edge("t", "s")])
    )
    run_id = engine.trigger(parent["id"], manual=True)["run"]["id"]
    assert wait_until(lambda: engine.status()["approvals"])
    approval = engine.status()["approvals"][0]
    engine.approve(approval["run_id"], approval["id"], "abort", "alice")
    assert wait_until(lambda: run_id not in engine.runs)
    record = engine.get_run(run_id)
    assert record["status"] == "stopped" and "Aborted by alice" in record["message"]
    assert next(s for s in record["steps"] if s["node"] == "s")["status"] == "stopped"


# ENG-17: only plain numbers become numbers in multi-part templates
def test_render_value_parses_only_plain_numbers(engine):
    from flow_automator.runner import FlowRun

    run = FlowRun(engine, make_flow([node("t", "trigger_manual")], []), "t", "test", {"x": "7"})
    render = run.render_value
    assert render("{{ x }}0") == 70
    assert render("-1.5e3") == -1500.0 and render("2.50") == 2.5
    for text in ("nan", "inf", "Infinity", "007", "1_000", "0x10", " "):
        assert render(text) == text


# ENG-11: one bad packet doesn't break every packet trigger, and bad refs aren't retried every tick
def test_bad_packet_does_not_break_other_packet_triggers(engine, backend):
    from flow_automator.triggers import TriggerManager

    original = backend.tlm_counts
    calls = []

    def counts(packets):
        calls.append(list(packets))
        if any(p[0] == "NOPE" for p in packets):
            raise RuntimeError("Packet 'NOPE NOPE' does not exist")
        return original(packets)

    backend.tlm_counts = counts
    backend.counts[("INST", "ADCS")] = 5
    armed_flow(engine, node("bad", "trigger_packet", target="NOPE", packet="NOPE"))
    armed_flow(engine, node("good", "trigger_packet", target="INST", packet="ADCS"))
    clock = [1000.0]
    manager = TriggerManager(engine, clock=lambda: clock[0])
    manager.tick()
    backend.counts[("INST", "ADCS")] = 6
    calls.clear()
    manager.tick()
    assert finished(engine, backend, 1)
    assert calls == [[("INST", "ADCS")]]  # the bad packet is left out of the batch for a while


# ENG-8: runs interrupted by a crash are recorded as failed and their scripts stopped
def test_interrupted_runs_are_recovered_at_startup():
    redis = MemoryRedis()
    backend = FakeBackend()
    store = FlowStore(redis, "DEFAULT")
    first = FlowEngine(store, backend, ListLogger())
    first.start(start_triggers=False)
    saved, _ = first.save_flow(
        make_flow(
            [
                node("t", "trigger_manual"),
                node("s", "run_script", filename="INST/procedures/long.py"),
            ],
            [edge("t", "s")],
        )
    )
    backend.scripts[1] = ["running"]  # never finishes
    run_id = first.trigger(saved["id"], manual=True)["run"]["id"]
    assert wait_until(lambda: backend.started_scripts)
    assert store.records("active_runs")[run_id]["scripts"] == [1]
    # The process dies: no shutdown, nothing finished. A new engine starts on the same store.
    second = FlowEngine(FlowStore(redis, "DEFAULT"), backend, ListLogger())
    second.start(start_triggers=False)
    try:
        record = second.get_run(run_id)
        assert record["status"] == "failed" and record["interrupted"] and "stopped script 1" in record["message"]
        assert 1 in backend.stopped_scripts and store.records("active_runs") == {}
    finally:
        second.shutdown()
        first.stop_all()


# B30 (R-23): a COSMOS API blip used to set every telemetry ref aside for 30 s, blinding every trigger
def test_an_api_outage_does_not_quarantine_every_ref(engine):
    manager = engine.triggers
    calls = {"down": True}

    def read(refs):
        if calls["down"]:
            raise ConnectionError("cmd-tlm-api unavailable")
        return [(1, None)] * len(refs)

    refs = [("A B C", "CONVERTED"), ("A B D", "CONVERTED")]
    first = manager._batched(refs, read)
    assert all(isinstance(v, Exception) for v in first.values())
    assert manager.bad_refs == {}
    calls["down"] = False
    assert all(not isinstance(v, Exception) for v in manager._batched(refs, read).values())


def test_a_single_bad_ref_is_still_set_aside(engine):
    manager = engine.triggers

    def read(refs):
        if ("BAD X Y", "CONVERTED") in refs:
            raise KeyError("no such item")
        return [(1, None)] * len(refs)

    manager._batched([("A B C", "CONVERTED"), ("BAD X Y", "CONVERTED")], read)
    assert list(manager.bad_refs) == [("BAD X Y", "CONVERTED")]
