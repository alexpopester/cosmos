#!/usr/bin/env python3
"""Functional test script for JsonTelemetryServerInterface.

Usage:
    python scripts/post_telemetry.py [--host HOST] [--port PORT]
                                     [--target TARGET] [--packet PACKET]
                                     [--api-key API_KEY]

Exercises all nine HTTP contract scenarios and prints a pass/fail summary.
Exits 0 only if every scenario passes.
"""

import argparse
import sys

import requests


def _scenario(label: str, resp: requests.Response, expected_status: int) -> bool:
    ok = resp.status_code == expected_status
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {label}: expected={expected_status} got={resp.status_code}")
    return ok


def run_tests(host: str, port: int, target: str, packet: str, api_key: str | None) -> bool:
    base = f"http://{host}:{port}"
    path = f"/{target}/{packet}"
    valid_body = '{"TEMPERATURE": 25.5, "PRESSURE": 1013.2}'
    headers_ok = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    results = []

    session = requests.Session()

    print("\n=== Scenario group: basic routing (no auth) ===")

    # 1. Valid POST → 200
    r = session.post(base + path, data=valid_body, headers={**headers_ok, "Content-Type": "application/json"})
    results.append(_scenario("200 — valid target/packet/JSON", r, 200))

    # 2. Unknown target → 404
    r = session.post(
        base + "/UNKNOWN_TARGET/" + packet,
        data=valid_body,
        headers={**headers_ok, "Content-Type": "application/json"},
    )
    results.append(_scenario("404 — unknown target name", r, 404))

    # 3. Unknown packet → 404
    r = session.post(
        base + f"/{target}/UNKNOWN_PACKET",
        data=valid_body,
        headers={**headers_ok, "Content-Type": "application/json"},
    )
    results.append(_scenario("404 — known target, unknown packet", r, 404))

    # 4. Malformed JSON → 400
    r = session.post(base + path, data="not-valid-json", headers={**headers_ok, "Content-Type": "application/json"})
    results.append(_scenario("400 — malformed JSON body", r, 400))

    # 5. GET → 405
    r = session.get(base + path, headers=headers_ok)
    results.append(_scenario("405 — GET to valid path", r, 405))

    # 6. PUT → 405
    r = session.put(base + path, data=valid_body, headers={**headers_ok, "Content-Type": "application/json"})
    results.append(_scenario("405 — PUT to valid path", r, 405))

    print("\n=== Scenario group: authentication ===")

    if api_key:
        # 7. Missing Authorization → 401
        r = session.post(base + path, data=valid_body, headers={"Content-Type": "application/json"})
        results.append(_scenario("401 — API key configured, Authorization header missing", r, 401))

        # 8. Wrong token → 401
        r = session.post(
            base + path,
            data=valid_body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer wrong-token"},
        )
        results.append(_scenario("401 — API key configured, wrong token", r, 401))

        # 9. Correct token → 200
        r = session.post(
            base + path,
            data=valid_body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        )
        results.append(_scenario("200 — API key configured, correct bearer token", r, 200))
    else:
        print("  [SKIP] Auth scenarios skipped — no --api-key provided")
        print("         Re-run with --api-key <token> to exercise auth scenarios")

    return all(results)


def main() -> int:
    parser = argparse.ArgumentParser(description="Functional test for JsonTelemetryServerInterface")
    parser.add_argument("--host", default="127.0.0.1", help="Interface host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Interface port (default: 8765)")
    parser.add_argument("--target", default="JSON_TLM_TEST", help="Target name (default: JSON_TLM_TEST)")
    parser.add_argument("--packet", default="SENSORS", help="Packet name (default: SENSORS)")
    parser.add_argument("--api-key", default=None, help="API key for auth scenarios (optional)")
    args = parser.parse_args()

    print(f"Testing JsonTelemetryServerInterface at {args.host}:{args.port}")

    try:
        passed = run_tests(args.host, args.port, args.target, args.packet, args.api_key)
    except requests.exceptions.ConnectionError as e:
        print(f"\nERROR: Could not connect to {args.host}:{args.port}: {e}")
        print("Make sure the plugin is loaded and the COSMOS instance is running.")
        return 2

    print("\n" + ("=" * 48))
    if passed:
        print("All scenarios PASSED")
        return 0
    else:
        print("Some scenarios FAILED — see details above")
        return 1


if __name__ == "__main__":
    sys.exit(main())
