"""realdata/realtlm_setup.py: post-pass checks on real satellite telemetry (step 3b), run in memory."""

import copy
import os
import sys

import pytest
from conftest import run_to_end
from test_contacts import EPOCH


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "realdata"))

import realtlm_setup  # noqa: E402
from flow_automator.graph import validate_flow  # noqa: E402


# Real beacon values from November 2026 (healthy)
HEALTHY = {
    "CANVAS APID_32": {
        "BCN_CMD_RECV_CNT": 94,
        "BCN_CMD_SUCC_CNT": 94,
        "BCN_CMD_RJCT_CNT": 0,
        "BCN_CMD_FAIL_CODE": 0,
        "BCN_FP_ERR_CNT": 0,
        "BCN_ADCS_ALIVE": 2,
        "BCN_ADCS_ECLIPSE": 0,
        "BCN_FSW_MAJOR_VERSION": 2,
        "BCN_EPS_BATT1_TEMP": 2124,
        "BCN_EPS_BATT2_TEMP": 2115,
    },
    "AEPEX APID_1": {
        "SW_CMD_RECV_COUNT": 29,
        "SW_CMD_SUCC_COUNT": 29,
        "SW_CMD_RJCT_COUNT": 0,
        "SW_CMD_FAIL_CODE": 0,
        "SW_MODE_SYSTEM_MODE": 1,
        "SW_BAT_ALIVE_STATE_BATTERY0": 1,
        "SW_UHF_ALIVE": 1,
        "SW_TIME_SINCE_RX": 27261,
    },
    "CATSAT TYPE_4": {
        "P60_BATT_V": 16320,
        "P60_BATT_CHRG": 907,
        "P60_BATT_DCHRG": 573,
        "P60_TEMP_0": 357,
        "P60_UPTIME": 9905,
        "P60_LATCHUP_ACU_X1_VCC": 0,
        "P60_LATCHUP_PDU_X2_VCC": 0,
        "P60_LATCHUP_PDU_X3_VCC": 0,
    },
}
AOS = EPOCH - 900
LOS = EPOCH - 300
CONTACT = {
    "contact_aos": AOS,
    "contact_tca": AOS + 300,
    "contact_los": LOS,
    "contact_satellite": "Soak: X",
    "contact_station": "Soak: EU1AEM #2029",
}


def quick(flow):
    """The template with its 90-minute wait cut to a moment."""
    flow = copy.deepcopy(flow)
    for node in flow["nodes"]:
        if node["type"] == "wait_tlm":
            node["data"].update(timeout=0.3, poll=0.05)
    flow["template"] = False
    return flow


def beacon(backend, name, frame_time, **changes):
    target_packet = realtlm_setup.MISSIONS[name]["beacon"]
    values = {**HEALTHY[target_packet], **changes, "FRAME_TIME": frame_time, "STATION": "EU1AEM"}
    for item, value in values.items():
        backend.values[f"{target_packet} {item}"] = value


@pytest.fixture
def alerts(engine):
    sent = []
    engine.alerts.emit = lambda event, title, message="", **kwargs: sent.append((event, title, message, kwargs))
    return sent


def messages(run):
    return " | ".join(step.get("message") or "" for step in run["steps"])


@pytest.mark.parametrize("name", list(realtlm_setup.MISSIONS))
def test_templates_are_valid_and_send_nothing(name):
    flow = realtlm_setup.template_flow(name)
    assert validate_flow(flow) == []
    assert not [n for n in flow["nodes"] if n["type"] in ("send_cmd", "run_script", "set_tlm", "ask")]


@pytest.mark.parametrize("name", list(realtlm_setup.MISSIONS))
def test_a_healthy_beacon_from_this_pass(engine, backend, alerts, name):
    beacon(backend, name, AOS + 120)
    run = run_to_end(engine, quick(realtlm_setup.template_flow(name)), variables=dict(CONTACT))
    assert run["status"] == "success", messages(run)
    assert f"{name} heard by EU1AEM" in messages(run) and f"{name} healthy after the pass" in messages(run)
    assert alerts == []


@pytest.mark.parametrize(
    ("name", "changes", "expected"),
    [
        ("CANVAS", {"BCN_CMD_RJCT_CNT": 2}, "rejected commands"),
        ("CANVAS", {"BCN_EPS_BATT2_TEMP": 2400}, "battery temperature"),
        ("AEPEX", {"SW_MODE_SYSTEM_MODE": 3}, "mode 3"),
        ("AEPEX", {"SW_TIME_SINCE_RX": 200000}, "no uplink for 2 days"),
        ("CATSAT", {"P60_BATT_V": 14000}, "battery low 14000 mV"),
        ("CATSAT", {"P60_LATCHUP_PDU_X2_VCC": 1}, "latch-up"),
    ],
)
def test_an_anomaly_alerts_and_says_what(engine, backend, alerts, name, changes, expected):
    beacon(backend, name, AOS + 120, **changes)
    run = run_to_end(engine, quick(realtlm_setup.template_flow(name)), variables=dict(CONTACT))
    assert run["status"] == "success", messages(run)
    notified = [a for a in alerts if a[1] == f"{name} anomaly"]
    assert notified and expected in notified[0][2], alerts


def test_a_beacon_from_an_earlier_pass_means_not_heard(engine, backend, alerts):
    beacon(backend, "CATSAT", AOS - 6000)  # the last beacon is from the orbit before
    run = run_to_end(engine, quick(realtlm_setup.template_flow("CATSAT")), variables=dict(CONTACT))
    assert run["status"] == "success", messages(run)
    assert "No CATSAT beacon heard" in messages(run)
    assert not [a for a in alerts if "anomaly" in a[1]]


def test_rules_are_accepted_by_the_planner(engine):
    from flow_automator.orbit import make_tle  # noqa: PLC0415

    planner = engine.planner
    sat = planner.save_satellite({"name": "CANVAS", "tle": make_tle(EPOCH, 97.5, 60.0, 10.0, catalog=68635)})
    station = planner.save_station({"name": "EU1AEM", "latitude": 53.9, "longitude": 27.1, "min_elevation": 0})
    flow, problems = engine.save_flow(realtlm_setup.template_flow("CANVAS"))
    assert problems == []
    result = planner.save_rule(realtlm_setup.rule_payload("CANVAS", flow["id"], [sat["id"]], [station["id"]]))
    assert result["rule"]["anchor"] == "los" and result["rule"]["stop_at_los"] is False
