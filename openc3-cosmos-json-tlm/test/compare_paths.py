# Live end-to-end benchmark of every JSON ingest path, against a running COSMOS.
# Run it with test/compare_paths.sh, which copies it into the operator container
# and passes the credentials through.
#
# Paths (each sends the same item values):
#   inject_tlm -> binary        COSMOS inject_tlm into JSONDEMO STATUS / WAVE
#   inject_tlm -> JSON packet   COSMOS inject_tlm into JSONDEMO STATUSJ / WAVEJ (ACCESSOR JsonAccessor)
#   JSON interface -> binary    this plugin, POST /tlm/JSONDEMO/STATUS
#   JSON interface -> JSON pkt  this plugin, POST /tlm/JSONDEMO/STATUSJ
#   built-in HTTP server        COSMOS HttpServerInterface, POST /status -> JSONHTTP STATUS (JSON accessor)
#
# The rate that matters is end-to-end: packets per second until the LAST packet
# sent had been decommutated into the current value table. After each run one
# marker packet (a unique COUNTER / TIMESTAMP) is sent; every path publishes in
# order, so once decom shows the marker, everything before it has been decoded.
#
# Environment:
#   OPENC3_API_PASSWORD  COSMOS password (inject_tlm, and reading the CVT; required)
#   JSON_TLM_TOKEN       the plugin's API token (JSON interface lanes skipped if unset)
#   JSON_TLM_URL         default http://127.0.0.1:7780/json-tlm
#   JSON_HTTP_URL        default http://127.0.0.1:7781 (the built-in HttpServerInterface)

import argparse
import contextlib
import json
import multiprocessing
import os
import sys
import time
import urllib.error
import urllib.request

STATUS = {"COUNTER": 5, "TEMP": 12000, "VOLTAGE": 3300, "MODE": 1, "LABEL": "rest", "WHEELS": [10, 20, 30, 40]}
WAVE = {"TIMESTAMP": 1, "SAMPLES": [i % 65536 for i in range(2048)]}

# (label, items, marker item, batch)
SCENARIOS = [
    ("small (STATUS)", STATUS, "COUNTER", 1),
    ("4 KB array (WAVE)", WAVE, "TIMESTAMP", 1),
    ("small, batches of 100", STATUS, "COUNTER", 100),
]

# lane: (method, {scenario kind: (target, packet)})
LANES = {
    "inject_tlm -> binary": ("inject", {"STATUS": ("JSONDEMO", "STATUS"), "WAVE": ("JSONDEMO", "WAVE")}),
    "inject_tlm -> JSON packet": ("inject", {"STATUS": ("JSONDEMO", "STATUSJ"), "WAVE": ("JSONDEMO", "WAVEJ")}),
    "JSON interface -> binary": ("json", {"STATUS": ("JSONDEMO", "STATUS"), "WAVE": ("JSONDEMO", "WAVE")}),
    "JSON interface -> JSON packet": ("json", {"STATUS": ("JSONDEMO", "STATUSJ"), "WAVE": ("JSONDEMO", "WAVEJ")}),
    "built-in HTTP server -> JSON packet": ("builtin", {"STATUS": ("JSONHTTP", "STATUS"), "WAVE": ("JSONHTTP", "WAVE")}),
}


def json_url():
    return os.environ.get("JSON_TLM_URL", "http://127.0.0.1:7780/json-tlm").rstrip("/")


def http_url():
    return os.environ.get("JSON_HTTP_URL", "http://127.0.0.1:7781").rstrip("/")


class QueueFull(Exception):
    """The JSON interface's 503 back-pressure: the sender backs off and retries."""


def post(url, body, headers):
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            response.read()
    except urllib.error.HTTPError as error:
        if error.code == 503:
            raise QueueFull() from error
        raise RuntimeError(f"HTTP {error.code}: {error.read().decode(errors='replace')[:200]}") from error


def make_sender(method, target, packet, items, batch):
    """A no-argument function that sends one request (of `batch` packets)."""
    if method == "inject":
        if batch != 1:
            return None  # no batch form
        from openc3.script import inject_tlm

        return lambda: inject_tlm(target, packet, items, type="RAW")
    if method == "builtin":
        if batch != 1:
            return None  # one packet per request
        body = json.dumps(items).encode()
        url = f"{http_url()}/{packet.lower()}"
        return lambda: post(url, body, {"Content-Type": "application/json"})
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {os.environ.get('JSON_TLM_TOKEN', '')}"}
    if batch == 1:
        url, body = f"{json_url()}/tlm/{target}/{packet}", json.dumps({"items": items}).encode()
    else:
        entry = {"target": target, "packet": packet, "items": items}
        url, body = f"{json_url()}/tlm", json.dumps({"packets": [entry] * batch}).encode()
    return lambda: post(url, body, headers)


def client(method, target, packet, items, batch, duration, results):
    sys.stdout = open(os.devnull, "w")  # noqa: SIM115 - the script API echoes every call
    send = make_sender(method, target, packet, items, batch)
    latencies, errors, rejected = [], [], 0
    stop_at = time.perf_counter() + duration
    while time.perf_counter() < stop_at:
        start = time.perf_counter()
        try:
            send()
            latencies.append(time.perf_counter() - start)
        except QueueFull:
            rejected += 1
            time.sleep(0.05)
        except Exception as error:
            errors.append(str(error))
    results.put((latencies, errors, rejected))


def cvt(target, packet, item):
    from openc3.script import tlm

    with contextlib.redirect_stdout(open(os.devnull, "w")):  # noqa: SIM115
        return tlm(f"{target} {packet} {item}", type="RAW")


def wait_for_marker(target, packet, marker_item, marker, timeout):
    start = time.perf_counter()
    while time.perf_counter() - start < timeout:
        if cvt(target, packet, marker_item) == marker:
            return time.perf_counter()
        time.sleep(0.02)
    return None


def measure(method, target, packet, items, marker_item, batch, clients, duration, run_id):
    results = multiprocessing.Queue()
    procs = [
        multiprocessing.Process(target=client, args=(method, target, packet, items, batch, duration, results))
        for _ in range(clients)
    ]
    start = time.perf_counter()
    for proc in procs:
        proc.start()
    latencies, errors, rejected = [], [], 0
    for _ in procs:
        got = results.get()
        latencies += got[0]
        errors += got[1]
        rejected += got[2]
    for proc in procs:
        proc.join()
    sending = time.perf_counter() - start

    # One marker packet behind everything else; when decom shows it, all are in
    marker_items = {**items, marker_item: run_id}
    send_marker = make_sender(method, target, packet, marker_items, 1)
    while True:
        try:
            with contextlib.redirect_stdout(open(os.devnull, "w")):  # noqa: SIM115
                send_marker()
            break
        except QueueFull:
            time.sleep(0.1)
    done = wait_for_marker(target, packet, marker_item, run_id, timeout=300)
    latencies.sort()
    packets = len(latencies) * batch
    return {
        "accepted": packets / sending,
        "end_to_end": (packets / (done - start)) if done else None,
        "lag": (done - start - sending) if done else None,
        "p50": latencies[len(latencies) // 2] * 1e3 if latencies else 0.0,
        "errors": errors,
        "rejected": rejected,
        "packets": packets,
    }


def check(method):
    if method == "json" and not os.environ.get("JSON_TLM_TOKEN"):
        return "skipped: set JSON_TLM_TOKEN"
    try:
        with contextlib.redirect_stdout(open(os.devnull, "w")):  # noqa: SIM115
            target, packet = ("JSONHTTP", "STATUS") if method == "builtin" else ("JSONDEMO", "STATUS")
            make_sender(method, target, packet, STATUS, 1)()
    except Exception as error:
        return f"failed: {str(error)[:150]}"
    return None


def try_once(method, target, packet, items):
    """Send one packet; the error message, or None."""
    try:
        with contextlib.redirect_stdout(open(os.devnull, "w")):  # noqa: SIM115
            make_sender(method, target, packet, items, 1)()
    except Exception as error:
        return str(error)
    return None


def main():
    parser = argparse.ArgumentParser(description="End-to-end rate of each JSON ingest path, live")
    parser.add_argument("--duration", type=float, default=5.0, help="seconds of sending per run (default 5)")
    parser.add_argument("--clients", type=int, nargs="+", default=[1, 4], help="client counts (default 1 4)")
    parser.add_argument("--only", help="only lanes or scenarios whose label contains this")
    args = parser.parse_args()
    if not os.environ.get("OPENC3_API_PASSWORD"):
        sys.exit("Set OPENC3_API_PASSWORD: the CVT is read through the script API")

    problems = {method: check(method) for method in ("inject", "json", "builtin")}
    print(f"End-to-end ingest paths, {args.duration:g}s of sending per run, rates in packets/s")
    for method, problem in problems.items():
        if problem:
            print(f"  {method}: {problem}")
    print()
    print(f"{'Path':36} {'Packets':22} {'Clients':>7} {'sent/s':>8} {'p50 ms':>7} {'to CVT/s':>9} {'lag s':>6} {'503s':>5}")

    run_id = int(time.time()) % 1_000_000 * 100
    notes = []
    for label, items, marker_item, batch in SCENARIOS:
        kind = "WAVE" if marker_item == "TIMESTAMP" else "STATUS"
        for lane, (method, packets) in LANES.items():
            if args.only and args.only.lower() not in (lane + label).lower():
                continue
            if problems[method] or make_sender(method, "X", "Y", items, batch) is None:
                continue
            target, packet = packets[kind]
            problem = try_once(method, target, packet, items)
            if problem and method == "inject" and packet.endswith("J") and not problems["json"]:
                # inject_tlm writes into the interface's live copy of the packet, whose buffer
                # starts zero-filled, which the JSON accessor can't parse. One real packet fixes it.
                notes.append(f"{lane}, {label}: first inject_tlm failed ({problem[:80]}); primed with one JSON interface packet")
                try_once("json", target, packet, items)
                time.sleep(1)
                problem = try_once(method, target, packet, items)
            if problem:
                print(f"{lane:36} {label:22} FAILED: {problem[:100]}", flush=True)
                continue
            for clients in args.clients:
                run_id += 1
                r = measure(method, target, packet, items, marker_item, batch, clients, args.duration, run_id)
                e2e = f"{r['end_to_end']:9.0f}" if r["end_to_end"] else f"{'timeout':>9}"
                lag = f"{r['lag']:6.1f}" if r["lag"] is not None else f"{'—':>6}"
                print(
                    f"{lane:36} {label:22} {clients:7} {r['accepted']:8.0f} {r['p50']:7.1f} {e2e} {lag} "
                    f"{r['rejected']:5}",
                    flush=True,
                )
                if r["errors"]:
                    notes.append(f"{lane}, {label}, {clients} clients: {len(r['errors'])} errors, e.g. {r['errors'][0]}")
    print()
    print("sent/s = requests the sender completed; to CVT/s = packets/s until the last one was in the current")
    print("value table; lag = seconds after sending stopped until decom caught up; 503s = JSON interface back-pressure.")
    if notes:
        print("\nNotes and errors:")
        for note in notes:
            print(f"  {note[:300]}")


if __name__ == "__main__":
    main()
