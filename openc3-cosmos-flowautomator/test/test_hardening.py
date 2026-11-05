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


# ENG-1 / SEC-2, amended by B6 (R-7): the command sent is exactly the one the operator approved, even if the
# telemetry it was rendered from changed meanwhile (it is never rendered again)
def test_command_is_sent_exactly_as_approved(engine, backend):
    backend.hazardous = {"FIRE", "SELFDESTRUCT"}
    backend.values["INST HEALTH_STATUS MODE"] = "FIRE"
    saved, _ = engine.save_flow(_walk_flow("INST {{ INST HEALTH_STATUS MODE }}"))
    run_id = engine.trigger(saved["id"], manual=True)["run"]["id"]

    def change():
        backend.values["INST HEALTH_STATUS MODE"] = "SELFDESTRUCT"

    approval, record = _approve_first(engine, run_id, change)
    assert approval["preview"] == "INST FIRE" and approval["hazardous"] is True
    assert backend.commands == ["INST FIRE"]
    assert record["status"] == "success"


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


# SEC-1 / SEC-4 / ENG-5: expressions can't stall or exhaust the engine
import time  # noqa: E402

import pytest  # noqa: E402
from flow_automator.expressions import Evaluator, ExpressionError, parse  # noqa: E402


@pytest.mark.parametrize(
    "source",
    [
        "((9**1000)**1000)**1000",
        "1 << 100000000",
        "str([[0] * 99999] * 99999)",
        "[[0] * 99999] * 99999",
        "sum([[0] * 3000] * 3000, [])",
        "format(1, '>999999999')",
        "round(1, -3000000)",
        "replace(s, '', s)",
        "s + s",
        "join(',', [s, s])",
        "'%999999999d' % 1",
        "b'a' * 50000000",
        "1j",
    ],
)
def test_expression_bombs_are_refused_quickly(source):
    evaluator = Evaluator({"s": "a" * 60_000})
    started = time.monotonic()
    with pytest.raises(ExpressionError):
        evaluator.evaluate(source)
    assert time.monotonic() - started < 1.0


def test_huge_or_deep_sources_are_refused():
    with pytest.raises(ExpressionError, match="longer than"):
        parse("[" + "1," * 1_000_000 + "]")
    with pytest.raises(ExpressionError):
        parse("+".join(["1"] * 4000))  # within the length limit, too many parts
    with pytest.raises(ExpressionError):
        parse("(" * 3000 + "1" + ")" * 3000)


def test_ordinary_expressions_still_work():
    evaluator = Evaluator({"s": "abc", "values": [1, 2, 3]})
    assert evaluator.evaluate("sum(values)") == 6
    assert evaluator.evaluate("round(3.14159, 2)") == 3.14
    assert evaluator.evaluate("format(3.14159, '8.3f')") == "   3.142"
    assert evaluator.evaluate("replace(s, 'b', 'xx')") == "axxc"
    assert evaluator.evaluate("join('-', values)") == "1-2-3"
    assert evaluator.evaluate("2 ** 100") == 2**100
    assert evaluator.evaluate("s * 3 + 'd'") == "abcabcabcd"
    assert evaluator.evaluate("10 % 3") == 1


def test_validate_needs_write_permission(engine):
    from flow_automator.http_api import AuthError, FlowApi

    def readonly(token, write):
        if write:
            raise AuthError("Changing flows needs the script_run permission", 403)
        return {"username": "viewer"}

    api = FlowApi(engine, readonly)
    status, _ = api.handle("POST", "/flow-automator/api/validate", {}, {"Authorization": "t"}, b"{}")
    assert status == 403


# SEC-5 / ENG-16: fan-out is bounded
def test_parallel_branch_limit_in_validation():
    from flow_automator.graph import MAX_PARALLEL_BRANCHES, validate_flow

    count = MAX_PARALLEL_BRANCHES + 1
    nodes = [node("t", "trigger_manual"), node("p", "parallel")] + [
        node(f"l{i}", "log", message="x") for i in range(count)
    ]
    edges = [edge("t", "p")] + [edge("p", f"l{i}") for i in range(count)]
    problems = validate_flow(make_flow(nodes, edges))
    assert any("at most" in p["message"] for p in problems)


def test_branch_thread_budget_fails_the_run(engine, backend, monkeypatch):
    import flow_automator.engine as engine_module

    monkeypatch.setattr(engine_module, "MAX_BRANCH_THREADS", 1)
    flow = make_flow(
        [
            node("t", "trigger_manual"),
            node("p", "parallel"),
            node("a", "log", message="a"),
            node("b", "log", message="b"),
        ],
        [edge("t", "p"), edge("p", "a"), edge("p", "b")],
    )
    record = run_to_end(engine, flow)
    assert record["status"] == "failed" and "too many parallel branches" in record["message"]
    assert engine.branch_threads == 0


def test_subflows_count_against_active_runs(engine, monkeypatch):
    import flow_automator.engine as engine_module

    child, _ = engine.save_flow(
        make_flow([node("t", "trigger_manual"), node("l", "log", message="c")], [edge("t", "l")])
    )
    monkeypatch.setattr(engine_module, "MAX_ACTIVE_RUNS", 1)
    flow = make_flow(
        [node("t", "trigger_manual"), node("s", "subflow", flow_id=child["id"])],
        [edge("t", "s")],
    )
    record = run_to_end(engine, flow)
    assert record["status"] == "failed" and "Too many active runs" in record["message"]


def test_max_steps_is_clamped(engine):
    from flow_automator.runner import HARD_MAX_STEPS, FlowRun

    flow = make_flow([node("t", "trigger_manual")], [], max_steps=10**12)
    run = FlowRun(engine, flow, "t", "test", {})
    assert run.max_steps == HARD_MAX_STEPS


# SEC-8 / ENG-18: the flow's mode is a floor, and relaxing an armed flow disarms it
def test_run_cannot_use_a_less_cautious_mode(engine, backend):
    saved, _ = engine.save_flow(_walk_flow("INST X"))
    from flow_automator.engine import EngineError

    with pytest.raises(EngineError, match="mode is walk"):
        engine.trigger(saved["id"], manual=True, mode="run")
    # More cautious is fine, and so is a dry run (it sends nothing)
    assert engine.trigger(saved["id"], manual=True, mode="crawl")["result"] == "started"
    engine.stop_all()
    assert wait_until(lambda: not engine.runs)
    assert engine.trigger(saved["id"], manual=True, mode="run", dry_run=True)["result"] == "started"


def test_relaxing_an_armed_flow_disarms_it(engine):
    saved, _ = engine.save_flow(_walk_flow("INST X"))
    engine.set_armed(saved["id"], True)
    renamed, _ = engine.save_flow({**saved, "name": "Renamed"}, saved["id"])
    assert renamed["armed"] is True  # harmless edits keep it armed
    relaxed = {**renamed, "nodes": [dict(n) for n in renamed["nodes"]]}
    relaxed["nodes"][1] = {**relaxed["nodes"][1], "data": {**relaxed["nodes"][1]["data"], "allow_hazardous": True}}
    flow, _ = engine.save_flow(relaxed, saved["id"])
    assert flow["armed"] is False and "Allow hazardous" in flow["disarmed_reason"]
    engine.set_armed(saved["id"], True)
    flow, _ = engine.save_flow({**engine.get_flow(saved["id"]), "mode": "run"}, saved["id"])
    assert flow["armed"] is False and "mode walk -> run" in flow["disarmed_reason"]
    assert "disarmed_reason" not in engine.set_armed(saved["id"], True)


# SEC-7 / CON-16: webhooks can't reach metadata services, follow redirects or read responses back
def test_webhook_to_link_local_is_refused(engine):
    from flow_automator.alerts import AlertError, blocked_address

    assert blocked_address("169.254.169.254")
    assert blocked_address("fe80::1")
    assert blocked_address("localhost") is None  # on-prem receivers are allowed
    assert blocked_address("10.1.2.3") is None
    with pytest.raises(AlertError, match="link-local"):
        engine.alerts.save_settings({"webhooks": [{"name": "meta", "url": "http://169.254.169.254/latest"}]})


def _local_server(status, headers=None, body=b"SECRET internal page"):
    import http.server
    import threading

    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            seen.append((self.path, self.headers.get("Host"), self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(status)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


def test_webhook_errors_do_not_echo_the_response():
    import flow_automator.alerts as alerts

    server, seen = _local_server(500)
    try:
        with pytest.raises(RuntimeError) as error:
            alerts.Alerter._post(f"http://127.0.0.1:{server.server_port}/hook?token=x", {"a": 1})
        assert "SECRET" not in str(error.value) and "500" in str(error.value)
        assert seen[0][0] == "/hook?token=x" and seen[0][2] == b'{"a": 1}'
    finally:
        server.shutdown()


def test_webhook_redirects_are_not_followed():
    import flow_automator.alerts as alerts

    server, seen = _local_server(302, {"Location": "http://169.254.169.254/latest/meta-data/"})
    try:
        with pytest.raises(RuntimeError, match="redirected"):
            alerts.Alerter._post(f"http://127.0.0.1:{server.server_port}/hook", {})
        assert len(seen) == 1
    finally:
        server.shutdown()


# B33 (R-24): the name was resolved once to check it and again to connect (DNS rebinding)
def test_webhook_connects_to_the_address_it_checked(monkeypatch):
    import socket

    import flow_automator.alerts as alerts

    server, seen = _local_server(204, body=b"")
    answers = iter(["127.0.0.1", "169.254.169.254", "169.254.169.254"])
    real = socket.getaddrinfo

    def rebinding(host, *args, **kwargs):
        if host == "hooks.rebind.test":
            address = next(answers)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]
        return real(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", rebinding)
    try:
        alerts.Alerter._post(f"http://hooks.rebind.test:{server.server_port}/hook", {})
        assert len(seen) == 1 and seen[0][1].startswith("hooks.rebind.test")
    finally:
        server.shutdown()


@pytest.mark.parametrize("address", ["fd00:ec2::254", "100.100.100.200", "::ffff:169.254.169.254"])
def test_cloud_metadata_addresses_are_blocked(monkeypatch, address):
    import socket

    import flow_automator.alerts as alerts

    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(family, socket.SOCK_STREAM, 6, "", (address, 0))])
    assert alerts.blocked_address("metadata.example")


def test_webhook_tests_are_rate_limited(engine):
    from flow_automator.alerts import Alerter, AlertError

    alerter = Alerter(engine.store, post=lambda url, payload: None, clock=lambda: 1000.0)
    alerter.settings["webhooks"] = [
        {"id": "h", "name": "h", "url": "http://10.0.0.5/x", "format": "generic", "events": [], "enabled": True}
    ]
    assert alerter.test("h")["ok"] is True
    with pytest.raises(AlertError, match="between tests"):
        alerter.test("h")
