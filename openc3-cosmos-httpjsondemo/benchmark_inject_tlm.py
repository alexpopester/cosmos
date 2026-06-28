#!/usr/bin/env python3
"""
Benchmark for inject_tlm API throughput and latency.

Sends the same four packet types as benchmark_server.py but via the COSMOS
JSON-RPC API endpoint (POST /openc3-api/api → inject_tlm) instead of the
HttpJsonServerInterface HTTP ingest port.

Run both scripts against the same COSMOS instance to compare:
  - HttpJsonServerInterface ingest:  python benchmark_server.py
  - inject_tlm API:                  python benchmark_inject_tlm.py

Key difference: HttpJsonServerInterface accepts packets into a queue and
returns immediately (async decom). inject_tlm is synchronous — it writes to
a Redis topic and waits for the decom microservice to acknowledge, so latency
reflects end-to-end pipeline overhead, not just HTTP accept time.

Usage:
  python benchmark_inject_tlm.py [options]

  --host HOST       COSMOS hostname    (default: localhost)
  --port PORT       Traefik port       (default: 2900)
  --password PASS   COSMOS password    (default: password)
  --scope SCOPE     COSMOS scope       (default: DEFAULT)
  --target TARGET   COSMOS target      (default: HTTPJSONDEMO)
  --requests N      Requests per packet (default: 500)
  --concurrency C   Concurrent workers  (default: 8)
  --warmup N        Warmup requests     (default: 50)
  --packets P,...   Comma-separated list (default: all)
"""

import argparse
import concurrent.futures
import http.client
import json
import sys
import threading
import time

# ---------------------------------------------------------------------------
# Packet definitions — item names must be UPPERCASE to match COSMOS item names.
# These mirror benchmark_server.py exactly so results are directly comparable.
# ---------------------------------------------------------------------------

ALL_PACKETS = {
    "INBOUND": {
        "description": "baseline  — 3 fields  (2 floats, 1 string)",
        "items": {
            "TEMPERATURE": 72.5,
            "PRESSURE": 101.3,
            "LABEL": "bench_sensor",
        },
    },
    "METRICS": {
        "description": "wide numeric — 12 fields (8 floats, 4 uints)",
        "items": {
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
        "items": {
            "EVENT_TYPE": "FAULT",
            "SOURCE": "subsystem_a",
            "MESSAGE": "Threshold exceeded on channel 3 — value 112.4 > limit 100.0",
            "SEVERITY": "WARNING",
            "SEQUENCE": 1001,
        },
    },
    "STATUS": {
        "description": "integer flags — 8 fields  (all uint)",
        "items": {
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
_req_id = threading.local()


def _conn(host: str, port: int) -> http.client.HTTPConnection:
    if getattr(_local, "conn", None) is None:
        _local.conn = http.client.HTTPConnection(host, port, timeout=30)
    return _local.conn


def _next_id() -> int:
    _req_id.counter = getattr(_req_id, "counter", 0) + 1
    return _req_id.counter


def _post(host: str, port: int, body: bytes, headers: dict) -> tuple[bool, float]:
    """POST to /openc3-api/api and return (success, elapsed_s)."""
    t0 = time.perf_counter()
    try:
        c = _conn(host, port)
        c.request("POST", "/openc3-api/api", body, headers)
        resp = c.getresponse()
        status = resp.status
        resp_body = resp.read()
        elapsed = time.perf_counter() - t0
        if status != 200:
            return False, elapsed
        data = json.loads(resp_body)
        return "error" not in data, elapsed
    except Exception:
        try:
            _local.conn.close()
        except Exception:
            pass
        _local.conn = None
        return False, time.perf_counter() - t0


def _build_rpc_body(target: str, packet: str, items: dict, scope: str) -> bytes:
    payload = {
        "jsonrpc": "2.0",
        "method": "inject_tlm",
        "params": [target, packet, items],
        "keyword_params": {"type": "CONVERTED", "scope": scope},
        "id": _next_id(),
    }
    return json.dumps(payload).encode()


def _get_token(host: str, port: int, password: str) -> str | None:
    """Authenticate and return a bearer token, or None on failure.

    The verify endpoint returns the session token as plain text (not JSON).
    The token is then sent as 'Authorization: Bearer <token>' on every request;
    the API controller injects it into keyword_params server-side.
    """
    body = json.dumps({"password": password}).encode()
    headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
    }
    try:
        c = http.client.HTTPConnection(host, port, timeout=10)
        c.request("POST", "/openc3-api/auth/verify", body, headers)
        resp = c.getresponse()
        raw = resp.read()
        c.close()
        if resp.status != 200:
            print(f"Auth failed: HTTP {resp.status} — wrong password?", file=sys.stderr)
            return None
        return raw.decode().strip()
    except Exception as e:
        print(f"Auth failed: {e}", file=sys.stderr)
        return None


def _check_reachable(args: argparse.Namespace, token: str) -> bool:
    """Return True if the COSMOS API responds to a ping."""
    try:
        c = http.client.HTTPConnection(args.host, args.port, timeout=10)
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        c.request("GET", "/openc3-api/ping", headers=headers)
        resp = c.getresponse()
        resp.read()
        c.close()
        return resp.status == 200
    except Exception as e:
        print(f"Cannot reach {args.host}:{args.port} — {e}", file=sys.stderr)
        print("Is COSMOS running? (./openc3.sh run)", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Core benchmark logic
# ---------------------------------------------------------------------------

def _run_packet(name: str, info: dict, args: argparse.Namespace, token: str) -> dict:
    headers = {
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    # Build a representative body for size reporting (id will vary per request)
    sample_body = _build_rpc_body(args.target, name, info["items"], args.scope)
    headers["Content-Length"] = str(len(sample_body))

    def task(_):
        body = _build_rpc_body(args.target, name, info["items"], args.scope)
        h = {**headers, "Content-Length": str(len(body))}
        return _post(args.host, args.port, body, h)

    # Warmup
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        list(pool.map(task, range(args.warmup)))

    # Timed run
    latencies: list[float] = []
    errors = 0

    t_start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        for success, elapsed in pool.map(task, range(args.requests)):
            latencies.append(elapsed)
            if not success:
                errors += 1
    total_s = time.perf_counter() - t_start

    latencies.sort()
    n = len(latencies)

    return {
        "body_bytes": len(sample_body),
        "rps": args.requests / total_s,
        "p50_ms": latencies[n // 2] * 1000,
        "p95_ms": latencies[int(n * 0.95)] * 1000,
        "p99_ms": latencies[int(n * 0.99)] * 1000,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark inject_tlm API throughput and latency",
    )
    parser.add_argument("--host", default="localhost", metavar="HOST")
    parser.add_argument("--port", type=int, default=2900, metavar="PORT")
    parser.add_argument("--password", default="ses_DqUmY5St2QkehkTZ1WkdLw", metavar="PASS",
                        help="COSMOS password for auth (default: password)")
    parser.add_argument("--scope", default="DEFAULT", metavar="SCOPE")
    parser.add_argument("--target", default="HTTPJSONDEMO", metavar="TARGET")
    parser.add_argument("--requests", type=int, default=500, metavar="N",
                        help="Requests per packet type (default: 500)")
    parser.add_argument("--concurrency", type=int, default=8, metavar="C",
                        help="Concurrent worker threads (default: 8)")
    parser.add_argument("--warmup", type=int, default=50, metavar="N",
                        help="Warmup requests before timing (default: 50)")
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

    token = _get_token(args.host, args.port, args.password)
    if token is None:
        print("Tip: pass the correct COSMOS password with --password <pass>", file=sys.stderr)
        sys.exit(1)

    print()
    print("inject_tlm API — ingest benchmark")
    print(f"  Endpoint:    http://{args.host}:{args.port}/openc3-api/api")
    print(f"  Target:      {args.target}  Scope: {args.scope}")
    print(f"  Auth:        token acquired")
    print(f"  Requests:    {args.requests:,} per packet type  ({args.warmup} warmup)")
    print(f"  Concurrency: {args.concurrency} workers")
    print()

    if not _check_reachable(args, token):
        sys.exit(1)

    FMT = "{:<10}  {:>7}  {:<46}  {:>7}  {:>7}  {:>7}  {:>7}  {}"
    print(FMT.format("Packet", "Body", "Description", "req/s", "p50 ms", "p95 ms", "p99 ms", "errors"))
    print("  " + "-" * 112)

    total_errors = 0
    for name, info in packets.items():
        print(f"  {name:<10}  (running...)", end="\r", flush=True)
        result = _run_packet(name, info, args, token)
        total_errors += result["errors"]

        print(FMT.format(
            name,
            f"{result['body_bytes']} B",
            info["description"],
            f"{result['rps']:,.0f}",
            f"{result['p50_ms']:.1f}",
            f"{result['p95_ms']:.1f}",
            f"{result['p99_ms']:.1f}",
            result["errors"] or "",
        ))

    print()
    if total_errors > 0:
        print(f"  {total_errors} error(s) — check COSMOS logs for details.")
    else:
        print("  No errors.")
    print("  Note: inject_tlm is synchronous end-to-end (Redis → decom ACK).")
    print("  Compare with benchmark_server.py for async HttpJsonServerInterface ingest.")
    print()


if __name__ == "__main__":
    main()
