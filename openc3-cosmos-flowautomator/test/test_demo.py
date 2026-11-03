"""The demo loads, is valid, can be reloaded, and its first pass is minutes away."""

import time

import pytest
from conftest import wait_until
from flow_automator.demo import PREFIX, clear_demo, load_demo


pytest.importorskip("sgp4")


@pytest.fixture
def demo_backend(backend):
    for i in range(1, 5):
        backend.values[f"INST HEALTH_STATUS TEMP{i}"] = 10.0 * i
    for item in ("Q1", "Q2"):
        backend.values[f"INST ADCS {item}"] = 0.5
    backend.values["INST HEALTH_STATUS COLLECTS"] = 3
    return backend


def test_demo_loads_and_reloads(engine, demo_backend):
    summary = load_demo(engine)
    assert summary["flows"] == 6 and summary["script_problem"] is None
    assert 120 < summary["first_pass"]["aos_in"] < 480 and summary["first_pass"]["max_elevation"] > 40
    assert summary["rule_activities"] >= 1
    flows = [f for f in engine.list_flows() if f["name"].startswith(PREFIX)]
    assert len(flows) == 6 and all(f["problems"] == 0 for f in flows)
    assert next(f for f in flows if f["name"] == f"{PREFIX}Hot temperature watch")["armed"] is True
    planner = engine.planner
    assert {s["name"] for s in planner.satellites.values()} == {"DEMOSAT-1", "DEMOSAT-2"}
    assert len(planner.stations) == 3 and len(planner.rules) == 1
    assert "FLOW_RESULT_KEY" in demo_backend.scripts_saved["INST/procedures/flow_demo_measure.py"]
    assert wait_until(lambda: not engine.runs)
    dry = engine.list_runs(dry_run=True)
    assert dry and dry[0]["status"] == "success", dry and dry[0]["message"]

    # Loading again resets it rather than duplicating
    again = load_demo(engine)
    assert again["cleared"]["flows"] == 6 and again["cleared"]["satellites"] == 2
    assert len([f for f in engine.list_flows() if f["name"].startswith(PREFIX)]) == 6
    assert len(planner.satellites) == 2 and len(planner.rules) == 1
    assert engine.list_deleted() == []


def test_demo_leaves_other_things_alone(engine, demo_backend):
    from conftest import edge, make_flow, node

    mine, _ = engine.save_flow(
        make_flow([node("t", "trigger_manual"), node("l", "log", message="x")], [edge("t", "l")], name="Mine")
    )
    engine.planner.save_station({"name": "My station", "latitude": 1, "longitude": 2})
    load_demo(engine)
    removed = clear_demo(engine)
    assert removed["flows"] == 6 and removed["rules"] == 1
    assert [f["name"] for f in engine.list_flows()] == ["Mine"]
    assert [s["name"] for s in engine.planner.stations.values()] == ["My station"]
    assert mine["id"] in engine.flows


@pytest.mark.parametrize("name", ["Health check", "Parallel checks", "Script result"])
def test_demo_flows_run(engine, demo_backend, name):
    load_demo(engine)
    flow = next(f for f in engine.flows.values() if f["name"] == f"{PREFIX}{name}")
    original = demo_backend.script_start

    def start(filename, environment):
        demo_backend.stash[environment["FLOW_RESULT_KEY"]] = {"average": 25.0, "hottest": 40.0}
        return original(filename, environment)

    demo_backend.script_start = start
    run_id = engine.trigger(flow["id"], manual=True, mode="run", dry_run=name == "Health check")["run"]["id"]
    assert wait_until(lambda: run_id not in engine.runs, 10)
    record = engine.get_run(run_id)
    assert record["status"] == "success", record["message"]
    if name == "Parallel checks":
        assert "3 branches" in record["steps"][-1]["message"]
    if name == "Script result":
        assert "average 25.0" in record["steps"][-1]["message"]
    time.sleep(0)
