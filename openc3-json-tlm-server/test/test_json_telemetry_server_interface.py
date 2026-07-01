# Copyright 2026 OpenC3, Inc.
# All Rights Reserved.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See LICENSE.md for more details.
#
# This file may also be used under the terms of a commercial license
# if purchased from OpenC3, Inc.

import json
import threading
import time
import unittest
import urllib.request
from urllib.error import HTTPError

# conftest.py adds ../python to sys.path so this resolves to the plugin source.
from json_telemetry_server_interface import JsonTelemetryServerInterface
from openc3.packets.packet import Packet
from openc3.packets.packet_item import PacketItem
from test.test_helper import mock_redis, setup_system


def _make_packet(items):
    """Return a Packet with the given {name: (bit_size, data_type)} items."""
    pkt = Packet("JSON_TLM_TEST", "SENSORS")
    offset = 0
    for name, (bit_size, data_type) in items.items():
        item = PacketItem(name, offset, bit_size, data_type, "BIG_ENDIAN")
        item.key = name
        pkt.define(item)
        offset += bit_size
    pkt.restore_defaults()
    return pkt


PORT = 18765


def _post(path, body, headers=None, method="POST", port=PORT):
    url = f"http://127.0.0.1:{port}{path}"
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read()
    except HTTPError as e:
        return e.code, e.read()


class TestJsonTelemetryServerInterfaceMapping(unittest.TestCase):
    """Unit tests for JSON-to-item mapping logic (no live server)."""

    def setUp(self):
        mock_redis(self)
        setup_system()

    def _make_interface(self):
        return JsonTelemetryServerInterface(PORT + 1)

    def test_maps_matching_keys_case_insensitively(self):
        iface = self._make_interface()
        pkt = _make_packet({"TEMPERATURE": (32, "FLOAT"), "PRESSURE": (32, "FLOAT")})
        iface._map_json_to_packet({"temperature": 25.5, "PRESSURE": 1013.2}, pkt)
        self.assertAlmostEqual(pkt.read("TEMPERATURE"), 25.5, places=2)
        self.assertAlmostEqual(pkt.read("PRESSURE"), 1013.2, places=1)

    def test_ignores_unrecognized_keys(self):
        iface = self._make_interface()
        pkt = _make_packet({"TEMPERATURE": (32, "FLOAT")})
        iface._map_json_to_packet({"TEMPERATURE": 10.0, "UNKNOWN_KEY": 99}, pkt)
        self.assertAlmostEqual(pkt.read("TEMPERATURE"), 10.0, places=2)

    def test_missing_keys_retain_defaults(self):
        iface = self._make_interface()
        pkt = _make_packet({"TEMPERATURE": (32, "FLOAT"), "COUNT": (32, "UINT")})
        pkt.restore_defaults()
        iface._map_json_to_packet({"TEMPERATURE": 5.0}, pkt)
        self.assertAlmostEqual(pkt.read("TEMPERATURE"), 5.0, places=2)
        self.assertEqual(pkt.read("COUNT"), 0)

    def test_int_json_value_to_float_item(self):
        iface = self._make_interface()
        pkt = _make_packet({"TEMPERATURE": (32, "FLOAT")})
        iface._map_json_to_packet({"TEMPERATURE": 30}, pkt)
        self.assertAlmostEqual(pkt.read("TEMPERATURE"), 30.0, places=2)

    def test_string_value_to_string_item(self):
        iface = self._make_interface()
        pkt = _make_packet({"STATUS": (128, "STRING")})
        iface._map_json_to_packet({"STATUS": "OK"}, pkt)
        self.assertEqual(pkt.read("STATUS").rstrip("\x00"), "OK")

    def test_uint_value(self):
        iface = self._make_interface()
        pkt = _make_packet({"COUNT": (32, "UINT")})
        iface._map_json_to_packet({"count": 42}, pkt)
        self.assertEqual(pkt.read("COUNT"), 42)

    def test_mixed_case_key_variants(self):
        iface = self._make_interface()
        pkt = _make_packet({"TEMPERATURE": (32, "FLOAT")})
        for key in ["temperature", "Temperature", "TEMPERATURE", "tEmPeRaTuRe"]:
            pkt.restore_defaults()
            iface._map_json_to_packet({key: 1.0}, pkt)
            self.assertAlmostEqual(pkt.read("TEMPERATURE"), 1.0, places=2, msg=f"key={key}")


class TestJsonTelemetryServerInterfaceHTTP(unittest.TestCase):
    """Integration tests against a live server instance."""

    def setUp(self):
        from unittest.mock import patch

        mock_redis(self)
        setup_system()

        self.iface = JsonTelemetryServerInterface(PORT)
        sensors_pkt = _make_packet(
            {
                "TEMPERATURE": (32, "FLOAT"),
                "PRESSURE": (32, "FLOAT"),
                "COUNT": (32, "UINT"),
                "STATUS": (128, "STRING"),
            }
        )
        health_pkt = _make_packet({"UPTIME": (32, "UINT")})

        def fake_packet(target, pkt_name):
            lookup = {
                ("JSON_TLM_TEST", "SENSORS"): sensors_pkt,
                ("JSON_TLM_TEST", "HEALTH"): health_pkt,
            }
            result = lookup.get((target.upper(), pkt_name.upper()))
            if result is None:
                raise RuntimeError(f"Unknown packet {target}/{pkt_name}")
            return result

        # Patch System in the plugin module's namespace (not openc3's)
        self._patch = patch("json_telemetry_server_interface.System")
        mock_sys = self._patch.start()
        mock_sys.telemetry.packet.side_effect = fake_packet

        self.iface.target_names = ["JSON_TLM_TEST"]
        self.iface.connect()
        time.sleep(0.1)

    def tearDown(self):
        self.iface.disconnect()
        self._patch.stop()

    def test_valid_post_returns_200(self):
        status, _ = _post("/JSON_TLM_TEST/SENSORS", '{"TEMPERATURE": 25.5}')
        self.assertEqual(status, 200)

    def test_valid_post_queues_packet_data(self):
        _post("/JSON_TLM_TEST/SENSORS", '{"TEMPERATURE": 99.0}')
        time.sleep(0.05)
        self.assertFalse(self.iface.server.request_queue.empty())

    def test_malformed_json_returns_400(self):
        status, _ = _post("/JSON_TLM_TEST/SENSORS", "not-json")
        self.assertEqual(status, 400)

    def test_empty_body_returns_400(self):
        status, _ = _post("/JSON_TLM_TEST/SENSORS", "")
        self.assertEqual(status, 400)

    def test_unknown_target_returns_404(self):
        status, _ = _post("/UNKNOWN_TARGET/SENSORS", '{"TEMPERATURE": 1.0}')
        self.assertEqual(status, 404)

    def test_unknown_packet_returns_404(self):
        status, _ = _post("/JSON_TLM_TEST/UNKNOWN_PKT", '{"TEMPERATURE": 1.0}')
        self.assertEqual(status, 404)

    def test_too_few_path_segments_returns_404(self):
        status, _ = _post("/JSON_TLM_TEST", '{"TEMPERATURE": 1.0}')
        self.assertEqual(status, 404)

    def test_get_returns_405(self):
        status, _ = _post("/JSON_TLM_TEST/SENSORS", "", method="GET")
        self.assertEqual(status, 405)

    def test_put_returns_405(self):
        status, _ = _post("/JSON_TLM_TEST/SENSORS", '{"TEMPERATURE": 1.0}', method="PUT")
        self.assertEqual(status, 405)

    def test_delete_returns_405(self):
        status, _ = _post("/JSON_TLM_TEST/SENSORS", "", method="DELETE")
        self.assertEqual(status, 405)


class TestJsonTelemetryServerInterfaceAuth(unittest.TestCase):
    """Authentication tests (API_KEY option)."""

    AUTH_PORT = PORT + 10

    def setUp(self):
        from unittest.mock import patch

        mock_redis(self)
        setup_system()

        self.iface = JsonTelemetryServerInterface(self.AUTH_PORT)
        self.iface.set_option("API_KEY", ["secret-token-123"])

        sensors_pkt = _make_packet({"TEMPERATURE": (32, "FLOAT")})

        def fake_packet(target, pkt_name):
            if target.upper() == "JSON_TLM_TEST" and pkt_name.upper() == "SENSORS":
                return sensors_pkt
            raise RuntimeError(f"Unknown packet {target}/{pkt_name}")

        self._patch = patch("json_telemetry_server_interface.System")
        mock_sys = self._patch.start()
        mock_sys.telemetry.packet.side_effect = fake_packet

        self.iface.target_names = ["JSON_TLM_TEST"]
        self.iface.connect()
        time.sleep(0.1)

    def tearDown(self):
        self.iface.disconnect()
        self._patch.stop()

    def _post_auth(self, auth_header=None):
        headers = {}
        if auth_header is not None:
            headers["Authorization"] = auth_header
        return _post("/JSON_TLM_TEST/SENSORS", '{"TEMPERATURE": 1.0}', headers=headers, port=self.AUTH_PORT)

    def test_correct_token_returns_200(self):
        status, _ = self._post_auth("Bearer secret-token-123")
        self.assertEqual(status, 200)

    def test_missing_auth_header_returns_401(self):
        status, _ = self._post_auth(None)
        self.assertEqual(status, 401)

    def test_wrong_token_returns_401(self):
        status, _ = self._post_auth("Bearer wrong-token")
        self.assertEqual(status, 401)

    def test_no_api_key_configured_accepts_all(self):
        from unittest.mock import patch as p2

        iface2 = JsonTelemetryServerInterface(self.AUTH_PORT + 1)
        sensors_pkt = _make_packet({"TEMPERATURE": (32, "FLOAT")})

        def fake_packet2(target, pkt_name):
            if target.upper() == "JSON_TLM_TEST" and pkt_name.upper() == "SENSORS":
                return sensors_pkt
            raise RuntimeError(f"Unknown packet {target}/{pkt_name}")

        with p2("json_telemetry_server_interface.System") as ms:
            ms.telemetry.packet.side_effect = fake_packet2
            iface2.target_names = ["JSON_TLM_TEST"]
            iface2.connect()
            time.sleep(0.1)
            try:
                url = f"http://127.0.0.1:{self.AUTH_PORT + 1}/JSON_TLM_TEST/SENSORS"
                req = urllib.request.Request(url, data=b'{"TEMPERATURE": 1.0}', method="POST")
                req.add_header("Content-Type", "application/json")
                with urllib.request.urlopen(req) as resp:
                    self.assertEqual(resp.status, 200)
            finally:
                iface2.disconnect()

    def test_set_option_calls_super_listen_address(self):
        iface = JsonTelemetryServerInterface(PORT + 20)
        iface.set_option("API_KEY", ["tok"])
        iface.set_option("LISTEN_ADDRESS", ["127.0.0.1"])
        self.assertEqual(iface.listen_address, "127.0.0.1")
        self.assertEqual(iface._api_key, "tok")


if __name__ == "__main__":
    unittest.main()
