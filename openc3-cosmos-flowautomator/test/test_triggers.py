import json
import urllib.request

import pytest
from conftest import edge, make_flow, node, wait_until
from flow_automator.http_api import AuthError, FlowApi, FlowHttpServer
from flow_automator.triggers import TriggerManager


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def armed_flow(engine, trigger, policy="parallel"):
    flow = make_flow(
        [trigger, node("c", "send_cmd", command="INST COLLECT with TYPE {{ var('limits_state', 'NORMAL') }}")],
        [edge(trigger["id"], "c")],
        policy=policy,
    )
    saved, problems = engine.save_flow(flow)
    assert problems == []
    engine.set_armed(saved["id"], True)
    return saved


@pytest.fixture
def manager(engine):
    clock = Clock()
    manager = TriggerManager(engine, clock=clock)
    manager.clock_ref = clock
    return manager


def finished(engine, backend, count):
    return wait_until(lambda: len(backend.commands) >= count and not engine.runs)


def test_tlm_trigger_fires_on_rising_edge_only(engine, backend, manager):
    backend.values["INST HEALTH_STATUS TEMP1"] = 60  # already true when armed: must not fire
    armed_flow(engine, node("tr", "trigger_tlm", condition="INST HEALTH_STATUS TEMP1 > 50"))
    manager.tick()
    manager.tick()
    assert backend.commands == []
    backend.values["INST HEALTH_STATUS TEMP1"] = 10
    manager.tick()
    backend.values["INST HEALTH_STATUS TEMP1"] = 70
    manager.tick()
    manager.tick()  # still true: no second fire
    assert finished(engine, backend, 1)
    assert len(backend.commands) == 1


def test_tlm_trigger_cooldown(engine, backend, manager):
    backend.values["INST HEALTH_STATUS TEMP1"] = 0
    armed_flow(engine, node("tr", "trigger_tlm", condition="INST HEALTH_STATUS TEMP1 > 50", cooldown=60))
    manager.tick()
    for value in (60, 0, 60):
        backend.values["INST HEALTH_STATUS TEMP1"] = value
        manager.tick()
    assert finished(engine, backend, 1)
    assert len(backend.commands) == 1
    manager.clock_ref.now += 61
    backend.values["INST HEALTH_STATUS TEMP1"] = 0
    manager.tick()
    backend.values["INST HEALTH_STATUS TEMP1"] = 60
    manager.tick()
    assert finished(engine, backend, 2)


def test_disarmed_flows_do_not_fire(engine, backend, manager):
    backend.values["INST HEALTH_STATUS TEMP1"] = 0
    flow = armed_flow(engine, node("tr", "trigger_tlm", condition="INST HEALTH_STATUS TEMP1 > 50"))
    manager.tick()
    engine.set_armed(flow["id"], False)
    backend.values["INST HEALTH_STATUS TEMP1"] = 60
    manager.tick()
    assert backend.commands == []
    assert manager.states == {}


def test_interval_schedule(engine, backend, manager):
    armed_flow(engine, node("tr", "trigger_schedule", mode="interval", interval=10))
    manager.tick()
    manager.clock_ref.now += 5
    manager.tick()
    assert backend.commands == []
    manager.clock_ref.now += 5
    manager.tick()
    assert finished(engine, backend, 1)
    manager.clock_ref.now += 100  # fell far behind: fire once, not ten times
    manager.tick()
    manager.tick()
    assert finished(engine, backend, 2)
    assert len(backend.commands) == 2


def test_cron_schedule(engine, backend, manager):
    manager.clock_ref.now = 1_800_000_000 - (1_800_000_000 % 3600) + 59 * 60 + 30  # hh:59:30
    armed_flow(engine, node("tr", "trigger_schedule", mode="cron", cron="0 * * * *"))
    manager.tick()
    manager.clock_ref.now += 15
    manager.tick()
    assert backend.commands == []
    manager.clock_ref.now += 30  # crosses into minute 00
    manager.tick()
    manager.tick()
    assert finished(engine, backend, 1)
    assert len(backend.commands) == 1


def test_packet_trigger(engine, backend, manager):
    backend.counts[("INST", "ADCS")] = 5
    armed_flow(engine, node("tr", "trigger_packet", target="inst", packet="adcs"))
    manager.tick()
    manager.tick()
    assert backend.commands == []
    backend.counts[("INST", "ADCS")] = 6
    manager.tick()
    assert finished(engine, backend, 1)


def test_limits_trigger(engine, backend, manager):
    backend.values["INST HEALTH_STATUS TEMP1"] = 0
    backend.limits["INST HEALTH_STATUS TEMP1"] = "GREEN"
    armed_flow(engine, node("tr", "trigger_limits", item="INST HEALTH_STATUS TEMP1", states=["RED_HIGH"]))
    manager.tick()
    backend.limits["INST HEALTH_STATUS TEMP1"] = "YELLOW_HIGH"
    manager.tick()
    assert backend.commands == []
    backend.limits["INST HEALTH_STATUS TEMP1"] = "RED_HIGH"
    manager.tick()
    assert finished(engine, backend, 1)
    assert backend.commands == ["INST COLLECT with TYPE RED_HIGH"]


def test_bad_item_does_not_break_other_triggers(engine, backend, manager):
    backend.values["INST HEALTH_STATUS TEMP1"] = 0
    armed_flow(engine, node("bad", "trigger_tlm", condition="NOPE NOPE NOPE > 1"))
    armed_flow(engine, node("good", "trigger_tlm", condition="INST HEALTH_STATUS TEMP1 > 50"))
    manager.tick()
    backend.values["INST HEALTH_STATUS TEMP1"] = 60
    manager.tick()
    assert finished(engine, backend, 1)
    assert any("does not exist" in error for error in manager.status()["errors"].values())


def test_trigger_thread_runs(engine, backend):
    backend.values["INST HEALTH_STATUS TEMP1"] = 0
    armed_flow(engine, node("tr", "trigger_tlm", condition="INST HEALTH_STATUS TEMP1 > 50"))
    engine.triggers.start()
    try:
        assert wait_until(
            lambda: engine.triggers.states and all(s["initialized"] for s in engine.triggers.states.values())
        )
        backend.values["INST HEALTH_STATUS TEMP1"] = 60
        assert finished(engine, backend, 1)
    finally:
        engine.triggers.stop()


# ---- HTTP API -----------------------------------------------------------------


def fake_authorize(token, write):
    if token is None:
        raise AuthError("Token is required", 401)
    if token == "readonly" and write:
        raise AuthError("Forbidden", 403)


@pytest.fixture
def server(engine):
    server = FlowHttpServer(FlowApi(engine, fake_authorize), 0, host="127.0.0.1")
    server.start()
    yield f"http://127.0.0.1:{server.port}/flow-automator/api"
    server.stop()


def call(url, method="GET", body=None, token="good"):  # noqa: S107 - test token
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if token:
        request.add_header("Authorization", token)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request) as response:  # noqa: S310 - local test server
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_http_api_round_trip(server, backend, engine):
    assert call(f"{server}/health", token=None) == (200, {"ok": True, "store": "ok"})
    assert call(f"{server}/flows", token=None)[0] == 401

    flow = make_flow([node("t", "trigger_manual"), node("c", "send_cmd", command="INST X")], [edge("t", "c")])
    status, created = call(f"{server}/flows", "POST", flow)
    assert status == 200 and created["problems"] == []
    flow_id = created["flow"]["id"]

    assert call(f"{server}/flows/{flow_id}/arm", "POST", {}, token="readonly")[0] == 403
    assert call(f"{server}/flows/{flow_id}/arm", "POST", {})[1]["flow"]["armed"] is True

    status, result = call(f"{server}/flows/{flow_id}/run", "POST", {})
    assert status == 200 and result["result"] == "started"
    assert wait_until(lambda: backend.commands == ["INST X"] and not engine.runs)

    runs = call(f"{server}/runs?flow_id={flow_id}")[1]
    assert runs[0]["status"] == "success"
    assert call(f"{server}/runs/{runs[0]['id']}")[1]["steps"][-1]["node"] == "c"

    assert call(f"{server}/flows")[1][0]["armed"] is True
    assert call(f"{server}/stop_all", "POST", {})[1] == {
        "disarmed": 1,
        "stopped": 0,
        "scripts_stopped": 0,
        "scheduler_paused": True,
    }
    assert call(f"{server}/flows/{flow_id}", "DELETE")[0] == 200
    assert call(f"{server}/flows/{flow_id}")[0] == 404
    assert call(f"{server}/nothing")[0] == 404


def test_http_api_errors(server):
    status, body = call(f"{server}/flows", "POST", make_flow([node("i", "if", condition="(")], []))
    assert status == 200 and body["problems"]
    status, body = call(f"{server}/flows/{body['flow']['id']}/arm", "POST", {})
    assert status == 400 and body["problems"]
    assert call(f"{server}/flows/missing/run", "POST", {})[0] == 404
    assert call(f"{server}/validate", "POST", {"name": "x", "nodes": [], "edges": []})[1]["problems"]
