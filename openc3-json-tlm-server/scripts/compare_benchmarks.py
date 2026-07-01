#!/usr/bin/env python3
"""Run both benchmarks and print a side-by-side comparison.

Runs benchmark_telemetry.py (JsonTelemetryServerInterface) and
benchmark_inject_tlm.py (COSMOS inject_tlm JSON-RPC API) with the same
packet and concurrency settings, then prints a comparison table.

Usage:
    python scripts/compare_benchmarks.py [OPTIONS]

Examples:
    python scripts/compare_benchmarks.py
    python scripts/compare_benchmarks.py --packet PERF_1K --generate-payload
    python scripts/compare_benchmarks.py --if-port 8765 --cosmos-port 2900 --workers 8 --duration 30
"""

import argparse
import importlib.util
import pathlib
import sys


def _load(filename: str):
    path = pathlib.Path(__file__).parent / filename
    spec = importlib.util.spec_from_file_location(filename[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _section(title: str) -> None:
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


def _compare_table(r_if: dict, r_api: dict) -> None:
    rows = [
        ("Throughput mean (req/s)", "throughput_mean", "{:.1f}"),
        ("Throughput peak (req/s)", "throughput_peak", "{:.1f}"),
        ("Latency p50 (ms)",        "p50_ms",          "{:.2f}"),
        ("Latency p95 (ms)",        "p95_ms",          "{:.2f}"),
        ("Latency p99 (ms)",        "p99_ms",          "{:.2f}"),
        ("Latency mean (ms)",       "mean_ms",         "{:.2f}"),
        ("Error rate (%)",          "error_rate",      "{:.2f}"),
        ("Total requests",          "total",           "{:d}"),
    ]

    col_label = 28
    col_val = 14

    header = f"{'Metric':<{col_label}}  {'JsonInterface':>{col_val}}  {'inject_tlm':>{col_val}}  {'Ratio (IF/API)':>{col_val}}"
    print()
    print(header)
    print("-" * len(header))

    for label, key, fmt in rows:
        v_if = r_if[key]
        v_api = r_api[key]
        v_if_str = fmt.format(v_if)
        v_api_str = fmt.format(v_api)
        if v_api != 0:
            ratio = v_if / v_api
            ratio_str = f"{ratio:.2f}x"
        else:
            ratio_str = "—"
        print(f"  {label:<{col_label - 2}}  {v_if_str:>{col_val}}  {v_api_str:>{col_val}}  {ratio_str:>{col_val}}")

    print()
    tput_if = r_if["throughput_mean"]
    tput_api = r_api["throughput_mean"]
    if tput_if > tput_api:
        pct = (tput_if / tput_api - 1) * 100
        print(f"  JsonInterface is {pct:.0f}% higher throughput than inject_tlm")
    elif tput_api > tput_if:
        pct = (tput_api / tput_if - 1) * 100
        print(f"  inject_tlm is {pct:.0f}% higher throughput than JsonInterface")
    else:
        print("  Throughput is equal")

    lat_if = r_if["p50_ms"]
    lat_api = r_api["p50_ms"]
    if lat_if < lat_api:
        pct = (lat_api / lat_if - 1) * 100
        print(f"  JsonInterface p50 latency is {pct:.0f}% lower than inject_tlm")
    elif lat_api < lat_if:
        pct = (lat_if / lat_api - 1) * 100
        print(f"  inject_tlm p50 latency is {pct:.0f}% lower than JsonInterface")
    else:
        print("  p50 latency is equal")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run both benchmarks and compare JsonTelemetryServerInterface vs inject_tlm"
    )
    # Shared
    parser.add_argument("--target", default="JSON_TLM_TEST", help="Target name (default: JSON_TLM_TEST)")
    parser.add_argument("--packet", default="SENSORS", help="Packet name (default: SENSORS)")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent workers (default: 4)")
    parser.add_argument("--duration", type=float, default=10.0, help="Run duration in seconds per benchmark (default: 10)")
    parser.add_argument("--generate-payload", action="store_true", help="Generate payload from tlm.txt")
    parser.add_argument("--tlm-file", default=None, help="Path to tlm.txt (default: auto-resolved)")
    parser.add_argument(
        "--payload",
        default='{"TEMPERATURE": 25.5, "PRESSURE": 1013.2, "COUNT": 1, "STATUS": "OK"}',
        help="JSON payload string (used when --generate-payload is not set)",
    )
    # JsonInterface specific
    parser.add_argument("--if-host", default="localhost", help="JsonInterface host (default: localhost)")
    parser.add_argument("--if-port", type=int, default=8765, help="JsonInterface port (default: 8765)")
    parser.add_argument("--api-key", default=None, help="JsonInterface bearer token (optional)")
    # inject_tlm specific
    parser.add_argument("--cosmos-host", default="localhost", help="COSMOS host (default: localhost)")
    parser.add_argument("--cosmos-port", type=int, default=2900, help="COSMOS Traefik port (default: 2900)")
    parser.add_argument("--password", default="password", help="COSMOS password (default: password)")
    parser.add_argument("--scope", default="DEFAULT", help="COSMOS scope (default: DEFAULT)")
    args = parser.parse_args()

    bench_if = _load("benchmark_telemetry.py")
    bench_api = _load("benchmark_inject_tlm.py")

    import json
    import pathlib as _pathlib

    # Resolve payload once, share between both benchmarks
    if args.generate_payload:
        tlm_file = _pathlib.Path(args.tlm_file) if args.tlm_file else (
            _pathlib.Path(__file__).parent.parent / "targets" / args.target.upper() / "cmd_tlm" / "tlm.txt"
        )
        try:
            item_hash = bench_api.generate_item_hash_from_tlm(args.target, args.packet, tlm_file)
            payload_str = json.dumps(item_hash)
            print(f"Generated payload: {len(item_hash)} items from {tlm_file}")
        except (OSError, ValueError) as e:
            print(f"ERROR generating payload: {e}")
            return 2
    else:
        try:
            item_hash = json.loads(args.payload)
            payload_str = args.payload
        except json.JSONDecodeError as e:
            print(f"ERROR: --payload is not valid JSON: {e}")
            return 2

    _section(f"Benchmark 1/2 — JsonTelemetryServerInterface  ({args.if_host}:{args.if_port}/{args.target}/{args.packet})")
    r_if = bench_if.run_benchmark(
        host=args.if_host,
        port=args.if_port,
        target=args.target,
        packet=args.packet,
        workers=args.workers,
        duration=args.duration,
        payload=payload_str,
        api_key=args.api_key,
    )

    try:
        token = bench_api.get_session_token(args.cosmos_host, args.cosmos_port, args.password)
    except Exception as e:
        print(f"ERROR: Could not authenticate with COSMOS: {e}")
        return 2

    _section(f"Benchmark 2/2 — inject_tlm  ({args.cosmos_host}:{args.cosmos_port}  {args.target}/{args.packet})")
    r_api = bench_api.run_benchmark(
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

    if r_if is None or r_api is None:
        print("\nOne or both benchmarks produced no samples — cannot compare.")
        return 1

    _section("Comparison")
    _compare_table(r_if, r_api)
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
