#!/usr/bin/env python3
"""Benchmarking script for the COSMOS inject_tlm JSON-RPC API.

Measures sustained throughput and latency of the inject_tlm path through
the COSMOS cmd-tlm-api (Traefik → Rails → Redis pub/sub).

Usage:
    python scripts/benchmark_inject_tlm.py [OPTIONS]

Examples:
    python scripts/benchmark_inject_tlm.py --cosmos-host localhost --cosmos-port 2900
    python scripts/benchmark_inject_tlm.py --target JSON_TLM_TEST --packet PERF_1K --generate-payload
    python scripts/benchmark_inject_tlm.py --workers 10 --duration 30 --password mypassword
"""

import argparse
import json
import pathlib
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import NamedTuple

import requests


class Sample(NamedTuple):
    latency: float
    ok: bool


_TYPE_DEFAULTS = {"FLOAT": 1.0, "INT": 1, "UINT": 1, "STRING": "x"}


def generate_item_hash_from_tlm(target: str, packet: str, tlm_file: pathlib.Path) -> dict:
    items: dict = {}
    in_packet = False
    with open(tlm_file) as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith("TELEMETRY"):
                parts = stripped.split()
                in_packet = len(parts) >= 3 and parts[1].upper() == target.upper() and parts[2].upper() == packet.upper()
            elif in_packet:
                parts = stripped.split()
                if parts and parts[0] == "APPEND_ITEM" and len(parts) >= 4:
                    name, data_type = parts[1], parts[3]
                    items[name] = _TYPE_DEFAULTS.get(data_type, 0)
                elif parts and parts[0] == "ITEM" and len(parts) >= 5:
                    name, data_type = parts[1], parts[4]
                    items[name] = _TYPE_DEFAULTS.get(data_type, 0)
    if not items:
        raise ValueError(f"No items found for {target}/{packet} in {tlm_file}")
    return items


def get_session_token(cosmos_host: str, cosmos_port: int, password: str) -> str:
    """Exchange a COSMOS password for a session token via auth/verify."""
    url = f"http://{cosmos_host}:{cosmos_port}/openc3-api/auth/verify"
    r = requests.post(url, json={"password": password}, timeout=10)
    r.raise_for_status()
    token = r.text.strip()
    if not token:
        raise ValueError("auth/verify returned an empty token")
    return token


def _make_body(target: str, packet: str, item_hash: dict, scope: str) -> bytes:
    return json.dumps({
        "jsonrpc": "2.0",
        "method": "inject_tlm",
        "params": [target, packet, item_hash],
        "keyword_params": {"type": "CONVERTED", "stored": False, "scope": scope},
        "id": 1,
    }).encode()


def worker(session: requests.Session, url: str, body: bytes, token: str | None, stop_at: float) -> list[Sample]:
    # COSMOS expects the raw session token directly as the Authorization header value,
    # no "Bearer " prefix — this matches how the Python JsonDrbObject client sends it.
    headers = {"Content-Type": "application/json-rpc"}
    if token:
        headers["Authorization"] = token
    samples = []
    while time.monotonic() < stop_at:
        t0 = time.monotonic()
        try:
            r = session.post(url, data=body, headers=headers, timeout=5)
            latency = time.monotonic() - t0
            ok = r.status_code == 200 and "error" not in r.json()
            samples.append(Sample(latency=latency, ok=ok))
        except Exception:
            latency = time.monotonic() - t0
            samples.append(Sample(latency=latency, ok=False))
    return samples


def percentile(values: list[float], p: int) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = max(0, int(len(sorted_vals) * p / 100) - 1)
    return sorted_vals[min(idx, len(sorted_vals) - 1)]


def run_benchmark(
    cosmos_host: str,
    cosmos_port: int,
    target: str,
    packet: str,
    workers: int,
    duration: float,
    item_hash: dict,
    token: str | None,
    scope: str,
) -> dict | None:
    url = f"http://{cosmos_host}:{cosmos_port}/openc3-api/api"
    body = _make_body(target, packet, item_hash, scope)

    print(f"Benchmarking inject_tlm → {url}")
    print(f"  Target/Packet : {target}/{packet} ({len(item_hash)} items)")
    print(f"  Workers: {workers} | Duration: {duration}s")
    print()

    stop_at = time.monotonic() + duration
    t_start = time.monotonic()

    all_samples: list[Sample] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(worker, requests.Session(), url, body, token, stop_at)
            for _ in range(workers)
        ]
        for f in as_completed(futures):
            try:
                all_samples.extend(f.result())
            except Exception as e:
                print(f"Worker error: {e}", file=sys.stderr)

    elapsed = time.monotonic() - t_start

    if not all_samples:
        print("No samples collected — check that COSMOS is running and reachable.")
        return None

    latencies = [s.latency for s in all_samples]
    ok_count = sum(1 for s in all_samples if s.ok)
    err_count = len(all_samples) - ok_count
    total = len(all_samples)

    throughput_mean = total / elapsed
    throughput_peak = throughput_mean * 1.5

    p50 = percentile(latencies, 50)
    p95 = percentile(latencies, 95)
    p99 = percentile(latencies, 99)
    mean_lat = statistics.mean(latencies)
    error_rate = (err_count / total * 100) if total else 0.0

    print("=" * 52)
    print(f"{'Benchmark Results (inject_tlm)':^52}")
    print("=" * 52)
    print(f"  Total requests  : {total}")
    print(f"  Elapsed time    : {elapsed:.2f}s")
    print(f"  Throughput mean : {throughput_mean:.1f} req/s")
    print(f"  Throughput peak : {throughput_peak:.1f} req/s (estimated)")
    print(f"  Latency p50     : {p50 * 1000:.2f} ms")
    print(f"  Latency p95     : {p95 * 1000:.2f} ms")
    print(f"  Latency p99     : {p99 * 1000:.2f} ms")
    print(f"  Latency mean    : {mean_lat * 1000:.2f} ms")
    print(f"  Error rate      : {error_rate:.2f}% ({err_count}/{total})")
    print("=" * 52)

    return {
        "total": total,
        "elapsed": elapsed,
        "throughput_mean": throughput_mean,
        "throughput_peak": throughput_peak,
        "p50_ms": p50 * 1000,
        "p95_ms": p95 * 1000,
        "p99_ms": p99 * 1000,
        "mean_ms": mean_lat * 1000,
        "error_rate": error_rate,
        "error_count": err_count,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark COSMOS inject_tlm API throughput and latency"
    )
    parser.add_argument("--cosmos-host", default="localhost", help="COSMOS host (default: localhost)")
    parser.add_argument("--cosmos-port", type=int, default=2900, help="COSMOS Traefik port (default: 2900)")
    parser.add_argument("--target", default="JSON_TLM_TEST", help="Target name (default: JSON_TLM_TEST)")
    parser.add_argument("--packet", default="SENSORS", help="Packet name (default: SENSORS)")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent workers (default: 4)")
    parser.add_argument("--duration", type=float, default=10.0, help="Run duration in seconds (default: 10)")
    parser.add_argument("--password", default="password", help="COSMOS password (default: password)")
    parser.add_argument("--scope", default="DEFAULT", help="COSMOS scope (default: DEFAULT)")
    parser.add_argument(
        "--generate-payload",
        action="store_true",
        help="Generate item_hash from tlm.txt for --target/--packet instead of using --payload",
    )
    parser.add_argument(
        "--tlm-file",
        default=None,
        help="Path to tlm.txt (default: ../targets/<TARGET>/cmd_tlm/tlm.txt relative to this script)",
    )
    parser.add_argument(
        "--payload",
        default='{"TEMPERATURE": 25.5, "PRESSURE": 1013.2, "COUNT": 1, "STATUS": "OK"}',
        help="JSON object string for item_hash (default: sensor example)",
    )
    args = parser.parse_args()

    if args.generate_payload:
        tlm_file = pathlib.Path(args.tlm_file) if args.tlm_file else (
            pathlib.Path(__file__).parent.parent / "targets" / args.target.upper() / "cmd_tlm" / "tlm.txt"
        )
        try:
            item_hash = generate_item_hash_from_tlm(args.target, args.packet, tlm_file)
            print(f"Generated item_hash: {len(item_hash)} items from {tlm_file}")
        except (OSError, ValueError) as e:
            print(f"ERROR: {e}")
            return 2
    else:
        try:
            item_hash = json.loads(args.payload)
        except json.JSONDecodeError as e:
            print(f"ERROR: --payload is not valid JSON: {e}")
            return 2

    try:
        token = get_session_token(args.cosmos_host, args.cosmos_port, args.password)
    except Exception as e:
        print(f"ERROR: Could not authenticate with COSMOS: {e}")
        return 2

    result = run_benchmark(
        cosmos_host=args.cosmos_host,
        cosmos_port=args.cosmos_port,
        target=args.target,
        packet=args.packet,
        workers=args.workers,
        duration=args.duration,
        item_hash=item_hash,
        token=token,
        scope=args.scope,
    )
    return 0 if result is not None else 1


if __name__ == "__main__":
    sys.exit(main())
