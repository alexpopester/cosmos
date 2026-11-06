"""Fault scenarios for commanding real flight software in contacts (docs/REAL-DATA-TESTING.md step 4).

Stages passes on demand: the contact gate's "window" mode opens the link from AOS to LOS,
and a Flow Automator run of the pass ops template (cfs_setup.py) gets a contact timed to
match. Then it breaks things and checks what the automation concludes:

| Scenario | Fault | Expected |
|---|---|---|
| nominal | none | nominal, link closed at LOS |
| dead pass | the link never opens | failed (alert: no telemetry) |
| early LOS | the link closes as soon as housekeeping is acquired | failed (alert) |
| reboot before the pass | ES_RESTART before AOS: telemetry is off | nominal (it switches telemetry on) |
| reboot mid-pass | ES_RESTART once the ES no-op is acknowledged | recovered on the second attempt |
| lossy link | 10% of packets lost both ways | nominal or recovered, no alert |
| link after LOS | the link stays up 60 s past LOS | the run reports telemetry after LOS |
| wait for packet after a reboot | a plain Wait for packet flow, right after ES_RESTART | it reads the rebooted counter (0), not the old one |

Waits for real passes (the gate's next pass) to be over before staging one, so it never
commands cFS in the middle of a real contact.

    python realdata/cfs_faults.py                 # all, about 15 minutes
    python realdata/cfs_faults.py --only reboot_mid_pass
"""

import argparse
import json
import os
import sys
import time

import requests


HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import cfs_setup  # noqa: E402
import soak_common as common  # noqa: E402
from replay_test import Cosmos  # noqa: E402


PREFIX = "CFS staged: "
GATE = os.environ.get("CFS_GATE_URL", "http://127.0.0.1:8089")
LEAD_S = 15
PASS_S = 60

SCENARIOS = [
    {"name": "nominal", "expect": {("nominal", "ok")}},
    {"name": "dead_pass", "gate": "closed", "expect": {("failed", "ok")}},
    {"name": "early_los", "close_after_node": "counters", "expect": {("failed", "ok")}},
    {"name": "reboot_before_pass", "reboot_before": True, "expect": {("nominal", "ok")}},
    {"name": "reboot_mid_pass", "reboot_after_node": "stage2", "expect": {("recovered", "ok")}},
    {"name": "lossy_link", "drop": 0.1, "expect": {("nominal", "ok"), ("recovered", "ok")}},
    {"name": "link_after_los", "window_s": PASS_S + 60, "expect": {("nominal", "late")}},
    # Not the pass ops template: a plain "wait for a packet, read it" flow, the engine fix in 1.15.0
    {"name": "wait_packet_after_reboot", "kind": "wait_packet", "expect": {("fresh", None)}},
]


def gate(path, body=None):
    if body is None:
        return requests.get(f"{GATE}{path}", timeout=10).json()
    return requests.post(f"{GATE}{path}", data=json.dumps(body), timeout=10).json()


def clear_of_real_passes(needed_s):
    """Wait until no real pass starts within the time a scenario needs (or is in progress)."""
    while True:
        status = gate("/status")
        upcoming = status.get("next_pass")
        now = time.time()
        if not upcoming or upcoming["aos"] - now > needed_s + 120:
            return
        wait = max(30.0, upcoming["los"] - now + 60)
        print(
            f"  a real pass is due (AOS {time.strftime('%H:%M:%S', time.gmtime(upcoming['aos']))} UTC): "
            f"waiting {wait / 60:.0f} min",
            flush=True,
        )
        time.sleep(wait)


def ensure_template(fa):
    flow = cfs_setup.template_flow(prefix=PREFIX)
    flow["template"] = False
    existing = fa.get("/flows")
    for old in existing.get("flows", existing) if isinstance(existing, dict) else existing:
        if old["name"] == flow["name"]:
            fa.delete(f"/flows/{old['id']}")
    saved = fa.post("/flows", flow)
    return saved.get("flow", saved)["id"]


def wait_packet_flow():
    """Telemetry on, Wait for packet, read the command counter: what any flow might do after a reboot."""
    nodes = [
        {"id": "t", "type": "trigger_manual", "position": {"x": 0, "y": 0}, "data": {}},
        {
            "id": "on",
            "type": "send_cmd",
            "position": {"x": 200, "y": 0},
            "data": {"command": "CFS TO_LAB_ENABLE with DEST_IP '127.0.0.1'"},
        },
        {
            "id": "wait",
            "type": "wait_packet",
            "position": {"x": 400, "y": 0},
            "data": {"target": "CFS", "packet": "ES_HK", "count": 1, "timeout": 30},
        },
        {
            "id": "read",
            "type": "set_var",
            "position": {"x": 600, "y": 0},
            "data": {"name": "es0", "expression": "tlm('CFS ES_HK CMD_CNT')"},
        },
    ]
    edges = [
        {"id": f"{a}-{h}-{b}", "source": a, "sourceHandle": h, "target": b}
        for a, h, b in (("t", "out", "on"), ("on", "out", "wait"), ("wait", "received", "read"))
    ]
    return {"name": f"{PREFIX}wait for packet", "mode": "run", "nodes": nodes, "edges": edges}


def run_wait_packet_scenario(fa, scenario):
    clear_of_real_passes(120)
    gate("/fault", {"mode": "open", "seconds": 90})
    fa.cmd_raw("cmd", "CFS TO_LAB_ENABLE with DEST_IP '127.0.0.1'")
    fa.cmd_raw("cmd", "CFS ES_NOOP")  # the counter is above 0 before the reboot
    time.sleep(3)
    fa.cmd_raw("cmd_no_hazardous_check", "CFS ES_RESTART with RESTART_TYPE PROCESSOR")
    time.sleep(8)
    existing = fa.get("/flows")
    for old in existing.get("flows", existing) if isinstance(existing, dict) else existing:
        if old["name"] == wait_packet_flow()["name"]:
            fa.delete(f"/flows/{old['id']}")
    saved = fa.post("/flows", wait_packet_flow())
    started = fa.post(f"/flows/{saved.get('flow', saved)['id']}/run", {})
    run_id = started["run"]["id"]
    run = {}
    for _ in range(200):
        run = fa.get(f"/runs/{run_id}")
        run = run.get("run", run)
        if run.get("status") not in ("running", "queued", None):
            break
        time.sleep(0.3)
    gate("/fault", {"mode": "auto"})
    read = next((s.get("message") or "" for s in run.get("steps") or [] if s.get("node") == "read"), "")
    got = ("fresh", None) if read == "es0 = 0" else (read or run.get("status"), None)
    passed = got in scenario["expect"]
    print(
        f"{'PASS' if passed else 'FAIL'}  {scenario['name']:20} expected es0 = 0 after the reboot, got {read!r}",
        flush=True,
    )
    return {
        "scenario": scenario["name"],
        "expect": ["es0 = 0"],
        "got": read,
        "result": "PASS" if passed else "FAIL",
        "run_id": run_id,
    }


def run_scenario(fa, flow_id, scenario):
    clear_of_real_passes(LEAD_S + PASS_S + 120)
    if scenario.get("reboot_before"):
        gate("/fault", {"mode": "open", "seconds": 10})
        fa.cmd_raw("cmd_no_hazardous_check", "CFS ES_RESTART with RESTART_TYPE PROCESSOR")
        time.sleep(8)  # it restarts (the container comes back) with telemetry off
    aos = time.time() + LEAD_S
    los = aos + PASS_S
    if scenario.get("gate") == "closed":
        gate("/fault", {"mode": "closed", "seconds": LEAD_S + PASS_S + 60})
    else:
        window_end = aos + scenario.get("window_s", PASS_S)
        gate("/fault", {"mode": "window", "aos": aos, "los": window_end, "drop": scenario.get("drop", 0)})
    started = fa.post(
        f"/flows/{flow_id}/run",
        {
            "contact": {
                "aos_in": aos - time.time(),
                "duration": PASS_S,
                "satellite": "CFS staged",
                "station": scenario["name"],
            }
        },
    )
    run_id = (started.get("run") or {}).get("id")
    rebooted = False
    deadline = los + 300
    run = {}
    while time.time() < deadline:
        run = fa.get(f"/runs/{run_id}")
        run = run.get("run", run)
        reached = {s.get("node") for s in run.get("steps") or []}
        if scenario.get("reboot_after_node") in reached and not rebooted:
            fa.cmd_raw("cmd_no_hazardous_check", "CFS ES_RESTART with RESTART_TYPE PROCESSOR")
            rebooted = True
        if scenario.get("close_after_node") in reached and not rebooted:
            gate("/fault", {"mode": "closed", "seconds": max(5.0, los - time.time() + 30)})  # cFS is quick: cut it here
            rebooted = True
        if run.get("status") not in ("running", "queued", None):
            break
        time.sleep(0.3)
    gate("/fault", {"mode": "auto"})
    got = cfs_setup.outcome(run)
    passed = got in scenario["expect"]
    print(
        f"{'PASS' if passed else 'FAIL'}  {scenario['name']:20} expected {sorted(scenario['expect'])} got {got}",
        flush=True,
    )
    log = next(
        (s.get("message") for s in run.get("steps") or [] if s.get("node") in ("done", "recovered", "failed")), ""
    )
    return {
        "scenario": scenario["name"],
        "expect": sorted(scenario["expect"]),
        "got": got,
        "result": "PASS" if passed else "FAIL",
        "message": log,
        "run_id": run_id,
    }


class CfsCosmos(Cosmos):
    def cmd_raw(self, method, text):
        response = self.session.post(
            f"{self.url}/openc3-api/api",
            json={
                "jsonrpc": "2.0",
                "method": method,
                "params": [text],
                "keyword_params": {"scope": self.scope},
                "id": 1,
            },
            headers={"Authorization": self.token, "Content-Type": "application/json-rpc"},
            timeout=60,
        )
        body = response.json()
        if "error" in body:
            raise RuntimeError(f"{text}: {body['error'].get('message')}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", choices=[s["name"] for s in SCENARIOS])
    parser.add_argument("--url", default=os.environ.get("COSMOS_URL", "http://localhost:2900"))
    args = parser.parse_args(argv)
    fa = CfsCosmos(args.url, password=os.environ.get("COSMOS_PASSWORD"))
    flow_id = ensure_template(fa)
    started = time.time()
    results = [
        run_wait_packet_scenario(fa, s) if s.get("kind") == "wait_packet" else run_scenario(fa, flow_id, s)
        for s in SCENARIOS
        if not args.only or s["name"] == args.only
    ]
    failed = [r for r in results if r["result"] != "PASS"]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed in {(time.time() - started) / 60:.1f} min")
    path = os.path.join(HERE, "reports", f"cfs_faults_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    common.save_json(path, {"results": results})
    print(f"Report: {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
