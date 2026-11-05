"""STOP ALL and Valkey hardening from docs/EVALUATION-2026-11-05.md (backlog B3, plus R-11 and E-12)."""

import json
import socket
import threading
import time

import pytest
from conftest import FakeBackend, ListLogger, edge, make_flow, node, wait_until
from flow_automator import http_api
from flow_automator.engine import FlowEngine
from flow_automator.http_api import FlowApi, FlowHttpServer
from flow_automator.store import FlowStore, MemoryRedis
from test_triggers import call, fake_authorize


class FlakyRedis(MemoryRedis):
    """MemoryRedis that refuses connections (down) or never answers (hang) on demand."""

    def __init__(self):
        super().__init__()
        self.down = False
        self.hang = threading.Event()  # set: calls block until released
        self.release = threading.Event()

    def __getattribute__(self, name):
        attr = super().__getattribute__(name)
        if name in ("hgetall", "hset", "hdel", "lpush", "ltrim", "lrange", "delete"):

            def call_through(*args, **kwargs):
                if super(FlakyRedis, self).__getattribute__("hang").is_set():
                    super(FlakyRedis, self).__getattribute__("release").wait(30)
                if super(FlakyRedis, self).__getattribute__("down"):
                    raise ConnectionError("Error 111 connecting to valkey. Connection refused.")
                return attr(*args, **kwargs)

            return call_through
        return attr


@pytest.fixture
def flaky():
    return FlakyRedis()


@pytest.fixture
def fengine(flaky):
    engine = FlowEngine(FlowStore(flaky, "DEFAULT"), FakeBackend(), ListLogger(), trigger_period=0.05)
    engine.start(start_triggers=False)
    yield engine
    flaky.release.set()
    flaky.hang.clear()
    flaky.down = False
    engine.shutdown()


def _waiting_flow(name="Waiter", **extra):
    return make_flow(
        [node("t", "trigger_manual"), node("w", "wait_time", seconds=30), node("c", "send_cmd", command="INST FIRE")],
        [edge("t", "w"), edge("w", "c")],
        name=name,
        **extra,
    )


def _stored_flow(redis, flow_id):
    return json.loads(redis.hashes["flow_automator__DEFAULT__flows"][flow_id])


# E-1: STOP ALL during a Valkey outage stopped nothing and raised
def test_stop_all_works_while_valkey_is_down(fengine, flaky):
    backend = fengine.backend
    saved, _ = fengine.save_flow(_waiting_flow())
    fengine.set_armed(saved["id"], True)
    run_id = fengine.trigger(saved["id"], manual=True)["run"]["id"]
    flaky.down = True

    result = fengine.stop_all("alice")

    assert result["disarmed"] == 1 and result["stopped"] == 1
    assert result["persisted"] is False
    assert fengine.flows[saved["id"]]["armed"] is False
    assert fengine.planner.settings["paused"] is True
    assert wait_until(lambda: run_id not in fengine.runs)
    assert backend.commands == []
    assert _stored_flow(flaky, saved["id"])["armed"] is True  # not written yet

    flaky.down = False
    assert fengine.store.flush() == 0
    assert _stored_flow(flaky, saved["id"])["armed"] is False
    settings = json.loads(flaky.hashes["flow_automator__DEFAULT__planner_settings"]["settings"])
    assert settings["paused"] is True


def test_disarm_takes_effect_while_valkey_is_down_and_is_saved_later(fengine, flaky):
    saved, _ = fengine.save_flow(_waiting_flow())
    fengine.set_armed(saved["id"], True)
    flaky.down = True
    assert fengine.set_armed(saved["id"], False)["armed"] is False
    flaky.down = False
    fengine.store.flush()
    assert _stored_flow(flaky, saved["id"])["armed"] is False


def test_arming_fails_visibly_and_stays_disarmed_while_valkey_is_down(fengine, flaky):
    saved, _ = fengine.save_flow(_waiting_flow())
    flaky.down = True
    with pytest.raises(ConnectionError):
        fengine.set_armed(saved["id"], True)
    assert fengine.flows[saved["id"]]["armed"] is False


def test_failed_delete_keeps_the_flow(fengine, flaky):
    saved, _ = fengine.save_flow(_waiting_flow())
    flaky.down = True
    with pytest.raises(ConnectionError):
        fengine.delete_flow(saved["id"])
    assert saved["id"] in fengine.flows


# P-6 / E-11: a Valkey that never answers used to hold the engine lock (no /status, no STOP ALL)
def test_engine_answers_while_valkey_hangs(fengine, flaky):
    fengine.store.inline_flush = False  # as in the running engine: its store thread writes, not the caller
    saved, _ = fengine.save_flow(_waiting_flow())
    flaky.hang.set()

    started = time.monotonic()
    run_id = fengine.trigger(saved["id"], manual=True)["run"]["id"]
    fengine.status()
    result = fengine.stop_all("alice")
    assert time.monotonic() - started < 1.0
    assert result["stopped"] == 1
    assert wait_until(lambda: run_id not in fengine.runs)
    assert fengine.get_run(run_id)["status"] == "stopped"  # findable before its history reaches Valkey


# R-2: STOP ALL while an approved step re-renders used to still send the command
def test_stop_all_during_approved_step_sends_nothing(engine, backend, monkeypatch):
    from flow_automator.runner import FlowRun

    backend.values["INST HEALTH_STATUS MODE"] = "FIRE"
    flow = make_flow(
        [
            node("t", "trigger_manual"),
            node("c", "send_cmd", command="INST {{ INST HEALTH_STATUS MODE }}", require_approval=True),
        ],
        [edge("t", "c")],
        mode="walk",
    )
    real = FlowRun._wait_for_approval

    def approved_then_stop_all(self, *args, **kwargs):
        result = real(self, *args, **kwargs)
        engine.stop_all("bob")  # lands just after the approval, before the command goes out
        return result

    monkeypatch.setattr(FlowRun, "_wait_for_approval", approved_then_stop_all)
    saved, _ = engine.save_flow(flow)
    run_id = engine.trigger(saved["id"], manual=True)["run"]["id"]
    assert wait_until(lambda: engine.runs[run_id].pending_approvals())
    approval = engine.runs[run_id].pending_approvals()[0]
    engine.approve(run_id, approval["id"], "go", "alice")
    assert wait_until(lambda: run_id not in engine.runs)
    assert backend.commands == []
    assert engine.get_run(run_id)["status"] == "stopped"


# E-10: shutdown used to start the next queued run, which then ran unsupervised
def test_shutdown_starts_nothing(backend):
    engine = FlowEngine(FlowStore(MemoryRedis(), "DEFAULT"), backend, ListLogger(), trigger_period=0.05)
    engine.start(start_triggers=False)
    flow = make_flow(
        [node("t", "trigger_manual"), node("w", "wait_time", seconds=30), node("c", "send_cmd", command="INST AFTER")],
        [edge("t", "w"), edge("w", "c")],
        policy="queue",
    )
    saved, _ = engine.save_flow(flow)
    assert engine.trigger(saved["id"], manual=True)["result"] == "started"
    assert engine.trigger(saved["id"], manual=True)["result"] == "queued"
    engine.shutdown()
    time.sleep(0.5)
    assert engine.runs == {}
    assert backend.commands == []
    assert engine.trigger(saved["id"], manual=True)["result"] == "skipped"


# E-13: STOP ALL used to forget fire-and-forget scripts after 200, and stop them one slow call at a time
def test_stop_all_stops_every_detached_script_at_once(engine, backend):
    for script_id in range(1, 251):
        engine.track_detached_script(script_id)
    real = backend.script_stop

    def slow_stop(script_id):
        time.sleep(0.2)
        real(script_id)

    backend.script_stop = slow_stop
    started = time.monotonic()
    result = engine.stop_all("alice")
    assert result["scripts_stopped"] == 250
    assert time.monotonic() - started < 5


def test_finished_detached_scripts_are_forgotten(engine, backend):
    backend.scripts[1] = ["completed"]
    backend.scripts[2] = ["running"]
    engine.track_detached_script(1)
    engine.track_detached_script(2)
    engine.prune_detached_scripts()
    assert engine.detached_scripts == [2]


# R-11: a buffered write used to bring a deleted record back
def test_delete_after_buffered_write_stays_deleted(fengine, flaky):
    store = fengine.store
    flaky.down = True
    store.save_record("stations", "s1", {"id": "s1", "name": "Edited during outage"}, defer=True)
    flaky.down = False
    store.delete_record("stations", "s1")
    assert store.flush() == 0
    assert "s1" not in store.records("stations")


# E-12: a finished run used to be briefly "not found", and a crash between steps could lose it
def test_run_is_always_findable_and_history_lands_before_active_record_goes(engine):
    flow = make_flow([node("t", "trigger_manual"), node("l", "log", message="x")], [edge("t", "l")])
    saved, _ = engine.save_flow(flow)
    order = []
    store = engine.store
    real_add, real_delete = store.add_run, store.delete_record

    def add_run(*args, **kwargs):
        order.append("history")
        return real_add(*args, **kwargs)

    def delete_record(kind, *args, **kwargs):
        if kind == "active_runs":
            order.append("clear_active")
        return real_delete(kind, *args, **kwargs)

    store.add_run, store.delete_record = add_run, delete_record
    run_id = engine.trigger(saved["id"], manual=True)["run"]["id"]
    while run_id in engine.runs:
        engine.get_run(run_id)  # never raises "not found" while it finishes
    assert engine.get_run(run_id)["status"] == "success"
    assert order == ["history", "clear_active"]


# R-8 / S-10: clients with no valid token used to fill every request slot and lock out STOP ALL
def test_stop_all_works_with_every_request_slot_held(engine, monkeypatch):
    monkeypatch.setattr(http_api, "MAX_CONCURRENT_REQUESTS", 4)
    monkeypatch.setattr(http_api, "PRIORITY_PEEK_SECONDS", 0.5)
    server = FlowHttpServer(FlowApi(engine, fake_authorize), 0, host="127.0.0.1")
    server.start()
    base = f"http://127.0.0.1:{server.port}/flow-automator/api"
    held = []
    try:
        for _ in range(4):  # connections that send a few header bytes, then stall
            sock = socket.create_connection(("127.0.0.1", server.port))
            sock.sendall(b"POST /flow-automator/api/flows HTTP/1.1\r\n")
            held.append(sock)
        time.sleep(0.2)
        assert call(f"{base}/health", token=None)[0] == 200
        status, result = call(f"{base}/stop_all", "POST", {})
        assert status == 200 and result["scheduler_paused"] is True
        sock = socket.create_connection(("127.0.0.1", server.port))
        sock.sendall(b"GET /flow-automator/api/flows HTTP/1.1\r\nAuthorization: good\r\n\r\n")
        assert sock.recv(64).startswith(b"HTTP/1.1 503")
        sock.close()
    finally:
        for sock in held:
            sock.close()
        server.stop()


def test_body_is_not_read_before_the_token_is_checked(engine):
    server = FlowHttpServer(FlowApi(engine, fake_authorize), 0, host="127.0.0.1")
    server.start()
    try:
        sock = socket.create_connection(("127.0.0.1", server.port))
        sock.settimeout(2)
        # A big body that never arrives, with a token that's refused: answered at once, not after a timeout
        sock.sendall(
            b"POST /flow-automator/api/flows/x/arm HTTP/1.1\r\nAuthorization: readonly\r\n"
            b"Content-Length: 100000\r\n\r\n"
        )
        assert sock.recv(64).startswith(b"HTTP/1.1 403")
        sock.close()
    finally:
        server.stop()


# STOP ALL must pause the scheduler even while the planner is busy (holding its lock)
def test_pause_works_while_the_planner_is_busy(engine, monkeypatch):
    from flow_automator import contacts

    monkeypatch.setattr(contacts, "PAUSE_LOCK_SECONDS", 0.2)
    held = threading.Event()
    done = threading.Event()

    def busy():
        with engine.planner.lock:
            held.set()
            done.wait(5)

    thread = threading.Thread(target=busy)
    thread.start()
    held.wait()
    try:
        started = time.monotonic()
        result = engine.stop_all("alice")
        assert time.monotonic() - started < 2
        assert result["scheduler_paused"] is True and engine.planner.settings["paused"] is True
        flow = make_flow([node("t", "trigger_manual"), node("c", "send_cmd", command="INST X")], [edge("t", "c")])
        with pytest.raises(Exception, match="paused"):
            engine.start_scheduled_run(flow, "late launch", {}, "run", trigger_info={"kind": "activity"})
    finally:
        done.set()
        thread.join()
