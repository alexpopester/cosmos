"""Regression tests for the safety fixes from docs/EVALUATION-2026-11-04.md (section 2.1)."""

from conftest import edge, make_flow, node, run_to_end, wait_until


def _walk_flow(command):
    return make_flow(
        [node("t", "trigger_manual"), node("cmd", "send_cmd", command=command)],
        [edge("t", "cmd")],
        mode="walk",
    )


def _approve_first(engine, run_id, before_go=None):
    assert wait_until(lambda: engine.runs[run_id].pending_approvals())
    approval = engine.runs[run_id].pending_approvals()[0]
    if before_go:
        before_go()
    engine.approve(run_id, approval["id"], "go", "alice")
    assert wait_until(lambda: run_id not in engine.runs)
    return approval, engine.get_run(run_id)


# ENG-1 / SEC-2: the command sent is the one the operator approved
def test_command_that_changes_after_approval_is_not_sent(engine, backend):
    backend.hazardous = {"FIRE", "SELFDESTRUCT"}
    backend.values["INST HEALTH_STATUS MODE"] = "FIRE"
    saved, _ = engine.save_flow(_walk_flow("INST {{ INST HEALTH_STATUS MODE }}"))
    run_id = engine.trigger(saved["id"], manual=True)["run"]["id"]

    def change():
        backend.values["INST HEALTH_STATUS MODE"] = "SELFDESTRUCT"

    approval, record = _approve_first(engine, run_id, change)
    assert approval["preview"] == "INST FIRE"
    assert backend.commands == []
    assert record["status"] == "failed" and "changed after it was approved" in record["message"]


def test_approved_hazardous_command_is_sent_when_unchanged(engine, backend):
    backend.hazardous = {"FIRE"}
    backend.values["INST HEALTH_STATUS MODE"] = "FIRE"
    saved, _ = engine.save_flow(_walk_flow("INST {{ INST HEALTH_STATUS MODE }}"))
    run_id = engine.trigger(saved["id"], manual=True)["run"]["id"]
    _approval, record = _approve_first(engine, run_id)
    assert record["status"] == "success" and backend.commands == ["INST FIRE"]


# ENG-2: nested Parallel whose branches go straight to the outer Join runs the tail once
def test_nested_parallel_to_outer_join_runs_tail_once(engine, backend):
    flow = make_flow(
        [
            node("t", "trigger_manual"),
            node("p1", "parallel"),
            node("a", "log", message="a"),
            node("p2", "parallel"),
            node("b1", "log", message="b1"),
            node("b2", "log", message="b2"),
            node("j", "join"),
            node("tail", "send_cmd", command="INST TAIL"),
        ],
        [
            edge("t", "p1"),
            edge("p1", "a"),
            edge("p1", "p2"),
            edge("p2", "b1"),
            edge("p2", "b2"),
            edge("a", "j"),
            edge("b1", "j"),
            edge("b2", "j"),
            edge("j", "tail"),
        ],
    )
    record = run_to_end(engine, flow)
    assert record["status"] == "success", record["message"]
    assert backend.commands == ["INST TAIL"]


def test_properly_nested_parallel_still_continues_past_inner_join(engine, backend):
    flow = make_flow(
        [
            node("t", "trigger_manual"),
            node("p1", "parallel"),
            node("a", "log", message="a"),
            node("p2", "parallel"),
            node("b1", "log", message="b1"),
            node("b2", "log", message="b2"),
            node("j2", "join"),
            node("mid", "send_cmd", command="INST MID"),
            node("j", "join"),
            node("tail", "send_cmd", command="INST TAIL"),
        ],
        [
            edge("t", "p1"),
            edge("p1", "a"),
            edge("p1", "p2"),
            edge("p2", "b1"),
            edge("p2", "b2"),
            edge("b1", "j2"),
            edge("b2", "j2"),
            edge("j2", "mid"),
            edge("mid", "j"),
            edge("a", "j"),
            edge("j", "tail"),
        ],
    )
    record = run_to_end(engine, flow)
    assert record["status"] == "success", record["message"]
    assert backend.commands == ["INST MID", "INST TAIL"]


# ENG-4 / SEC-6 / CON-2: STOP ALL is airtight
def test_stop_all_stops_scripts_started_without_waiting(engine, backend):
    flow = make_flow(
        [node("t", "trigger_manual"), node("s", "run_script", filename="INST/procedures/x.py", wait=False)],
        [edge("t", "s")],
    )
    assert run_to_end(engine, flow)["status"] == "success"
    result = engine.stop_all()
    assert result["scripts_stopped"] == 1 and backend.stopped_scripts == [1]


def test_planner_launch_rechecks_pause(engine):
    planner = engine.planner
    activity = {"id": "a1", "status": "scheduled", "contact": {"los": 0}, "flow": {}}
    planner.activities["a1"] = activity
    planner.settings["paused"] = True
    planner._launch(activity, 0)  # a tick collected it before STOP ALL paused the scheduler
    assert activity["status"] == "scheduled" and not engine.runs


def test_stop_all_pauses_scheduler_before_stopping_runs(engine):
    order = []
    original = engine.planner.set_paused

    def spy(paused, reason=""):
        order.append(("paused", len(engine.runs)))
        return original(paused, reason)

    engine.planner.set_paused = spy
    saved, _ = engine.save_flow(
        make_flow([node("t", "trigger_manual"), node("w", "wait_time", seconds=30)], [edge("t", "w")])
    )
    engine.trigger(saved["id"], manual=True)
    engine.stop_all()
    assert order == [("paused", 1)]  # paused while the run was still there to be stopped


def test_stop_all_works_when_cosmos_cannot_verify_the_token(engine):
    from flow_automator.http_api import AuthError, FlowApi

    def unreachable(token, write):
        if not token:
            raise AuthError("Token is required", 401)
        raise AuthError("Could not verify token: connection refused", 503)

    api = FlowApi(engine, unreachable)
    status, body = api.handle("POST", "/flow-automator/api/stop_all", {}, {"Authorization": "tok"}, b"")
    assert status == 200 and body["scheduler_paused"] is True
    # Anything else still needs a verified token
    status, _ = api.handle("POST", "/flow-automator/api/flows", {}, {"Authorization": "tok"}, b"{}")
    assert status == 503
    status, _ = api.handle("POST", "/flow-automator/api/stop_all", {}, {}, b"")
    assert status == 401


# ENG-3 / ENG-16: queued runs never overlap and don't start after disarm
def test_queued_trigger_starts_inside_the_finish_lock(engine, backend):
    flow = make_flow(
        [node("t", "trigger_manual"), node("w", "wait_time", seconds=0.2), node("c", "send_cmd", command="INST X")],
        [edge("t", "w"), edge("w", "c")],
        policy="queue",
    )
    saved, _ = engine.save_flow(flow)
    slow_add = engine.store.add_run

    def add_run(*args):
        # While history is being written, a new trigger must see the queued run already running
        assert engine.trigger(saved["id"], manual=True)["result"] in ("queued", "skipped")
        return slow_add(*args)

    engine.store.add_run = add_run
    engine.trigger(saved["id"], manual=True)
    engine.trigger(saved["id"], manual=True)
    assert wait_until(lambda: len(backend.commands) >= 2, timeout=5)
    engine.store.add_run = slow_add
    engine.stop_all()


def test_queued_trigger_dropped_when_flow_disarmed(engine, backend):
    flow = make_flow(
        [node("t", "trigger_manual"), node("w", "wait_time", seconds=0.2), node("c", "send_cmd", command="INST X")],
        [edge("t", "w"), edge("w", "c")],
        policy="queue",
    )
    saved, _ = engine.save_flow(flow)
    engine.set_armed(saved["id"], True)
    engine.trigger(saved["id"], manual=True)
    assert engine.trigger(saved["id"], manual=False, reason="tlm")["result"] == "queued"
    engine.set_armed(saved["id"], False)
    assert wait_until(lambda: not engine.runs)
    assert backend.commands == ["INST X"]
