# Live side-by-side benchmark: COSMOS's built-in inject_tlm vs this plugin's JSON
# interface, against a running COSMOS. Run it with test/compare_live.sh, which
# copies it into the operator container (where the interface listens and the
# COSMOS Python library lives) and passes the credentials through.
#
# Both methods send the same packets from the same kind of client (one process
# per client, each looping for the run duration). inject_tlm returns once the
# interface has published the packet; the JSON interface returns once it has
# validated and queued it. So the JSON side reports two rates:
#   accepted   packets the HTTP side took in per second
#   sustained  packets per second until the interface had published all of them,
#              i.e. the queue was empty again. This is the fair comparison.
# Every run starts with the JSON queue empty, since the same interface process
# also services inject_tlm. Neither method waits for decom.
#
# Environment:
#   OPENC3_API_PASSWORD  COSMOS password, for inject_tlm (skipped if unset)
#   JSON_TLM_TOKEN       the plugin's API token, for the JSON interface (skipped if unset)
#   JSON_TLM_URL         default http://127.0.0.1:7780/json-tlm (the interface, from inside the operator)

import argparse
import contextlib
import json
import multiprocessing
import os
import sys
import time
import urllib.error
import urllib.request

TARGET = "JSONDEMO"
STATUS = {"COUNTER": 5, "TEMP": 12000, "VOLTAGE": 3300, "MODE": 1, "LABEL": "rest", "WHEELS": [10, 20, 30, 40]}
WAVE = {"TIMESTAMP": 1, "SAMPLES": [i % 65536 for i in range(2048)]}

VARIABLE_SMALL = {"NAME": "hello", "SAMPLES": [1, -2, 3], "BLOB": [1, 2, 3, 4, 5], "VINT": 1000, "TRAILER": 7}
VARIABLE_LARGE = {
    "NAME": "x" * 200,
    "SAMPLES": [i % 30000 for i in range(2048)],
    "BLOB": [i % 256 for i in range(4096)],
    "VINT": 70000,
    "TRAILER": 7,
}
TAIL = {"SEQ": 1, "MESSAGE": "m" * 1000}
TAILARRAY = {"DATA": [i % 65536 for i in range(2048)], "CRC": 9}

# (label, packet, items, batch size)
SCENARIOS = [
    ("STATUS (26 B)", "STATUS", STATUS, 1),
    ("WAVE (4 KB)", "WAVE", WAVE, 1),
    ("STATUS, batches of 100", "STATUS", STATUS, 100),
    # Dynamic: "rest of the packet" items
    ("TAIL (1 KB string)", "TAIL", TAIL, 1),
    ("TAILARRAY (2048 x u16)", "TAILARRAY", TAILARRAY, 1),
    # VARIABLE_BIT_SIZE items: string, 16 bit array, block, QUIC integer
    ("VARIABLE small", "VARIABLE", VARIABLE_SMALL, 1),
    ("VARIABLE large (8 KB)", "VARIABLE", VARIABLE_LARGE, 1),
]

# Sent once per method and read back from COSMOS to check the values survive
CHECKS = [
    ("TAIL (1 KB string)", "TAIL", TAIL),
    ("TAILARRAY (2048 x u16)", "TAILARRAY", TAILARRAY),
    ("VARIABLE: string + block", "VARIABLE", {"NAME": "hello", "BLOB": [1, 2, 3]}),
    ("VARIABLE: 16 bit array", "VARIABLE", {"SAMPLES": [1, -2, 3]}),
    ("VARIABLE: non-ASCII string", "VARIABLE", {"NAME": "héllo"}),
    ("VARIABLE: QUIC integer", "VARIABLE", {"VINT": 70000, "TRAILER": 7}),
    ("VARIABLE: everything", "VARIABLE", VARIABLE_SMALL),
]


def json_url():
    return os.environ.get("JSON_TLM_URL", "http://127.0.0.1:7780/json-tlm").rstrip("/")


class QueueFull(Exception):
    """The JSON interface's 503 back-pressure: not an error, the sender should retry."""


def queue_depth():
    try:
        with urllib.request.urlopen(f"{json_url()}/health", timeout=5) as response:
            return json.loads(response.read())["queue"]
    except Exception:
        return None


def wait_for_empty_queue(timeout=300):
    """Wait until the JSON interface has published everything it accepted. Returns seconds waited."""
    start = time.perf_counter()
    while time.perf_counter() - start < timeout:
        depth = queue_depth()
        if not depth:  # 0, or no JSON interface to wait on
            break
        time.sleep(0.1)
    return time.perf_counter() - start


# ------------------------------------------------------------------ senders


def make_inject_sender(packet_name, items, batch):
    if batch != 1:
        return None  # inject_tlm has no batch form
    from openc3.script import inject_tlm

    # inject_tlm's JSON-RPC carries real bytes for BLOCK items
    items = {name: bytes(value) if name == "BLOB" else value for name, value in items.items()}

    def send():
        inject_tlm(TARGET, packet_name, items, type="RAW")

    return send


def make_json_sender(packet_name, items, batch):
    if batch == 1:
        url = f"{json_url()}/tlm/{TARGET}/{packet_name}"
        body = json.dumps({"items": items}).encode()
    else:
        url = f"{json_url()}/tlm"
        entry = {"target": TARGET, "packet": packet_name, "items": items}
        body = json.dumps({"packets": [entry] * batch}).encode()
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {os.environ['JSON_TLM_TOKEN']}"}

    def send():
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                response.read()
        except urllib.error.HTTPError as error:
            if error.code == 503:
                raise QueueFull() from error
            raise RuntimeError(f"HTTP {error.code}: {error.read().decode(errors='replace')[:200]}") from error

    return send


SENDERS = {"inject_tlm": make_inject_sender, "json": make_json_sender}


def client(method, scenario_index, duration, results):
    # The COSMOS script API echoes every call to stdout
    sys.stdout = open(os.devnull, "w")  # noqa: SIM115 - lives as long as this process
    _, packet_name, items, batch = SCENARIOS[scenario_index]
    send = SENDERS[method](packet_name, items, batch)
    latencies, errors, rejected = [], [], 0
    stop_at = time.perf_counter() + duration
    while time.perf_counter() < stop_at:
        start = time.perf_counter()
        try:
            send()
            latencies.append(time.perf_counter() - start)
        except QueueFull:
            rejected += 1
            time.sleep(0.05)  # back off like a real sender would
        except Exception as error:
            errors.append(str(error))
    results.put((latencies, errors, rejected))


def measure(method, scenario_index, clients, duration):
    batch = SCENARIOS[scenario_index][3]
    wait_for_empty_queue()
    results = multiprocessing.Queue()
    procs = [
        multiprocessing.Process(target=client, args=(method, scenario_index, duration, results)) for _ in range(clients)
    ]
    start = time.perf_counter()
    for proc in procs:
        proc.start()
    latencies, errors, rejected = [], [], 0
    for _ in procs:
        got_latencies, got_errors, got_rejected = results.get()
        latencies.extend(got_latencies)
        errors.extend(got_errors)
        rejected += got_rejected
    for proc in procs:
        proc.join()
    sending = time.perf_counter() - start
    # inject_tlm calls have already been published when they return
    draining = wait_for_empty_queue() if method == "json" else 0.0
    latencies.sort()
    packets = len(latencies) * batch
    return {
        "accepted": packets / sending,
        "sustained": packets / (sending + draining),
        "p50": latencies[len(latencies) // 2] * 1e3 if latencies else 0.0,
        "errors": errors,
        "rejected": rejected,
    }


# ------------------------------------------------------------------ output


def check(method):
    """One call up front so bad credentials or a missing target fail clearly."""
    if method == "inject_tlm" and not os.environ.get("OPENC3_API_PASSWORD"):
        return "skipped: set OPENC3_API_PASSWORD"
    if method == "json" and not os.environ.get("JSON_TLM_TOKEN"):
        return "skipped: set JSON_TLM_TOKEN"
    try:
        with contextlib.redirect_stdout(open(os.devnull, "w")):  # noqa: SIM115
            SENDERS[method]("STATUS", STATUS, 1)()
    except Exception as error:
        return f"failed: {str(error)[:150]}"
    return None


def read_back(method, packet_name, items):
    """Send one packet, wait for COSMOS to decom it, and compare what it stored."""
    from openc3.script import get_tlm_packet, tlm

    quiet = open(os.devnull, "w")  # noqa: SIM115
    with contextlib.redirect_stdout(quiet):
        before = tlm(f"{TARGET} {packet_name} RECEIVED_COUNT")
        try:
            SENDERS[method](packet_name, items, 1)()
        except Exception as error:
            return f"ERROR: {str(error).splitlines()[0][:90]}"
        deadline = time.perf_counter() + 10
        while tlm(f"{TARGET} {packet_name} RECEIVED_COUNT") == before:
            if time.perf_counter() > deadline:
                # Accepted, but nothing reached the current value table: COSMOS's
                # decom raised on it (see the DECOM microservice log)
                return "DROPPED by COSMOS decom"
            time.sleep(0.1)
        time.sleep(0.3)  # let decom update the current value table
        stored = {name: value for name, value, _ in get_tlm_packet(f"{TARGET} {packet_name}", type="RAW")}
    wrong = []
    for name, sent in items.items():
        got = stored.get(name)
        if name == "BLOB":
            got, sent = bytes(got or b""), bytes(sent)
        if got != sent:
            shown = repr(got) if len(repr(got)) < 30 else f"{len(got)} long"
            wrong.append(f"{name}={shown}")
    return "OK" if not wrong else "WRONG " + ", ".join(wrong)


DASH = "—"


def main():
    parser = argparse.ArgumentParser(description="inject_tlm vs JSON interface, live")
    parser.add_argument("--duration", type=float, default=5.0, help="seconds per run (default 5)")
    parser.add_argument("--clients", type=int, nargs="+", default=[1, 4, 16], help="client counts (default 1 4 16)")
    parser.add_argument("--only", help="only packets whose label contains this, e.g. VARIABLE")
    parser.add_argument("--no-check", action="store_true", help="skip the read-back correctness check")
    args = parser.parse_args()

    problems = {method: check(method) for method in SENDERS}
    print(f"inject_tlm vs JSON interface — {TARGET} target, {args.duration:g}s per run, rates in packets/s")
    for method, problem in problems.items():
        if problem:
            print(f"  {'JSON interface' if method == 'json' else method}: {problem}")
    print()

    checks = [c for c in CHECKS if not args.only or args.only.lower() in c[0].lower()]
    if checks and not args.no_check and os.environ.get("OPENC3_API_PASSWORD"):
        print("Correctness: each packet sent once, then read back from COSMOS (RAW values)")
        print(f"{'Packet':28} {'inject_tlm':40} {'JSON interface'}")
        for label, packet_name, items in checks:
            results = []
            for method in SENDERS:
                wait_for_empty_queue()
                results.append(read_back(method, packet_name, items) if not problems[method] else DASH)
            print(f"{label:28} {results[0][:40]:40} {results[1][:90]}", flush=True)
        print()

    scenarios = [(i, s) for i, s in enumerate(SCENARIOS) if not args.only or args.only.lower() in s[0].lower()]
    print(f"{'':24} {'':>7}   {'inject_tlm':^15}   {'JSON interface':^33}")
    print(
        f"{'Packet':24} {'Clients':>7}   {'pkt/s':>7} {'p50 ms':>7}   "
        f"{'accepted':>8} {'p50 ms':>7} {'sustained':>9} {'503s':>6}   {'JSON vs inject':>14}"
    )

    notes = []
    for index, (label, _, _, batch) in scenarios:
        for clients in args.clients:
            row = {}
            for method in SENDERS:
                if problems[method] or (method == "inject_tlm" and batch != 1):
                    row[method] = None
                    continue
                row[method] = measure(method, index, clients, args.duration)
                if row[method]["errors"]:
                    errors = row[method]["errors"]
                    notes.append(f"{label}, {clients} clients, {method}: {len(errors)} errors, e.g. {errors[0][:150]}")
            inject, json_result = row["inject_tlm"], row["json"]
            inject_cells = f"{inject['sustained']:7.0f} {inject['p50']:7.1f}" if inject else f"{DASH:>7} {DASH:>7}"
            if json_result:
                json_cells = (
                    f"{json_result['accepted']:8.0f} {json_result['p50']:7.1f} "
                    f"{json_result['sustained']:9.0f} {json_result['rejected']:6}"
                )
            else:
                json_cells = f"{DASH:>8} {DASH:>7} {DASH:>9} {DASH:>6}"
            if inject and json_result and inject["sustained"]:
                ratio = f"{json_result['sustained'] / inject['sustained']:13.1f}x"
            else:
                ratio = f"{DASH:>14}"
            print(f"{label:24} {clients:7}   {inject_cells}   {json_cells}   {ratio}", flush=True)

    print()
    print("sustained = packets/s until COSMOS had published everything (the JSON queue was empty again);")
    print("JSON vs inject compares sustained rates. 503s = requests refused because the queue was full.")
    print("inject_tlm has no batch form: one packet per call.")
    if notes:
        print("\nErrors:")
        for note in notes:
            print(f"  {note}")


if __name__ == "__main__":
    main()
