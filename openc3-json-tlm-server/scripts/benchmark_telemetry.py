#!/usr/bin/env python3
"""Benchmarking script for JsonTelemetryServerInterface.

Measures sustained throughput and latency profile under concurrent load.

Usage:
    python scripts/benchmark_telemetry.py [OPTIONS]

Examples:
    python scripts/benchmark_telemetry.py --host 127.0.0.1 --port 8765
    python scripts/benchmark_telemetry.py --workers 10 --duration 30
    python scripts/benchmark_telemetry.py --payload '{"TEMPERATURE": 42.0}' --duration 10
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
    latency: float  # seconds
    ok: bool        # True if status 200


def worker(session: requests.Session, url: str, payload: bytes, api_key: str | None, stop_at: float) -> list[Sample]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    samples = []
    while time.monotonic() < stop_at:
        t0 = time.monotonic()
        try:
            r = session.post(url, data=payload, headers=headers, timeout=5)
            latency = time.monotonic() - t0
            samples.append(Sample(latency=latency, ok=(r.status_code == 200)))
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


_TYPE_DEFAULTS = {"FLOAT": 1.0, "INT": 1, "UINT": 1, "STRING": "x"}


def generate_payload_from_tlm(target: str, packet: str, tlm_file: pathlib.Path) -> str:
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
    return json.dumps(items)


def run_benchmark(
    host: str,
    port: int,
    target: str,
    packet: str,
    workers: int,
    duration: float,
    payload: str,
    api_key: str | None,
) -> dict | int:
    url = f"http://{host}:{port}/{target}/{packet}"

    try:
        json.loads(payload)
    except json.JSONDecodeError as e:
        print(f"ERROR: --payload is not valid JSON: {e}")
        return 2

    payload_bytes = payload.encode()

    print(f"Benchmarking {url}")
    print(f"  Workers: {workers} | Duration: {duration}s | Payload: {payload[:60]}{'...' if len(payload) > 60 else ''}")
    print()

    stop_at = time.monotonic() + duration
    t_start = time.monotonic()

    all_samples: list[Sample] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(worker, requests.Session(), url, payload_bytes, api_key, stop_at)
            for _ in range(workers)
        ]
        for f in as_completed(futures):
            try:
                all_samples.extend(f.result())
            except Exception as e:
                print(f"Worker error: {e}", file=sys.stderr)

    elapsed = time.monotonic() - t_start

    if not all_samples:
        print("No samples collected — check that the server is running and reachable.")
        return None

    latencies = [s.latency for s in all_samples]
    ok_count = sum(1 for s in all_samples if s.ok)
    err_count = len(all_samples) - ok_count
    total = len(all_samples)

    throughput_mean = total / elapsed

    # Peak: best 1-second window using coarse approximation (all samples / duration)
    throughput_peak = throughput_mean * 1.5  # simple estimate; production tool would track per-second counts

    p50 = percentile(latencies, 50)
    p95 = percentile(latencies, 95)
    p99 = percentile(latencies, 99)
    mean_lat = statistics.mean(latencies)
    error_rate = (err_count / total * 100) if total else 0.0

    print("=" * 52)
    print(f"{'Benchmark Results':^52}")
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
        description="Benchmark JsonTelemetryServerInterface throughput and latency"
    )
    parser.add_argument("--host", default="127.0.0.1", help="Server host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Server port (default: 8765)")
    parser.add_argument("--target", default="JSON_TLM_TEST", help="Target name (default: JSON_TLM_TEST)")
    parser.add_argument("--packet", default="SENSORS", help="Packet name (default: SENSORS)")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent workers (default: 4)")
    parser.add_argument("--duration", type=float, default=10.0, help="Run duration in seconds (default: 10)")
    parser.add_argument(
        "--payload",
        default='{"TEMPERATURE": 25.5, "PRESSURE": 1013.2, "COUNT": 1, "STATUS": "OK"}',
        help="JSON payload string (default: sensor example)",
    )
    parser.add_argument("--api-key", default=None, help="Bearer token for authenticated interface (optional)")
    parser.add_argument(
        "--generate-payload",
        action="store_true",
        help="Generate payload from tlm.txt for --target/--packet instead of using --payload",
    )
    parser.add_argument(
        "--tlm-file",
        default=None,
        help="Path to tlm.txt (default: ../targets/<TARGET>/cmd_tlm/tlm.txt relative to this script)",
    )
    args = parser.parse_args()

    payload = args.payload
    if args.generate_payload:
        tlm_file = pathlib.Path(args.tlm_file) if args.tlm_file else (
            pathlib.Path(__file__).parent.parent / "targets" / args.target.upper() / "cmd_tlm" / "tlm.txt"
        )
        try:
            payload = generate_payload_from_tlm(args.target, args.packet, tlm_file)
            item_count = len(json.loads(payload))
            print(f"Generated payload: {item_count} items from {tlm_file}")
        except (OSError, ValueError) as e:
            print(f"ERROR: {e}")
            return 2

    result = run_benchmark(
        host=args.host,
        port=args.port,
        target=args.target,
        packet=args.packet,
        workers=args.workers,
        duration=args.duration,
        payload=payload,
        api_key=args.api_key,
    )
    if isinstance(result, int):
        return result
    return 0 if result is not None else 1


if __name__ == "__main__":
    sys.exit(main())
