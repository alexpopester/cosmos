"""Regression test on recorded real passes (docs/REAL-DATA-TESTING.md step 3c).

For each recorded pass in the openc3-cosmos-satnogs plugin (CANVAS, AEPEX,
CatSat), replays it through COSMOS three ways and checks what the step 3b
post-pass template concludes:

| Case | Replay | Expected |
|---|---|---|
| as recorded | the real frames | healthy |
| injected fault | one decoded field overridden | an anomaly alert naming it |
| lost pass | every frame dropped | not heard |

Each case: the plugin's REPLAY PLAY puts the pass "now" in its own REPLAY_<SAT>
target; a Flow Automator run of the template reading that target gets a
contact timed to match. Satellites run in parallel, cases one after another.

    python realdata/replay_test.py                 # 10x speed: about 6 minutes
    python realdata/replay_test.py --speed 1       # real time: about 40 minutes
    python realdata/replay_test.py --only CANVAS

Writes realdata/reports/replay_<time>.json. Exits 1 if any case fails.
"""

import argparse
import concurrent.futures
import json
import os
import sys
import time


HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import realtlm_setup  # noqa: E402
import soak_common as common  # noqa: E402
from soak_recorder import check_outcome  # noqa: E402


PREFIX = "Replay: "
LEAD_S = 20  # the replayed AOS is this far after the case starts
DELIVERY_S = 15  # frames arrive this long after the replayed LOS (an "upload")
# A fault to inject per satellite: (decoded field, value, what the alert must say)
FAULTS = {
    "CANVAS": ("bcn_cmd_rjct_cnt", 3, "rejected commands"),
    "AEPEX": ("sw_mode_system_mode", 3, "mode 3"),
    "CATSAT": ("p60_batt_v", 14000, "battery low 14000 mV"),
}
RECORDINGS = {  # in openc3-cosmos-satnogs/lib/recordings
    "CANVAS": ("CANVAS_15365914", 619),
    "AEPEX": ("AEPEX_15365033", 594),
    "CATSAT": ("CATSAT_15372617", 394),
}


def cases(name):
    field, value, words = FAULTS[name]
    return [
        {"case": "as recorded", "overrides": {}, "drop": 0, "expect": "healthy"},
        {
            "case": f"injected fault: {field} = {value}",
            "overrides": {field: value},
            "drop": 0,
            "expect": "anomaly",
            "alert_says": words,
        },
        {"case": "lost pass (all frames dropped)", "overrides": {}, "drop": 1, "expect": "not_heard"},
    ]


class Cosmos(common.FlowAutomator):
    def cmd(self, text):
        response = self.session.post(
            f"{self.url}/openc3-api/api",
            json={
                "jsonrpc": "2.0",
                "method": "cmd",
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
        return body["result"]


def ensure_template(fa, name, speed):
    """The step 3b template for this satellite, reading its REPLAY_ target, saved (replacing an older one)."""
    duration = RECORDINGS[name][1] / speed
    flow = realtlm_setup.template_flow(
        name,
        target=f"REPLAY_{name}",
        wait_s=round(DELIVERY_S + 60 + duration * 0.2),  # the upload, plus slack
        early_s=5,  # replays are exact; a beacon left from the previous case mustn't count
        poll_s=2,
        prefix=PREFIX,
    )
    flow["template"] = False  # run directly, not scheduled
    existing = fa.get("/flows")
    existing = existing.get("flows", existing) if isinstance(existing, dict) else existing
    for old in existing:
        if old["name"] == flow["name"]:
            fa.delete(f"/flows/{old['id']}")
    saved = fa.post("/flows", flow)
    return saved.get("flow", saved)["id"]


def run_case(fa, name, flow_id, case, speed):
    recording, recorded_duration = RECORDINGS[name]
    duration = recorded_duration / speed
    aos = time.time() + LEAD_S
    fa.cmd(
        f"REPLAY PLAY with RECORDING '{recording}', AS_TARGET 'REPLAY_{name}', AOS_TIME {aos:.3f}, "
        f"SPEED {speed:g}, DELIVERY {DELIVERY_S}, OVERRIDES '{json.dumps(case['overrides'])}', DROP {case['drop']}"
    )
    started = fa.post(
        f"/flows/{flow_id}/run",
        {
            "contact": {
                "aos_in": aos - time.time(),
                "duration": duration,
                "satellite": f"REPLAY {name}",
                "station": recording,
            }
        },
    )
    run_id = (started.get("run") or {}).get("id")
    if not run_id:
        return {**case, "satellite": name, "result": "FAIL", "got": f"run didn't start: {started}"}
    deadline = time.time() + LEAD_S + duration + DELIVERY_S + 600
    run = {}
    while time.time() < deadline:
        run = fa.get(f"/runs/{run_id}")
        run = run.get("run", run)
        if run.get("status") not in ("running", "queued", None):
            break
        time.sleep(3)
    outcome, summary = check_outcome(run)
    alert = next((s.get("message") or "" for s in run.get("steps") or [] if s.get("node") == "bad"), "")
    ok = outcome == case["expect"] and (not case.get("alert_says") or case["alert_says"] in alert)
    return {
        "satellite": name,
        "case": case["case"],
        "expect": case["expect"],
        "got": outcome,
        "result": "PASS" if ok else "FAIL",
        "summary": summary,
        "alert": alert or None,
        "run_id": run_id,
    }


def run_satellite(fa, name, speed):
    flow_id = ensure_template(fa, name, speed)
    results = []
    for case in cases(name):
        result = run_case(fa, name, flow_id, case, speed)
        print(
            f"{result['result']}  {name:7} {result['case']:42} expected {result['expect']:9} got {result['got']}",
            flush=True,
        )
        results.append(result)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--speed", type=float, default=10.0)
    parser.add_argument("--only", choices=list(RECORDINGS))
    parser.add_argument("--url", default=os.environ.get("COSMOS_URL", "http://localhost:2900"))
    args = parser.parse_args(argv)
    fa = Cosmos(args.url, password=os.environ.get("COSMOS_PASSWORD"))
    names = [args.only] if args.only else list(RECORDINGS)
    started = time.time()
    with concurrent.futures.ThreadPoolExecutor(len(names)) as pool:
        results = [r for batch in pool.map(lambda n: run_satellite(fa, n, args.speed), names) for r in batch]
    failed = [r for r in results if r["result"] != "PASS"]
    print(
        f"\n{len(results) - len(failed)}/{len(results)} passed in {(time.time() - started) / 60:.1f} min "
        f"(speed {args.speed:g}x)"
    )
    report_dir = os.path.join(HERE, "reports")
    os.makedirs(report_dir, exist_ok=True)
    path = os.path.join(report_dir, f"replay_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json")
    common.save_json(path, {"speed": args.speed, "results": results})
    print(f"Report: {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
