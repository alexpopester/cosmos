# Benchmarks for the JSON telemetry plugin, run against the real COSMOS Python
# library with the repo's fakeredis harness (no running COSMOS needed).
#
#   ./test/run_bench.sh build                 # per-packet cost: JSON parse, build, COSMOS decom
#   ./test/run_bench.sh http                  # HTTP load against the real server
#   ./test/run_bench.sh build --lib /old/lib  # same cases against another copy of lib/
#
# Numbers are for one process on this machine. The HTTP server shares one core
# (the GIL) with the drain thread that stands in for the interface microservice.

import argparse
import json
import multiprocessing
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace


PLUGIN_DIR = os.environ.get("COSMOS_PLUGIN_DIR", os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def setup(lib_dir):
    sys.path.insert(0, lib_dir)
    from openc3.models.target_model import TargetModel
    from openc3.system.system import System
    from openc3.utilities.logger import Logger
    from test.test_helper import mock_redis  # COSMOS repo test harness

    mock_redis(SimpleNamespace(addCleanup=lambda *args: None))
    Logger.stdout = False
    Logger.level = Logger.FATAL
    System.instance_obj = None
    TargetModel.clear_packet_cache()
    System.instance(["JSONDEMO"], os.path.join(PLUGIN_DIR, "targets"))


def cases():
    status_all = {
        "COUNTER": 5,
        "TEMP": 12000,
        "VOLTAGE": 3300,
        "MODE": 1,
        "LABEL": "rest",
        "WHEELS": [10, 20, 30, 40],
    }
    variable_small = {"NAME": "hello", "SAMPLES": [1, -2, 3], "BLOB": [1, 2, 3, 4, 5], "VINT": 1000, "TRAILER": 7}
    variable_large = {
        "NAME": "x" * 200,
        "SAMPLES": [i % 30000 for i in range(2048)],
        "BLOB": [i % 256 for i in range(4096)],
        "VINT": 70000,
        "TRAILER": 7,
    }
    return [
        # (label, packet, items, kwargs, fixed-size baseline packet for comparison)
        ("STATUS all items", "STATUS", status_all, {}),
        ("STATUSJ all items (JSON accessor)", "STATUSJ", status_all, {}),
        ("STATUS 1 item", "STATUS", {"COUNTER": 1}, {}),
        ("STATUS CONVERTED", "STATUS", {"TEMP": 20.0, "MODE": "NOMINAL"}, {"default_value_type": "CONVERTED"}),
        ("WAVE 2048 x u16", "WAVE", {"TIMESTAMP": 1, "SAMPLES": [i % 65536 for i in range(2048)]}, {}),
        ("WAVEJ 2048 x u16 (JSON accessor)", "WAVEJ", {"TIMESTAMP": 1, "SAMPLES": [i % 65536 for i in range(2048)]}, {}),
        ("WAVEBIG 8192 x u16", "WAVEBIG", {"TIMESTAMP": 1, "SAMPLES": [i % 65536 for i in range(8192)]}, {}),
        ("VARIABLE small", "VARIABLE", variable_small, {}),
        ("VARIABLE small LAST", "VARIABLE", variable_small, {"missing_items": "LAST"}),
        ("VARIABLE 1 item", "VARIABLE", {"TRAILER": 7}, {}),
        ("VARIABLE large (2048 x i16 + 4 KB)", "VARIABLE", variable_large, {}),
        ("TAIL 1000 char string", "TAIL", {"SEQ": 1, "MESSAGE": "m" * 1000}, {}),
        ("TAILARRAY 2048 x u16", "TAILARRAY", {"DATA": [i % 65536 for i in range(2048)], "CRC": 9}, {}),
    ]


def timeit(fn, min_time=0.5, min_runs=20):
    fn()  # warm up
    times = []
    start = time.perf_counter()
    while len(times) < min_runs or time.perf_counter() - start < min_time:
        t0 = time.perf_counter()
        fn()
        times.append(time.perf_counter() - t0)
    return statistics.median(times)


def bench_build(args):
    setup(args.lib)
    from json_packet_protocol import build_packet_from_json

    from openc3.system.system import System

    print(f"lib: {args.lib}")
    print(
        f"{'case':38} {'bytes':>6} {'json body':>9} {'parse us':>9} {'build us':>9} {'decom us':>9} {'build pkt/s':>11}"
    )
    for label, packet_name, items, kwargs in cases():
        body = json.dumps({"items": items})
        try:
            built = build_packet_from_json(json.loads(body), "JSONDEMO", packet_name, **kwargs)
            buffer = built.buffer
            # Prove the bytes are what COSMOS decodes, so we never time a wrong answer
            check = System.telemetry.packet("JSONDEMO", packet_name).deep_copy()
            check.buffer = buffer
            for name, value in items.items():
                if name == "BLOB":
                    ok = bytes(check.read(name)) == bytes(value)
                elif kwargs.get("default_value_type") == "CONVERTED":
                    ok = check.read(name) == value or abs(check.read(name) - value) < 1e-9
                else:
                    ok = check.read(name, "RAW") == value
                if not ok:
                    raise ValueError(f"{name} decoded wrong")
        except Exception as error:
            print(f"{label:38} FAILED: {str(error)[:80]}")
            continue

        parse = timeit(lambda body=body: json.loads(body))

        def build_once(body=body, packet_name=packet_name, kwargs=kwargs):
            build_packet_from_json(json.loads(body), "JSONDEMO", packet_name, **kwargs)

        build = timeit(build_once) - parse

        # What the decom microservice pays: update the definition then read every item
        defined = System.telemetry.packet("JSONDEMO", packet_name)

        def decom(defined=defined, buffer=buffer):
            defined.buffer = buffer
            defined.read_all("CONVERTED")

        decom_time = timeit(decom)
        print(
            f"{label:38} {len(buffer):6} {len(body):9} {parse * 1e6:9.1f} {build * 1e6:9.1f} "
            f"{decom_time * 1e6:9.1f} {1 / (build + parse):11.0f}"
        )


# ---------------------------------------------------------------- HTTP load


def client_worker(url, token, bodies, duration, threads, out_queue):
    """One client process running `threads` threads, each posting in a loop."""
    results = []
    lock = threading.Lock()
    stop_at = time.perf_counter() + duration

    def run():
        local = []
        i = 0
        while time.perf_counter() < stop_at:
            data = bodies[i % len(bodies)]
            i += 1
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Authorization", f"Bearer {token}")
            t0 = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    resp.read()
                    status = resp.status
            except urllib.error.HTTPError as error:
                status = error.code
            except Exception:
                status = 0
            local.append((time.perf_counter() - t0, status))
        with lock:
            results.extend(local)

    workers = [threading.Thread(target=run) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    out_queue.put(results)


def bench_http(args):
    setup(args.lib)
    from json_tlm_server_interface import JsonTlmServerInterface

    from openc3.system.system import System

    interface = JsonTlmServerInterface(0, "/json-tlm")
    interface.name = "JSON_TLM_INT"
    interface.target_names = ["JSONDEMO"]
    interface.tlm_target_names = ["JSONDEMO"]
    interface.tlm_target_enabled = {"JSONDEMO": True}
    interface.set_option("AUTH_TOKEN", ["bench"])
    interface.max_queue = 1_000_000
    interface.connect()
    port = interface.server.server_address[1]

    # Stand-in for the interface microservice thread: read each queued packet and
    # update the definition (the CPU part of handle_packet; the Redis write is left out)
    drained = [0]

    def drain():
        while True:
            packet = interface.read()
            if packet is None:
                return
            System.telemetry.update(packet.target_name, packet.packet_name, packet.buffer)
            drained[0] += 1

    threading.Thread(target=drain, daemon=True).start()

    by_name = {label: (packet_name, items) for label, packet_name, items, _ in cases()}
    scenarios = [
        ("STATUS single", "STATUS all items", 1),
        ("VARIABLE small single", "VARIABLE small", 1),
        ("VARIABLE large single", "VARIABLE large (2048 x i16 + 4 KB)", 1),
        ("WAVE single", "WAVE 2048 x u16", 1),
        ("STATUS batch 100", "STATUS all items", 100),
        ("VARIABLE small batch 100", "VARIABLE small", 100),
    ]
    if args.only:
        scenarios = [s for s in scenarios if args.only.lower() in s[0].lower()]

    print(f"{'scenario':26} {'clients':>7} {'req/s':>8} {'pkt/s':>8} {'p50 ms':>8} {'p99 ms':>8} {'errors':>6}")
    for name, case, batch in scenarios:
        packet_name, items = by_name[case]
        if batch == 1:
            url = f"http://127.0.0.1:{port}/json-tlm/tlm/JSONDEMO/{packet_name}"
            bodies = [json.dumps({"items": items}).encode()]
        else:
            url = f"http://127.0.0.1:{port}/json-tlm/tlm"
            entry = {"target": "JSONDEMO", "packet": packet_name, "items": items}
            bodies = [json.dumps({"packets": [entry] * batch}).encode()]
        for clients in args.clients:
            out = multiprocessing.Queue()
            procs_count = min(clients, args.client_procs)
            per_proc = [clients // procs_count + (1 if i < clients % procs_count else 0) for i in range(procs_count)]
            procs = [
                multiprocessing.Process(target=client_worker, args=(url, "bench", bodies, args.duration, threads, out))
                for threads in per_proc
            ]
            for proc in procs:
                proc.start()
            results = []
            for _ in procs:
                results.extend(out.get())
            for proc in procs:
                proc.join()
            latencies = sorted(t for t, status in results if status == 202)
            errors = sum(1 for _, status in results if status != 202)
            ok = len(latencies)
            rps = ok / args.duration
            p50 = latencies[len(latencies) // 2] * 1e3 if latencies else 0
            p99 = latencies[int(len(latencies) * 0.99)] * 1e3 if latencies else 0
            print(f"{name:26} {clients:7} {rps:8.0f} {rps * batch:8.0f} {p50:8.1f} {p99:8.1f} {errors:6}")
            # Let the drain thread catch up so one run's backlog doesn't slow the next
            while interface.request_queue.qsize():
                time.sleep(0.05)
    interface.disconnect()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["build", "http"])
    parser.add_argument("--lib", default=os.path.join(PLUGIN_DIR, "lib"), help="plugin lib dir to benchmark")
    parser.add_argument("--duration", type=float, default=5.0, help="seconds per HTTP run")
    parser.add_argument("--clients", type=int, nargs="+", default=[1, 4, 16, 64])
    parser.add_argument("--client-procs", type=int, default=4, help="processes the HTTP clients are spread over")
    parser.add_argument("--only", help="only HTTP scenarios whose name contains this")
    args = parser.parse_args()
    if args.mode == "build":
        bench_build(args)
    else:
        bench_http(args)


if __name__ == "__main__":
    main()
