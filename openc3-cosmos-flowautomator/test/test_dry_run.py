"""Dry runs, safe command values, parameter constraints and alerts."""

import time

import pytest
from conftest import edge, make_flow, node, run_to_end, wait_until
from flow_automator.alerts import AlertError
from flow_automator.engine import EngineError
from flow_automator.templates import fill_parameters


# ---- dry runs ----------------------------------------------------------------


def chain(*nodes):
    nodes = [node("t", "trigger_manual"), *nodes]
    return make_flow(nodes, [edge(a["id"], b["id"]) for a, b in zip(nodes, nodes[1:], strict=False)])


def test_dry_run_sends_nothing_but_checks_commands(engine, backend):
    flow = chain(
        node("c", "send_cmd", command="INST COLLECT with TYPE NORMAL"),
        node("s", "run_script", filename="INST/procedures/x.py"),
        node("v", "set_tlm", item="INST HEALTH_STATUS TEMP1", value="5"),
        node("n", "notify", message="hello"),
    )
    record = run_to_end(engine, flow, dry_run=True)
    assert record["status"] == "success" and record["dry_run"] is True
    assert backend.commands == [] and backend.started_scripts == [] and backend.set_calls == []
    messages = {s["node"]: s["message"] for s in record["steps"]}
    assert messages["c"] == "DRY RUN: would send INST COLLECT with TYPE NORMAL"
    assert messages["s"].startswith("DRY RUN: would run INST/procedures/x.py")
    assert messages["n"].startswith("DRY RUN: would notify")

    backend.bad_commands["COLLECT"] = ["TYPE = 'FAST' is not one of NORMAL, SPECIAL"]
    failed = run_to_end(engine, flow, dry_run=True)
    assert failed["status"] == "failed" and "Command check failed" in failed["message"]


def test_dry_run_can_skip_waits(engine, backend):
    backend.values["INST HEALTH_STATUS TEMP1"] = 10
    flow = chain(
        node("w", "wait_time", seconds=30),
        node("k", "wait_check", condition="INST HEALTH_STATUS TEMP1 > 50", timeout=30),
        node("c", "send_cmd", command="INST X"),
    )
    started = time.monotonic()
    record = run_to_end(engine, flow, dry_run=True, skip_waits=True)
    assert time.monotonic() - started < 2 and record["status"] == "success"
    messages = {s["node"]: s["message"] for s in record["steps"]}
    assert messages["w"] == "DRY RUN: skipped a 30s wait"
    assert "assumed it comes true" in messages["k"]
    with pytest.raises(AssertionError):  # without skip_waits it really waits (and the test times out)
        run_to_end(engine, chain(node("w", "wait_time", seconds=30)), timeout=0.5, dry_run=True)
    engine.stop_all()


def test_flow_set_to_dry_run_and_subflows_inherit(engine, backend):
    child, _ = engine.save_flow(chain(node("c", "send_cmd", command="INST CHILD")) | {"name": "Child"})
    parent = chain(node("s", "subflow", flow_id=child["id"]), node("c", "send_cmd", command="INST PARENT"))
    parent["dry_run"] = True
    record = run_to_end(engine, parent)
    assert record["dry_run"] and backend.commands == []
    assert engine.list_runs(child["id"])[0]["dry_run"] is True


def test_dry_run_still_asks_for_approvals(engine, backend):
    backend.hazardous.add("CLEAR")
    flow, _ = engine.save_flow(chain(node("c", "send_cmd", command="INST CLEAR")) | {"mode": "walk"})
    run_id = engine.trigger(flow["id"], manual=True, dry_run=True)["run"]["id"]
    assert wait_until(lambda: engine.status()["approvals"])
    approval = engine.status()["approvals"][0]
    assert "HAZARDOUS" in approval["reason"]
    engine.approve(run_id, approval["id"], "go", "alice")
    assert wait_until(lambda: run_id not in engine.runs)
    assert engine.get_run(run_id)["status"] == "success" and backend.commands == []


# ---- command values can't change the command -----------------------------------


@pytest.mark.parametrize(
    ("value", "template", "ok"),
    [
        ("NORMAL", "INST COLLECT with TYPE {{ v }}", True),
        (2.5, "INST COLLECT with DURATION {{ v }}", True),
        ("NORMAL, DURATION 99", "INST COLLECT with TYPE {{ v }}", False),
        ("NORMAL' X", "INST COLLECT with TYPE {{ v }}", False),
        ("COLLECT with TYPE SPECIAL", "INST {{ v }}", False),
        ("a, b and c", "INST SETPARAMS with NOTE '{{ v }}'", True),
        ("it's", "INST SETPARAMS with NOTE '{{ v }}'", False),
        ("it's", 'INST SETPARAMS with NOTE "{{ v }}"', True),
        ("line\nbreak", "INST SETPARAMS with NOTE '{{ v }}'", False),
        # COSMOS treats a backslash in quotes as an escape and [ ] as one array token (ENG-6)
        ("\\", 'INST SETPARAMS with NOTE "{{ v }}", B 2', False),
        ("[1", "INST SETPARAMS with A {{ v }}, B 2, C 3]", False),
        ("1 B 2", "INST SETPARAMS with A {{ v }}", False),
        ("COLLECT", "INST {{ v }} with TYPE NORMAL", True),
    ],
)
def test_template_values_cannot_inject(engine, backend, value, template, ok):
    flow = chain(node("c", "send_cmd", command=template))
    flow["variables"] = {"v": value}
    record = run_to_end(engine, flow)
    assert (record["status"] == "success") is ok, record["message"]
    if not ok:
        assert "can't go into a command" in record["message"] and backend.commands == []


def test_allow_raw_values_turns_the_check_off(engine, backend):
    flow = chain(node("c", "send_cmd", command="INST COLLECT with TYPE {{ v }}", allow_raw_values=True))
    flow["variables"] = {"v": "NORMAL, DURATION 2"}
    assert run_to_end(engine, flow)["status"] == "success"
    assert backend.commands == ["INST COLLECT with TYPE NORMAL, DURATION 2"]


def test_parameter_constraints():
    params = [
        {"name": "rate", "type": "number", "min": 1, "max": 10, "integer": True},
        {"name": "code", "type": "string", "pattern": "[A-Z]{3}", "max_length": 3},
    ]
    assert fill_parameters(params, {"rate": "4", "code": "ABC"}) == ({"rate": 4, "code": "ABC"}, [])
    _vars, problems = fill_parameters(params, {"rate": 11, "code": "abc"})
    assert any("above the maximum 10" in p for p in problems) and any("doesn't match" in p for p in problems)
    _vars, problems = fill_parameters(params, {"rate": 2.5, "code": "ABCD"})
    assert any("whole number" in p for p in problems) and any("longer than 3" in p for p in problems)
    from flow_automator.templates import check_parameters

    assert check_parameters([{"name": "x", "pattern": "("}])[0].startswith("Parameter 'x': pattern is not")
    assert check_parameters([{"name": "x", "type": "number", "min": 5, "max": 1}]) == [
        "Parameter 'x': min is more than max"
    ]


# ---- simulated contacts and rehearsals -------------------------------------------


def test_manual_run_with_a_simulated_contact(engine, backend):
    flow = chain(
        node("w", "wait_contact", anchor="aos", offset=0),
        node("l", "log", message="{{ contact_satellite }} {{ round(time_to_los()) }}"),
    )
    saved, _ = engine.save_flow(flow | {"template": True})
    variables = engine.planner.contact_variables({"aos_in": 0.3, "duration": 100, "satellite": "SIM-1"})
    run_id = engine.trigger(saved["id"], manual=True, variables=variables)["run"]["id"]
    assert wait_until(lambda: run_id not in engine.runs, 3)
    record = engine.get_run(run_id)
    assert record["status"] == "success"
    assert record["steps"][-1]["message"].startswith("SIM-1 ")


def test_rehearsing_an_activity_leaves_it_scheduled(engine, backend):
    from test_contacts import DC, EPOCH, ISS_1, ISS_2, Clock, first_contact, template

    planner = engine.planner
    planner.clock = Clock(EPOCH)
    planner.horizon = 86400
    planner.save_satellite({"name": "ISS", "tle": f"{ISS_1}\n{ISS_2}"})
    planner.save_station(DC)
    flow = template(engine)
    activity = planner.create_activity(
        {"template_id": flow["id"], "contact_id": first_contact(planner)["id"], "mode": "run"}
    )
    run = planner.rehearse(activity["id"], user="alice")
    assert wait_until(lambda: run.id not in engine.runs)
    record = engine.get_run(run.id)
    assert record["dry_run"] and record["status"] == "success" and backend.commands == []
    assert record["trigger"]["kind"] == "rehearsal" and record["trigger"]["activity_id"] == activity["id"]
    assert planner.activities[activity["id"]]["status"] == "scheduled"


# ---- alerts ------------------------------------------------------------------


@pytest.fixture
def alerts(engine):
    sent = []
    posted = []
    engine.alerts.notify = lambda level, message, url: sent.append((level, message, url))
    engine.alerts.post = lambda url, payload: posted.append((url, payload))
    engine.alerts.sent_log = sent
    engine.alerts.posted_log = posted
    engine.alerts.settings["approval_escalate_after"] = 0  # alert at once (the delay is tested below)
    return engine.alerts


def test_approval_waiting_and_failures_reach_cosmos_notifications(engine, backend, alerts):
    flow, _ = engine.save_flow(chain(node("c", "send_cmd", command="INST X")) | {"mode": "crawl", "name": "Pass"})
    run_id = engine.trigger(flow["id"], manual=True)["run"]["id"]
    assert wait_until(lambda: alerts.sent_log)
    level, message, url = alerts.sent_log[0]
    assert level == "alert" and "'Pass' is waiting for approval" in message
    assert url == f"/tools/flowautomator?flow={flow['id']}&run={run_id}"
    engine.approve(run_id, engine.status()["approvals"][0]["id"], "abort")
    assert wait_until(lambda: run_id not in engine.runs)

    backend.fail_commands.add("X")
    run_to_end(engine, chain(node("c", "send_cmd", command="INST X")) | {"name": "Breaks"})
    assert any("'Breaks' failed" in m for _l, m, _u in alerts.sent_log)


def test_webhooks_get_events_in_their_format_and_urls_stay_secret(engine, backend, alerts):
    saved = alerts.save_settings(
        {
            "base_url": "https://cosmos.example",
            "webhooks": [
                {
                    "name": "Ops Slack",
                    "url": "https://hooks.slack.com/services/T0/B0/secret",
                    "format": "slack",
                    "events": ["run_failed"],
                },
                {
                    "name": "Pager",
                    "url": "https://relay.example/hook",
                    "format": "generic",
                    "events": ["run_failed"],
                    "include_dry_runs": True,
                },
            ],
        }
    )
    assert saved["webhooks"][0]["url_masked"] == "https://hooks.slack.com/…" and "url" not in saved["webhooks"][0]
    backend.fail_commands.add("X")
    run_to_end(engine, chain(node("c", "send_cmd", command="INST X")) | {"name": "Breaks"})
    slack = next(p for u, p in alerts.posted_log if "slack" in u)
    generic = next(p for u, p in alerts.posted_log if "relay" in u)
    assert "*'Breaks' failed*" in slack["text"] and "<https://cosmos.example/tools/flowautomator?flow=" in slack["text"]
    assert generic["event"] == "run_failed" and generic["url"].startswith("https://cosmos.example/")

    # Saving without the URL keeps it; dry runs only go to hooks that ask for them
    alerts.save_settings({**saved, "webhooks": [{**h, "url": ""} for h in saved["webhooks"]]})
    assert alerts.settings["webhooks"][0]["url"].endswith("/secret")
    alerts.posted_log.clear()
    alerts.sent_log.clear()
    backend.bad_commands["X"] = ["nope"]
    run_to_end(engine, chain(node("c", "send_cmd", command="INST X")) | {"name": "Dry"}, dry_run=True)
    assert [u for u, _p in alerts.posted_log] == ["https://relay.example/hook"] and alerts.posted_log[0][1]["dry_run"]
    assert alerts.sent_log == []  # dry runs don't ring the COSMOS bell
    with pytest.raises(AlertError, match="http"):
        alerts.save_settings({"webhooks": [{"name": "bad", "url": "file:///etc/passwd"}]})


def test_repeats_are_dropped_and_test_alerts(engine, alerts):
    assert alerts.emit("stop_all", "x", key="k") is True
    assert alerts.emit("stop_all", "x", key="k") is False
    alerts.save_settings({"webhooks": [{"id": "h1", "name": "Hook", "url": "https://h.example/x"}]})
    assert alerts.test("h1") == {"ok": True, "error": None}
    assert alerts.test() == {"ok": True}
    alerts.post = lambda url, payload: (_ for _ in ()).throw(RuntimeError("HTTP 500"))
    assert alerts.test("h1")["error"] == "HTTP 500"
    assert alerts.settings_json()["webhooks"][0]["status"]["failed"] == 1


def test_activity_missed_and_stop_all_alert(engine, alerts):
    engine.stop_all("alice")
    assert any("STOP ALL by alice" in m for _l, m, _u in alerts.sent_log)


def test_notify_node(engine, alerts):
    run_to_end(engine, chain(node("n", "notify", title="Pass {{ 1 + 1 }}", message="done", level="alert")))
    assert ("alert", "Flow Automator: Pass 2: done") in [(level, m) for level, m, _u in alerts.sent_log]


def test_run_rejects_bad_parameters_before_starting(engine):
    flow, _ = engine.save_flow(
        chain(node("l", "log", message="x"))
        | {"template": True, "parameters": [{"name": "n", "type": "number", "max": 3}]}
    )
    with pytest.raises(EngineError, match="above the maximum"):
        engine.trigger(flow["id"], manual=True, variables={"n": 5})


# ---- richer data: functions, script results, packets, isolation, asking ---------


def test_text_collection_and_math_functions():
    from flow_automator.expressions import Evaluator, ExpressionError

    ev = Evaluator({"name": " inst ", "items": ["a", "b"], "d": {"x": 1}, "t": 21.456})
    assert ev.evaluate("upper(strip(name))") == "INST"
    assert ev.evaluate("join(',', items)") == "a,b"
    assert ev.evaluate("split('A B C')") == ["A", "B", "C"]
    assert ev.evaluate("format(t, '.1f')") == "21.5"
    assert ev.evaluate("get(d, 'y', 5)") == 5 and ev.evaluate("contains(keys(d), 'x')") is True
    assert ev.evaluate("sum(range(5))") == 10 and ev.evaluate("floor(sqrt(17))") == 4
    assert ev.evaluate("utc(0)") == "1970-01-01T00:00:00.000Z"
    assert ev.render("{{ hex(255) }}") == "0xff"
    with pytest.raises(ExpressionError, match="format spec"):
        ev.evaluate("format(t, '{0}')")
    with pytest.raises(ExpressionError, match="limited"):
        ev.evaluate("range(100000)")


def test_scripts_hand_values_back(engine, backend):
    flow = chain(
        node("s", "run_script", filename="INST/procedures/measure.py", result_variable="measured"),
        node("c", "send_cmd", command="INST SET with VALUE {{ measured['temp'] }}"),
    )
    original = backend.script_start

    def start(filename, environment):
        backend.stash[environment["FLOW_RESULT_KEY"]] = {"temp": 21.5}
        return original(filename, environment)

    backend.script_start = start
    record = run_to_end(engine, flow)
    assert record["status"] == "success", record["message"]
    assert backend.commands == ["INST SET with VALUE 21.5"]
    assert record["variables"]["measured"] == {"temp": 21.5} and backend.stash == {}


def test_several_variables_and_read_packet(engine, backend):
    backend.values.update({"INST HEALTH_STATUS TEMP1": 10.0, "INST HEALTH_STATUS TEMP2": 30.0})
    flow = chain(
        node("p", "read_packet", target="INST", packet="HEALTH_STATUS", variable="hs"),
        node(
            "v",
            "set_var",
            name="avg",
            expression="(hs['TEMP1'] + hs['TEMP2']) / 2",
            more={"hot": "avg > 15", "label": "upper('ok') if hot else 'cold'"},
        ),
        node("c", "send_cmd", command="INST SET with AVG {{ avg }}, STATE {{ label }}"),
    )
    record = run_to_end(engine, flow)
    assert record["status"] == "success", record["message"]
    assert backend.commands == ["INST SET with AVG 20.0, STATE OK"]


def test_isolated_parallel_branches_keep_their_own_variables(engine):
    nodes = [
        node("t", "trigger_manual"),
        node("p", "parallel", isolate=True),
        node("a", "set_var", name="x", expression="1", more={"a_only": "'A'"}),
        node("b", "set_var", name="x", expression="2"),
        node("j", "join"),
        node("l", "log", message="{{ x }} {{ a_only }} {{ len(branch_results) }}"),
    ]
    edges = [edge("t", "p"), edge("p", "a"), edge("p", "b"), edge("a", "j"), edge("b", "j"), edge("j", "l")]
    record = run_to_end(engine, make_flow(nodes, edges))
    assert record["status"] == "success", record["message"]
    assert record["steps"][-1]["message"] == "2 A 2"  # later branch wins x; both branches kept
    assert record["variables"]["branch_results"] == [{"x": 1, "a_only": "A"}, {"x": 2}]


def test_wait_for_packet(engine, backend):
    import threading

    backend.counts[("INST", "ADCS")] = 5

    def flow(timeout, handle):
        return make_flow(
            [
                node("t", "trigger_manual"),
                node("w", "wait_packet", target="INST", packet="ADCS", timeout=timeout, poll=0.05),
                node("l", "log", message="got {{ packet_count }}" if handle == "received" else "late"),
            ],
            [edge("t", "w"), edge("w", "l", handle)],
        )

    threading.Timer(0.3, lambda: backend.counts.__setitem__(("INST", "ADCS"), 6)).start()
    record = run_to_end(engine, flow(3, "received"))
    assert record["status"] == "success" and record["steps"][-1]["message"] == "got 6"
    record = run_to_end(engine, flow(0.2, "timeout"))
    assert record["steps"][-1]["message"] == "late"


def test_ask_operator_for_a_value(engine, backend):
    flow = make_flow(
        [
            node("t", "trigger_manual"),
            node("q", "ask", prompt="Rate for {{ 'pass' }}?", kind="number", variable="rate", min=1, max=10),
            node("c", "send_cmd", command="INST RATE with VALUE {{ rate }}"),
        ],
        [edge("t", "q"), edge("q", "c", "answered")],
        mode="crawl",
    )
    saved, _ = engine.save_flow(flow)
    run_id = engine.trigger(saved["id"], manual=True)["run"]["id"]
    assert wait_until(lambda: engine.status()["approvals"])
    question = engine.status()["approvals"][0]
    assert question["ask"]["kind"] == "number" and question["preview"] == "Rate for pass?"
    with pytest.raises(EngineError, match="above the max"):
        engine.approve(run_id, question["id"], "go", "alice", 50)
    engine.approve(run_id, question["id"], "go", "alice", "4")
    # crawl mode still asks before the command
    assert wait_until(lambda: [a for a in engine.status()["approvals"] if a["node"] == "c"])
    gate = engine.status()["approvals"][0]
    engine.approve(run_id, gate["id"], "go", "alice")
    assert wait_until(lambda: run_id not in engine.runs)
    record = engine.get_run(run_id)
    assert backend.commands == ["INST RATE with VALUE 4"]
    assert next(s for s in record["steps"] if s["node"] == "q")["message"] == "alice answered 4"


def test_ask_times_out_to_its_default(engine):
    flow = make_flow(
        [
            node("t", "trigger_manual"),
            node("q", "ask", prompt="Go?", kind="confirm", default="yes", timeout=0.2),
            node("l", "log", message="{{ answer }}"),
        ],
        [edge("t", "q"), edge("q", "l", "timeout")],
    )
    record = run_to_end(engine, flow)
    assert record["steps"][-1]["message"] == "True"


def test_item_command_and_list_parameters():
    params = [
        {"name": "sensor", "type": "item"},
        {"name": "setup", "type": "command"},
        {"name": "modes", "type": "list"},
    ]
    values, problems = fill_parameters(
        params, {"sensor": "inst health_status temp1", "setup": "INST CLEAR", "modes": "A, B,,C"}
    )
    assert problems == [] and values == {
        "sensor": "INST HEALTH_STATUS TEMP1",
        "setup": "INST CLEAR",
        "modes": ["A", "B", "C"],
    }
    _values, problems = fill_parameters(params, {"sensor": "TEMP1", "setup": "CLEAR", "modes": []})
    assert any("telemetry item" in p for p in problems) and any("not a command" in p for p in problems)


def test_item_parameter_in_a_condition_and_command_parameter_sent_raw(engine, backend):
    backend.values["INST HEALTH_STATUS TEMP1"] = 70
    flow = make_flow(
        [
            node("t", "trigger_manual"),
            node("i", "if", condition="tlm(sensor) > 50"),
            node("c", "send_cmd", command="{{ setup }}", allow_raw_values=True),
        ],
        [edge("t", "i"), edge("i", "c", "true")],
        template=True,
        parameters=[{"name": "sensor", "type": "item"}, {"name": "setup", "type": "command"}],
    )
    record = run_to_end(
        engine,
        flow,
        variables={"sensor": "INST HEALTH_STATUS TEMP1", "setup": "INST COLLECT with TYPE NORMAL, DURATION 1"},
    )
    assert record["status"] == "success" and backend.commands == ["INST COLLECT with TYPE NORMAL, DURATION 1"]


# ---- which events become COSMOS alerts (toasts) ----------------------------------


def test_approval_alert_waits_and_skips_answered_approvals(engine, alerts):
    alerts.settings["approval_escalate_after"] = 0.4
    flow, _ = engine.save_flow(
        chain(node("a", "log", message="a"), node("b", "log", message="b")) | {"mode": "crawl", "name": "Quick"}
    )
    run_id = engine.trigger(flow["id"], manual=True)["run"]["id"]
    assert wait_until(lambda: engine.status()["approvals"])
    engine.approve(run_id, engine.status()["approvals"][0]["id"], "go")  # answered at once
    assert wait_until(lambda: engine.status()["approvals"])  # the next step asks
    time.sleep(0.6)
    approvals = [m for _l, m, _u in alerts.sent_log if "waiting for approval" in m]
    assert len(approvals) == 1 and "(0.4s)" in approvals[0]  # only the one nobody answered in time
    engine.stop_all()


def test_only_unattended_failures_and_real_problems_toast(engine, backend, alerts):
    from flow_automator.triggers import TriggerManager

    backend.fail_commands.add("X")
    run_to_end(engine, chain(node("c", "send_cmd", command="INST X")) | {"name": "Mine"})
    flow, _ = engine.save_flow(
        make_flow(
            [
                node("t", "trigger_tlm", condition="INST HEALTH_STATUS TEMP1 > 50"),
                node("c", "send_cmd", command="INST X"),
            ],
            [edge("t", "c")],
            name="Watcher",
        )
    )
    engine.set_armed(flow["id"], True)
    manager = TriggerManager(engine)
    backend.values["INST HEALTH_STATUS TEMP1"] = 10
    manager.tick()
    backend.values["INST HEALTH_STATUS TEMP1"] = 60
    manager.tick()
    assert wait_until(lambda: any("'Watcher' failed" in m for _l, m, _u in alerts.sent_log))
    levels = {m.split(": ", 1)[1].split(":")[0]: level for level, m, _u in alerts.sent_log}
    assert levels["'Mine' failed"] == "notification"  # you pressed Run, you're watching
    assert levels["'Watcher' failed"] == "alert"  # nobody was
    engine.stop_all("alice")
    assert (
        "notification",
        "Flow Automator: STOP ALL by alice: Disarmed 1 flows, stopped 0 runs, paused the contact scheduler",
    ) in [(level, m) for level, m, _u in alerts.sent_log]


def test_approval_alert_delay_setting(engine, alerts):
    assert alerts.save_settings({"approval_escalate_after": 45})["approval_escalate_after"] == 45
    with pytest.raises(AlertError, match="between 0 and 3600"):
        alerts.save_settings({"approval_escalate_after": -1})
