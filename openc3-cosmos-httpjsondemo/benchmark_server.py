#!/usr/bin/env python3
"""
Benchmark for HttpJsonServerInterface ingest throughput and latency.

Sends concurrent HTTP POSTs across four packet types and reports
throughput (req/s) and latency percentiles per packet. Uses only the
Python standard library so it runs in any Python 3.9+ environment with
no pip install step — including a plain python:3-slim Docker container.

One persistent HTTP connection per worker thread is reused across
requests (keep-alive), so measurements reflect steady-state throughput
rather than connection-setup overhead.

Important: req/s is the interface *accept* rate. COSMOS decom runs
asynchronously from the queue. 503 responses mean the queue depth was
exceeded and is the signal that the interface is saturated relative to
how fast COSMOS is consuming packets downstream.

Usage:
  python benchmark_server.py [options]

  --host HOST       Server hostname       (default: localhost)
  --port PORT       Server port           (default: 4567)
  --api-key KEY     X-Api-Key header      (default: none)
  --target TARGET   COSMOS target name    (default: HTTPJSONDEMO)
  --requests N      Requests per packet   (default: 2000)
  --concurrency C   Concurrent workers    (default: 16)
  --warmup N        Warmup requests       (default: 100)
  --packets P,...   Comma-separated list  (default: all)
"""

import argparse
import concurrent.futures
import http.client
import json
import sys
import threading
import time

# ---------------------------------------------------------------------------
# Packet definitions — JSON keys must be UPPERCASE to match COSMOS item names.
# COSMOS upcases all item names (e.g. APPEND_ITEM power_on → key "POWER_ON"),
# so incoming JSON must use the same uppercase keys for JsonAccessor to parse them.
# ---------------------------------------------------------------------------

ALL_PACKETS = {
    "INBOUND": {
        "description": "baseline  — 3 fields  (2 floats, 1 string)",
        "body": {
            "TEMPERATURE": 72.5,
            "PRESSURE": 101.3,
            "LABEL": "bench_sensor",
        },
    },
    "METRICS": {
        "description": "wide numeric — 12 fields (8 floats, 4 uints)",
        "body": {
            "CPU_PCT": 45.2,
            "MEM_PCT": 62.1,
            "DISK_PCT": 78.3,
            "NET_RX_KBPS": 1024.5,
            "NET_TX_KBPS": 512.0,
            "LOAD_1M": 1.45,
            "LOAD_5M": 1.32,
            "LOAD_15M": 1.10,
            "UPTIME_SECS": 86400,
            "PROC_COUNT": 142,
            "THREAD_COUNT": 891,
            "FD_COUNT": 2048,
        },
    },
    "EVENT": {
        "description": "string-heavy — 5 fields  (4 strings, 1 uint)",
        "body": {
            "EVENT_TYPE": "FAULT",
            "SOURCE": "subsystem_a",
            "MESSAGE": "Threshold exceeded on channel 3 — value 112.4 > limit 100.0",
            "SEVERITY": "WARNING",
            "SEQUENCE": 1001,
        },
    },
    "STATUS": {
        "description": "integer flags — 8 fields  (all uint)",
        "body": {
            "POWER_ON": 1,
            "COMM_ACTIVE": 1,
            "SAFE_MODE": 0,
            "ERROR_FLAGS": 0,
            "MODE": 2,
            "CYCLE_COUNT": 50000,
            "LAST_CMD_SEQ": 999,
            "CHECKSUM": 65280,
        },
    },
}

# ---------------------------------------------------------------------------
# HTTP transport — one persistent connection per worker thread.
# ---------------------------------------------------------------------------

_local = threading.local()


def _conn(host: str, port: int) -> http.client.HTTPConnection:
    if getattr(_local, "conn", None) is None:
        _local.conn = http.client.HTTPConnection(host, port, timeout=10)
    return _local.conn


def _post(host: str, port: int, path: str, body: bytes, headers: dict) -> tuple[int, float]:
    t0 = time.perf_counter()
    try:
        c = _conn(host, port)
        c.request("POST", path, body, headers)
        resp = c.getresponse()
        status = resp.status
        resp.read()  # drain so the connection can be reused
        return status, time.perf_counter() - t0
    except Exception:
        # Reset broken connection; next call reconnects automatically.
        try:
            _local.conn.close()
        except Exception:
            pass
        _local.conn = None
        return 0, time.perf_counter() - t0


def _check_reachable(args: argparse.Namespace) -> bool:
    """Return True if the server responds to any HTTP request."""
    headers = {"Content-Type": "application/json", "Content-Length": "2"}
    if args.api_key:
        headers["X-Api-Key"] = args.api_key
    # POST to a single-segment path — server returns 400 (not 0) if it's up.
    status, _ = _post(args.host, args.port, "/__ping", b"{}", headers)
    if status == 0:
        print(f"Cannot reach {args.host}:{args.port} — connection refused or timeout.",
              file=sys.stderr)
        print("Is the COSMOS operator running and the plugin loaded?", file=sys.stderr)
        return False
    return True


# ---------------------------------------------------------------------------
# Core benchmark logic
# ---------------------------------------------------------------------------

def _run_packet(name: str, info: dict, args: argparse.Namespace) -> dict:
    path = f"/{args.target.lower()}/{name.lower()}"
    body = json.dumps(info["body"]).encode()
    headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
    }
    if args.api_key:
        headers["X-Api-Key"] = args.api_key

    def task(_):
        return _post(args.host, args.port, path, body, headers)

    # Warmup: prime keep-alive connections and let COSMOS reach steady state.
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        list(pool.map(task, range(args.warmup)))

    # Timed run.
    latencies: list[float] = []
    status_counts: dict[int, int] = {}

    t_start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        for status, elapsed in pool.map(task, range(args.requests)):
            latencies.append(elapsed)
            status_counts[status] = status_counts.get(status, 0) + 1
    total_s = time.perf_counter() - t_start

    latencies.sort()
    n = len(latencies)
    errors = args.requests - status_counts.get(200, 0)

    return {
        "body_bytes": len(body),
        "rps": args.requests / total_s,
        "p50_ms": latencies[n // 2] * 1000,
        "p95_ms": latencies[int(n * 0.95)] * 1000,
        "p99_ms": latencies[int(n * 0.99)] * 1000,
        "errors": errors,
        "status_counts": status_counts,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark HttpJsonServerInterface ingest throughput",
    )
    parser.add_argument("--host", default="localhost", metavar="HOST")
    parser.add_argument("--port", type=int, default=4567, metavar="PORT")
    parser.add_argument("--api-key", default="", metavar="KEY",
                        help="Value for X-Api-Key header (omit if auth not configured)")
    parser.add_argument("--target", default="HTTPJSONDEMO", metavar="TARGET")
    parser.add_argument("--requests", type=int, default=2000, metavar="N",
                        help="Requests per packet type (default: 2000)")
    parser.add_argument("--concurrency", type=int, default=16, metavar="C",
                        help="Concurrent worker threads (default: 16)")
    parser.add_argument("--warmup", type=int, default=100, metavar="N",
                        help="Warmup requests before timing (default: 100)")
    parser.add_argument("--packets", default="", metavar="P,...",
                        help="Comma-separated packet names to test (default: all)")
    args = parser.parse_args()

    packets = {
        k: v for k, v in ALL_PACKETS.items()
        if not args.packets or k in {p.strip().upper() for p in args.packets.split(",")}
    }
    if not packets:
        print(f"No matching packets. Available: {', '.join(ALL_PACKETS)}", file=sys.stderr)
        sys.exit(1)

    print()
    print("HttpJsonServerInterface — ingest benchmark")
    print(f"  Endpoint:    http://{args.host}:{args.port}")
    print(f"  Target:      {args.target}")
    print(f"  Auth:        {'enabled' if args.api_key else 'disabled'}")
    print(f"  Requests:    {args.requests:,} per packet type  ({args.warmup} warmup)")
    print(f"  Concurrency: {args.concurrency} workers")
    print()

    if not _check_reachable(args):
        sys.exit(1)

    FMT = "{:<10}  {:>7}  {:<46}  {:>7}  {:>7}  {:>7}  {:>7}  {}"
    print(FMT.format("Packet", "Body", "Description", "req/s", "p50 ms", "p95 ms", "p99 ms", "errors"))
    print("  " + "-" * 112)

    total_errors = 0
    for name, info in packets.items():
        print(f"  {name:<10}  (running...)", end="\r", flush=True)
        result = _run_packet(name, info, args)
        total_errors += result["errors"]

        error_col = str(result["errors"])
        if result["errors"] > 0:
            non_200 = {k: v for k, v in result["status_counts"].items() if k != 200}
            error_col += f"  {non_200}"

        print(FMT.format(
            name,
            f"{result['body_bytes']} B",
            info["description"],
            f"{result['rps']:,.0f}",
            f"{result['p50_ms']:.1f}",
            f"{result['p95_ms']:.1f}",
            f"{result['p99_ms']:.1f}",
            error_col,
        ))

    print()
    if total_errors > 0:
        print("  503 errors → queue depth exceeded: COSMOS decom is slower than ingest.")
        print("  Lower --concurrency or raise MAX_QUEUE_DEPTH in plugin.txt.")
    else:
        print("  No errors. All packets accepted within queue depth.")
    print("  Note: req/s is the accept rate at the HTTP layer. Decom runs async from the queue.")
    print()


if __name__ == "__main__":
    main()
