"""Version history of flows, and what triggered each run."""

import pytest
from conftest import edge, make_flow, node, run_to_end, wait_until
from flow_automator.engine import ConflictError, NotFoundError
from flow_automator.http_api import FlowApi
from flow_automator.versions import diff_flows


def simple(command="INST A", **extra):
    return make_flow([node("t", "trigger_manual"), node("c", "send_cmd", command=command)], [edge("t", "c")], **extra)


def test_every_save_is_a_version_with_who_and_what(engine):
    flow, _ = engine.save_flow(simple(), user="alice", note="first try")
    assert flow["version"] == 1 and flow["saved_by"] == "alice"
    edited = {**flow, "mode": "walk", "nodes": flow["nodes"] + [node("l", "log", message="hi", label="Say hi")]}
    edited["nodes"][1] = {**edited["nodes"][1], "data": {"command": "INST B"}}
    flow, _ = engine.save_flow(edited, flow["id"], user="bob", note="added logging")
    assert flow["version"] == 2

    versions = engine.list_versions(flow["id"])
    assert [(v["version"], v["saved_by"], v["note"]) for v in versions] == [
        (2, "bob", "added logging"),
        (1, "alice", "first try"),
    ]
    assert versions[1]["summary"] == "created"
    assert "added 'Say hi'" in versions[0]["summary"] and "changed 'send_cmd'" in versions[0]["summary"]
    assert "mode 'run' → 'walk'" in versions[0]["summary"]
    assert engine.get_version(flow["id"], 1)["flow"]["nodes"][1]["data"]["command"] == "INST A"

    diff = engine.diff_versions(flow["id"], 1)
    assert diff["nodes_changed"][0]["fields"] == {"command": ["INST A", "INST B"]}
    assert diff["settings"]["mode"] == ["run", "walk"]


def test_saving_unchanged_content_or_arming_keeps_the_version(engine):
    flow, _ = engine.save_flow(simple())
    again, _ = engine.save_flow(flow, flow["id"])
    assert again["version"] == 1
    engine.set_armed(flow["id"], True)
    assert engine.get_flow(flow["id"])["version"] == 1 and len(engine.list_versions(flow["id"])) == 1


def test_saving_over_someone_elses_newer_version_is_refused(engine):
    flow, _ = engine.save_flow(simple(), user="alice")
    engine.save_flow({**flow, "description": "bob's edit"}, flow["id"], user="bob", base_version=1)
    with pytest.raises(ConflictError) as conflict:
        engine.save_flow({**flow, "description": "alice's edit"}, flow["id"], user="alice", base_version=1)
    assert conflict.value.status == 409 and conflict.value.details["saved_by"] == "bob"
    assert conflict.value.details["current_version"] == 2
    forced, _ = engine.save_flow(
        {**flow, "description": "alice's edit"}, flow["id"], user="alice", base_version=1, force=True
    )
    assert forced["version"] == 3 and forced["description"] == "alice's edit"


def test_restoring_an_old_version_makes_a_new_one(engine):
    flow, _ = engine.save_flow(simple("INST A"))
    engine.save_flow(simple("INST B"), flow["id"])
    restored, _ = engine.restore_version(flow["id"], 1, user="carol")
    assert restored["version"] == 3 and restored["nodes"][1]["data"]["command"] == "INST A"
    latest = engine.list_versions(flow["id"])[0]
    assert latest["restored_from"] == 1 and latest["saved_by"] == "carol" and "Restored version 1" in latest["note"]


def test_deleted_flows_can_be_restored_with_their_history(engine, backend):
    flow, _ = engine.save_flow(simple(), user="alice")
    record = run_to_end(engine, engine.get_flow(flow["id"]))
    engine.delete_flow(flow["id"], user="bob")
    assert engine.list_deleted()[0]["deleted_by"] == "bob"
    assert engine.get_run(record["id"])["id"] == record["id"]  # its runs stay findable
    restored, _ = engine.restore_deleted(flow["id"], user="alice")
    assert restored["id"] == flow["id"] and restored["armed"] is False
    assert [v["version"] for v in engine.list_versions(flow["id"])][:2] == [
        restored["version"],
        restored["version"] - 1,
    ]
    assert engine.list_deleted() == []
    with pytest.raises(NotFoundError):
        engine.restore_deleted("nope")


def test_flows_from_before_versions_get_version_1(engine):
    legacy = simple()
    legacy.update(id="legacy1", armed=False, mode="run", policy="skip", variables={})
    engine.store.save_flow(legacy)
    engine.start(start_triggers=False)
    assert engine.get_flow("legacy1")["version"] == 1
    assert engine.list_versions("legacy1")[0]["note"] == "Saved before version history"


def test_diff_ignores_moves_but_counts_them():
    a = simple()
    b = {**a, "nodes": [dict(a["nodes"][0], position={"x": 99, "y": 0}), a["nodes"][1]]}
    diff = diff_flows(a, b)
    assert diff["nodes_moved"] == 1 and diff["summary"] == "moved 1 nodes"


# ---- what triggered each run --------------------------------------------------


def test_runs_record_trigger_version_and_inputs(engine, backend):
    api = FlowApi(engine, lambda token, write: {"username": "alice"})
    flow, _ = engine.save_flow(simple("INST {{ kind }}", parameters=[{"name": "kind", "default": "X"}], template=True))
    import json

    status, result = api.handle(
        "POST", f"/flow-automator/api/flows/{flow['id']}/run", {}, {}, json.dumps({"variables": {"kind": "Y"}})
    )
    assert status == 200
    assert wait_until(lambda: not engine.runs)
    run = engine.get_run(result["run"]["id"])
    assert run["trigger"]["kind"] == "manual" and run["trigger"]["user"] == "alice"
    assert run["flow_version"] == 1 and run["start_variables"]["kind"] == "Y"
    assert run["reason"] == "Manual run by alice"


def test_trigger_runs_record_the_trigger_node(engine, backend):
    from flow_automator.triggers import TriggerManager

    flow, _ = engine.save_flow(
        make_flow(
            [
                node("t", "trigger_tlm", condition="INST HEALTH_STATUS TEMP1 > 50", label="Hot"),
                node("l", "log", message="x"),
            ],
            [edge("t", "l")],
        )
    )
    engine.set_armed(flow["id"], True)
    backend.values["INST HEALTH_STATUS TEMP1"] = 10
    manager = TriggerManager(engine)
    manager.tick()
    backend.values["INST HEALTH_STATUS TEMP1"] = 60
    manager.tick()
    assert wait_until(lambda: engine.list_runs(flow["id"]) and not engine.runs)
    run = engine.list_runs(flow["id"])[0]
    assert run["trigger"]["kind"] == "trigger_tlm" and run["trigger"]["node_label"] == "Hot"
    assert "INST HEALTH_STATUS TEMP1 > 50" in run["trigger"]["detail"]


def test_subflow_runs_record_their_parent(engine):
    child, _ = engine.save_flow(
        make_flow([node("t", "trigger_manual"), node("l", "log", message="c")], [edge("t", "l")], name="Child")
    )
    parent = make_flow(
        [node("t", "trigger_manual"), node("s", "subflow", flow_id=child["id"])], [edge("t", "s")], name="Parent"
    )
    record = run_to_end(engine, parent)
    child_run = engine.list_runs(child["id"])[0]
    assert child_run["trigger"]["kind"] == "subflow" and child_run["trigger"]["parent_run_id"] == record["id"]


def test_run_history_filters(engine, backend):
    flow, _ = engine.save_flow(simple())
    for _ in range(3):
        run_to_end(engine, engine.get_flow(flow["id"]))
    backend.fail_commands.add("A")
    run_to_end(engine, engine.get_flow(flow["id"]))
    dry = engine.trigger(flow["id"], manual=True, dry_run=True)
    assert wait_until(lambda: not engine.runs)
    assert len(engine.list_runs(status="failed")) == 1
    assert [r["id"] for r in engine.list_runs(dry_run=True)] == [dry["run"]["id"]]
    assert len(engine.list_runs(trigger="manual", dry_run=False)) == 4
    assert engine.list_runs(search="failed")[0]["status"] == "failed"
    newest = engine.list_runs()[0]["started_at"]
    assert engine.list_runs(since=newest + 1) == []
