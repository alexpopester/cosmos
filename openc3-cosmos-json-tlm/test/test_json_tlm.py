# Tests run against the real OpenC3 COSMOS Python library (openc3/python in the
# COSMOS repo) using the repo's own fakeredis test harness. They load this
# plugin's JSONDEMO packet definition, run the real HTTP server, and push data
# through COSMOS's own InterfaceMicroservice.handle_packet and
# DecomMicroservice.decom_packet code into the current value table.
#
# Run from the COSMOS python dir so the repo's test helper is importable:
#   cd cosmos/openc3/python
#   COSMOS_PLUGIN_DIR=/path/to/openc3-cosmos-json-tlm python -m pytest $COSMOS_PLUGIN_DIR/test -v

import json
import os
import sys
import unittest
import urllib.error
import urllib.request
from types import SimpleNamespace

PLUGIN_DIR = os.environ.get("COSMOS_PLUGIN_DIR", os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.join(PLUGIN_DIR, "lib"))

from test.test_helper import mock_redis  # noqa: E402  (COSMOS repo test harness)

from openc3.interfaces.interface import Interface  # noqa: E402
from openc3.microservices.decom_microservice import DecomMicroservice  # noqa: E402
from openc3.microservices.interface_microservice import InterfaceMicroservice  # noqa: E402
from openc3.models.cvt_model import CvtModel  # noqa: E402
from openc3.models.target_model import TargetModel  # noqa: E402
from openc3.system.system import System  # noqa: E402
from openc3.utilities.logger import Logger  # noqa: E402
from openc3.utilities.store import Store  # noqa: E402

from json_packet_protocol import JsonPacketProtocol, JsonTlmError, build_packet_from_json  # noqa: E402
from json_tlm_server_interface import JsonTlmServerInterface  # noqa: E402

TOKEN = "test-token"
TOPIC = "DEFAULT__TELEMETRY__{JSONDEMO}__STATUS"


def load_system():
    System.instance_obj = None
    TargetModel.clear_packet_cache()
    System.instance(["JSONDEMO"], os.path.join(PLUGIN_DIR, "targets"))


class TestBuildPacket(unittest.TestCase):
    """The JSON -> binary packet logic on its own."""

    def setUp(self):
        mock_redis(self)
        load_system()

    def build(self, items, **body):
        return build_packet_from_json({"items": items, **body}, "JSONDEMO", "STATUS", allowed_targets=["JSONDEMO"])

    def test_raw_values_round_trip_through_definition(self):
        pkt = self.build({"COUNTER": 42, "TEMP": 12000, "VOLTAGE": 3300, "MODE": 2, "LABEL": "hello", "WHEELS": [1, -2, 3, -4]})
        self.assertEqual(pkt.read("COUNTER", "RAW"), 42)
        self.assertAlmostEqual(pkt.read("TEMP"), 20.0)  # -100 + 0.01*12000
        self.assertAlmostEqual(pkt.read("VOLTAGE"), 3.3)
        self.assertEqual(pkt.read("MODE"), "SCIENCE")
        self.assertEqual(pkt.read("LABEL"), "hello")
        self.assertEqual(pkt.read("WHEELS"), [1, -2, 3, -4])
        self.assertEqual(pkt.read("PKTID"), 1)  # ID item comes from the definition default

    def test_json_accessor_packet_buffer_is_the_document(self):
        pkt = build_packet_from_json(
            {"items": {"COUNTER": 5, "TEMP": 20.0, "MODE": "SCIENCE", "WHEELS": [1, -2]}, "type": "CONVERTED"},
            "JSONDEMO", "STATUSJ",
        )
        # Write conversion and state applied, nothing packed into bits
        self.assertEqual(
            json.loads(pkt.buffer), {"COUNTER": 5, "TEMP": 12000, "MODE": 2, "WHEELS": [1, -2]}
        )
        self.assertAlmostEqual(pkt.read("TEMP"), 20.0)
        self.assertAlmostEqual(pkt.read("TEMP_X2"), 40.0)
        # Unsent items are absent, so they read as nil rather than 0
        self.assertIsNone(pkt.read("VOLTAGE"))

    def test_json_accessor_packet_errors(self):
        with self.assertRaises(JsonTlmError) as ctx:
            build_packet_from_json({"items": {"NOPE": 1, "VOLTAGE": 3.3}, "type": "CONVERTED"}, "JSONDEMO", "STATUSJ")
        self.assertIn("NOPE: no such item", str(ctx.exception))
        self.assertIn("VOLTAGE: has a read conversion but no write conversion", str(ctx.exception))

    def test_json_accessor_packet_last_starts_from_last_document(self):
        System.telemetry.packet("JSONDEMO", "STATUSJ").buffer = b'{"COUNTER": 1, "VOLTAGE": 3300}'
        pkt = build_packet_from_json({"items": {"COUNTER": 2}}, "JSONDEMO", "STATUSJ", missing_items="LAST")
        self.assertEqual(json.loads(pkt.buffer), {"COUNTER": 2, "VOLTAGE": 3300})

    def test_converted_uses_write_conversion_and_states(self):
        pkt = self.build({"TEMP": 20.0, "MODE": "NOMINAL"}, type="CONVERTED")
        self.assertEqual(pkt.read("TEMP", "RAW"), 12000)
        self.assertAlmostEqual(pkt.read("TEMP"), 20.0)
        self.assertEqual(pkt.read("MODE", "RAW"), 1)

    def test_converted_refuses_double_conversion(self):
        # VOLTAGE has a read conversion and no write conversion: inject_tlm would silently
        # store 3.3 as raw and later report 0.0033 V. We reject it instead.
        with self.assertRaises(JsonTlmError) as ctx:
            self.build({"VOLTAGE": 3.3}, type="CONVERTED")
        self.assertIn("no write conversion", str(ctx.exception))

    def test_missing_items_get_defaults_not_last_value(self):
        System.telemetry.packet("JSONDEMO", "STATUS").write("COUNTER", 999, "RAW")  # simulate a previous packet
        pkt = self.build({"TEMP": 1})
        self.assertEqual(pkt.read("COUNTER", "RAW"), 0)

    def test_errors_are_collected_and_nothing_is_built(self):
        with self.assertRaises(JsonTlmError) as ctx:
            self.build({"NOPE": 1, "TEMP_X2": 5, "MODE": "BOGUS", "COUNTER": 1})
        msg = str(ctx.exception)
        self.assertIn("NOPE: no such item", msg)
        self.assertIn("TEMP_X2: DERIVED", msg)
        self.assertIn("MODE", msg)

    def test_value_out_of_range_rejected(self):
        with self.assertRaises(JsonTlmError):
            self.build({"TEMP": 70000})  # 16 bit UINT

    def test_unknown_packet_and_unmapped_target(self):
        with self.assertRaises(JsonTlmError) as ctx:
            build_packet_from_json({"items": {"X": 1}}, "JSONDEMO", "NOPE")
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(JsonTlmError) as ctx:
            build_packet_from_json({"items": {"X": 1}}, "JSONDEMO", "STATUS", allowed_targets=["OTHER"])
        self.assertEqual(ctx.exception.status, 403)

    def test_received_time_and_stored(self):
        pkt = self.build({"COUNTER": 1}, received_time="2026-10-08T12:00:00Z", stored=True)
        self.assertEqual(pkt.received_time.isoformat(), "2026-10-08T12:00:00+00:00")
        self.assertTrue(pkt.stored)
        pkt = self.build({"COUNTER": 1}, received_time=1_760_000_000_000_000_000)
        self.assertEqual(int(pkt.received_time.timestamp()), 1_760_000_000)


class TestDynamicItems(unittest.TestCase):
    """Legacy dynamic items and VARIABLE_BIT_SIZE items, checked by decoding with COSMOS itself."""

    def setUp(self):
        mock_redis(self)
        load_system()

    def build(self, packet_name, items, **kwargs):
        return build_packet_from_json({"items": items}, "JSONDEMO", packet_name, **kwargs)

    def decom(self, packet_name, buffer):
        """Decode a buffer the way the decom microservice does, from the definition."""
        packet = System.telemetry.packet("JSONDEMO", packet_name).deep_copy()
        packet.buffer = buffer
        return packet

    def layout(self, packet_name):
        packet = System.telemetry.packet("JSONDEMO", packet_name)
        return [(i.name, i.bit_offset, i.bit_size, i.array_size) for i in packet.sorted_items]

    def test_variable_items_round_trip_and_fill_their_length_items(self):
        built = self.build("VARIABLE", {
            "NAME": "héllo", "SAMPLES": [1, -2, 3], "BLOB": [1, 2, 3, 4, 5], "VINT": 1000, "TRAILER": 7,
        })
        pkt = self.decom("VARIABLE", built.buffer)
        self.assertEqual(pkt.read("NAME_LEN"), 6)  # UTF-8 bytes, not characters
        self.assertEqual(pkt.read("NAME"), "héllo")
        self.assertEqual(pkt.read("COUNT"), 3)  # elements: VARIABLE_BIT_SIZE COUNT 16 on a 16 bit array
        self.assertEqual(pkt.read("SAMPLES"), [1, -2, 3])
        self.assertEqual(pkt.read("BLOB_LEN"), 7)  # 5 bytes + the -16 bit length offset
        self.assertEqual(pkt.read("BLOB"), b"\x01\x02\x03\x04\x05")
        self.assertEqual(pkt.read("VINT_LEN"), 1)  # QUIC code for a 14 bit integer
        self.assertEqual(pkt.read("VINT"), 1000)
        self.assertEqual(pkt.read("TRAILER", "RAW"), 7)  # fixed item after them all shifted correctly
        self.assertEqual(pkt.read("PKTID"), 4)

    def test_definition_is_not_changed_by_variable_writes(self):
        before = self.layout("VARIABLE")
        self.build("VARIABLE", {"NAME": "a long name", "SAMPLES": list(range(10)), "VINT": 70000})
        self.assertEqual(self.layout("VARIABLE"), before)

    def test_item_order_in_json_does_not_matter(self):
        items = {"NAME": "abc", "SAMPLES": [5, 6], "BLOB": "xyz", "VINT": 70000, "TRAILER": 9}
        forward = self.build("VARIABLE", items).buffer
        backward = self.build("VARIABLE", dict(reversed(list(items.items())))).buffer
        self.assertEqual(forward, backward)

    def test_quic_integer_sizes(self):
        for value, code in ((10, 0), (1000, 1), (70000, 2), (2**40, 3)):
            built = self.build("VARIABLE", {"VINT": value, "TRAILER": 5}, empty_variable_items="ALLOW")
            pkt = self.decom("VARIABLE", built.buffer)
            self.assertEqual((pkt.read("VINT_LEN"), pkt.read("VINT"), pkt.read("TRAILER", "RAW")), (code, value, 5))

    def test_unsent_variable_items_are_empty(self):
        pkt = self.decom("VARIABLE", self.build("VARIABLE", {"TRAILER": 2}).buffer)
        self.assertEqual(pkt.read("NAME_LEN"), 0)
        self.assertEqual(pkt.read("COUNT"), 0)
        self.assertEqual(pkt.read("BLOB_LEN"), 2)  # length 2 = empty, because of the -16 bit offset
        self.assertEqual(pkt.read("VINT"), 0)
        self.assertEqual(pkt.read("TRAILER", "RAW"), 2)

    def test_length_items_are_checked_not_written(self):
        pkt = self.build("VARIABLE", {"NAME": "abc", "NAME_LEN": 3})
        self.assertEqual(pkt.read("NAME"), "abc")
        with self.assertRaises(JsonTlmError) as ctx:
            self.build("VARIABLE", {"NAME": "abc", "NAME_LEN": 5})
        self.assertIn("NAME_LEN: is the length of NAME", str(ctx.exception))

    def test_last_keeps_previous_variable_items(self):
        defined = System.telemetry.packet("JSONDEMO", "VARIABLE")
        defined.buffer = self.build("VARIABLE", {"NAME": "abc", "SAMPLES": [9, 9], "VINT": 1000}).buffer
        pkt = self.decom("VARIABLE", self.build("VARIABLE", {"TRAILER": 3}, missing_items="LAST").buffer)
        self.assertEqual((pkt.read("NAME"), pkt.read("SAMPLES"), pkt.read("VINT")), ("abc", [9, 9], 1000))
        self.assertEqual(pkt.read("TRAILER", "RAW"), 3)

    def test_last_falls_back_to_defaults_when_last_buffer_is_invalid(self):
        # Nothing received yet: the all-zero buffer has BLOB_LEN 0, a -16 bit BLOB
        pkt = self.decom("VARIABLE", self.build("VARIABLE", {"NAME": "x"}, missing_items="LAST").buffer)
        self.assertEqual((pkt.read("NAME"), pkt.read("BLOB_LEN"), pkt.read("PKTID")), ("x", 2, 4))

    def test_legacy_dynamic_items(self):
        pkt = self.decom("TAIL", self.build("TAIL", {"SEQ": 1, "MESSAGE": "hello wörld"}).buffer)
        self.assertEqual((pkt.read("SEQ"), pkt.read("MESSAGE")), (1, "hello wörld"))
        pkt = self.decom("TAILARRAY", self.build("TAILARRAY", {"CRC": 9, "DATA": [1, 2, 3]}).buffer)
        self.assertEqual((pkt.read("DATA"), pkt.read("CRC")), ([1, 2, 3], 9))

    def test_guard_rejects_empty_items_cosmos_decom_cannot_decode(self):
        # Seen live: SAMPLES (INT16, VARIABLE_BIT_SIZE) left empty makes COSMOS decom
        # read it as the rest of the packet, which isn't whole INT16s, so decom
        # raises and drops the packet. CHECK turns that into a 400 for the sender.
        items = {"NAME": "hello", "BLOB": [1, 2, 3]}
        with self.assertRaises(JsonTlmError) as ctx:
            self.build("VARIABLE", items)
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("SAMPLES: empty, and COSMOS decom can't decode", str(ctx.exception))
        # Confirm the guard matches COSMOS: decoding that exact buffer really fails
        built = self.build("VARIABLE", items, empty_variable_items="ALLOW")
        with self.assertRaises(ValueError):
            self.decom("VARIABLE", built.buffer).read("SAMPLES")

    def test_guard_allows_empty_items_cosmos_can_decode(self):
        # Empty NAME and BLOB read wrong in COSMOS but don't break decom, so CHECK lets them through
        pkt = self.decom("VARIABLE", self.build("VARIABLE", {"SAMPLES": [1, 2], "VINT": 5}).buffer)
        self.assertEqual(pkt.read("SAMPLES"), [1, 2])
        self.assertEqual(pkt.read("NAME_LEN"), 0)

    def test_guard_reject_mode_refuses_any_empty_item(self):
        with self.assertRaises(JsonTlmError) as ctx:
            self.build("VARIABLE", {"SAMPLES": [1, 2]}, empty_variable_items="REJECT")
        self.assertIn("NAME, BLOB: empty", str(ctx.exception))
        everything = {"NAME": "a", "SAMPLES": [1], "BLOB": [1], "VINT": 1}
        self.assertEqual(self.build("VARIABLE", everything, empty_variable_items="REJECT").read("NAME"), "a")

    def test_block_accepts_json_binary_forms(self):
        for blob in ([1, 2, 255], {"json_class": "String", "raw": [1, 2, 255]}):
            built = self.build("VARIABLE", {"BLOB": blob}, empty_variable_items="ALLOW")
            pkt = self.decom("VARIABLE", built.buffer)
            self.assertEqual(pkt.read("BLOB"), b"\x01\x02\xff")


class TestProtocolOnAnyInterface(unittest.TestCase):
    """JsonPacketProtocol attached to a generic interface (e.g. MQTT/TCP carrying JSON)."""

    def setUp(self):
        mock_redis(self)
        load_system()

    def test_protocol_converts_and_drops_bad_messages(self):
        interface = Interface()
        interface.name = "GENERIC_INT"
        interface.tlm_target_names = ["JSONDEMO"]
        proto = JsonPacketProtocol("RAW", "DEFAULTS")
        proto.interface = interface
        good = interface.convert_data_to_packet(json.dumps({"target": "JSONDEMO", "packet": "STATUS", "items": {"COUNTER": 7}}).encode(), None)
        out = proto.read_packet(good)
        self.assertEqual((out.target_name, out.packet_name), ("JSONDEMO", "STATUS"))
        self.assertEqual(out.read("COUNTER"), 7)
        bad = interface.convert_data_to_packet(b"{not json", None)
        self.assertEqual(proto.read_packet(bad), "STOP")  # dropped, interface stays up
        self.assertEqual(proto.dropped_count, 1)


class TestRestEndToEnd(unittest.TestCase):
    """HTTP POST -> interface -> COSMOS handle_packet -> raw stream -> COSMOS decom -> CVT."""

    def setUp(self):
        self.redis = mock_redis(self)
        load_system()
        Logger.stdout = False
        TargetModel(folder_name="JSONDEMO", name="JSONDEMO", scope="DEFAULT").create()
        self.interface = JsonTlmServerInterface(0, "/json-tlm")  # port 0 = pick a free port
        self.interface.name = "JSON_TLM_INT"
        self.interface.target_names = ["JSONDEMO"]
        self.interface.tlm_target_names = ["JSONDEMO"]
        self.interface.tlm_target_enabled = {"JSONDEMO": True}
        self.interface.set_option("AUTH_TOKEN", [TOKEN])
        self.interface.connect()
        self.addCleanup(self.interface.disconnect)
        self.base = f"http://127.0.0.1:{self.interface.server.server_address[1]}/json-tlm"

        # Minimal stand-ins for the microservice objects; the methods run are COSMOS's own code
        self.int_ms = SimpleNamespace(interface=self.interface, scope="DEFAULT", queued=False, cancel_thread=True, logger=Logger)
        self.decom_ms = SimpleNamespace(
            scope="DEFAULT", stored_limits_mode="PROCESS", error_count=0, error=None, logger=Logger,
            metric=SimpleNamespace(set=lambda **kw: None), target_names=["JSONDEMO"],
        )

    def request(self, method, path, body=None, token=TOKEN):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def pump(self, count=1, packet_name="STATUS"):
        """Run each queued packet through COSMOS's interface + decom code paths."""
        topic = f"DEFAULT__TELEMETRY__{{JSONDEMO}}__{packet_name}"
        for _ in range(count):
            packet = self.interface.read()
            InterfaceMicroservice.handle_packet(self.int_ms, packet)
        for msg_id, msg_hash in Store.xrange(topic)[-count:]:
            DecomMicroservice.decom_packet(self.decom_ms, topic, msg_id.decode(), msg_hash, None)
        self.assertEqual(self.decom_ms.error_count, 0, self.decom_ms.error)

    def cvt(self, item, value_type="CONVERTED", packet_name="STATUS"):
        return CvtModel.get_item("JSONDEMO", packet_name, item, type=value_type, cache_timeout=0, scope="DEFAULT")

    def test_json_accessor_packet_lands_in_current_value_table(self):
        status, _ = self.request("POST", "/tlm/JSONDEMO/STATUSJ", {
            "items": {"COUNTER": 5, "TEMP": 12000, "MODE": 1, "LABEL": "rest", "WHEELS": [10, 20]},
        })
        self.assertEqual(status, 202)
        self.pump(packet_name="STATUSJ")
        topic = "DEFAULT__TELEMETRY__{JSONDEMO}__STATUSJ"
        self.assertEqual(json.loads(Store.xrange(topic)[-1][1][b"buffer"])["TEMP"], 12000)  # the raw log holds JSON
        self.assertAlmostEqual(self.cvt("TEMP", packet_name="STATUSJ"), 20.0)
        self.assertAlmostEqual(self.cvt("TEMP_X2", packet_name="STATUSJ"), 40.0)
        self.assertEqual(self.cvt("MODE", packet_name="STATUSJ"), "NOMINAL")
        self.assertEqual(self.cvt("WHEELS", packet_name="STATUSJ"), [10, 20])
        self.assertIsNone(self.cvt("VOLTAGE", "RAW", packet_name="STATUSJ"))

    def test_variable_packets_land_in_current_value_table(self):
        # Two packets of different sizes, so decom has to re-layout between them
        for name, samples, vint in (("first", [1, 2, 3, 4], 70000), ("2nd", [-7], 9)):
            status, _ = self.request("POST", "/tlm/JSONDEMO/VARIABLE", {
                "items": {"NAME": name, "SAMPLES": samples, "BLOB": [0xAA, 0xBB], "VINT": vint, "TRAILER": 10},
            })
            self.assertEqual(status, 202)
            self.pump(packet_name="VARIABLE")
            self.assertEqual(self.cvt("NAME", packet_name="VARIABLE"), name)
            self.assertEqual(self.cvt("COUNT", packet_name="VARIABLE"), len(samples))
            self.assertEqual(self.cvt("SAMPLES", packet_name="VARIABLE"), samples)
            self.assertEqual(self.cvt("VINT", packet_name="VARIABLE"), vint)
            self.assertEqual(self.cvt("TRAILER", packet_name="VARIABLE"), 5.0)

    def test_schema_marks_dynamic_and_length_items(self):
        status, schema = self.request("GET", "/tlm/JSONDEMO/VARIABLE")
        self.assertEqual(status, 200)
        items = {i["name"]: i for i in schema["items"]}
        self.assertEqual(items["NAME_LEN"]["length_of"], "NAME")
        self.assertTrue(items["SAMPLES"]["dynamic"])
        self.assertEqual(items["SAMPLES"]["variable_bit_size"]["length_item_name"], "COUNT")
        self.assertFalse(items["TRAILER"]["dynamic"])

    def test_post_lands_in_current_value_table(self):
        status, body = self.request("POST", "/tlm/jsondemo/status", {
            "items": {"COUNTER": 5, "TEMP": 12000, "VOLTAGE": 3300, "MODE": 1, "LABEL": "rest", "WHEELS": [10, 20, 30, 40]},
        })
        self.assertEqual((status, body), (202, {"accepted": 1}))
        self.pump()
        self.assertEqual(self.cvt("COUNTER"), 5)
        self.assertAlmostEqual(self.cvt("TEMP"), 20.0)
        self.assertAlmostEqual(self.cvt("TEMP_X2"), 40.0)  # derived item computed by COSMOS decom
        self.assertAlmostEqual(self.cvt("VOLTAGE"), 3.3)
        self.assertEqual(self.cvt("MODE"), "NOMINAL")
        self.assertEqual(self.cvt("MODE", "RAW"), 1)
        self.assertEqual(self.cvt("LABEL"), "rest")
        self.assertEqual(self.cvt("WHEELS"), [10, 20, 30, 40])
        self.assertEqual(self.cvt("TEMP", "FORMATTED"), "20.0 C")
        # Raw stream entry exists, which is what the raw logger and reingest consume
        raw = Store.xrange(TOPIC)[-1][1]
        self.assertEqual(raw[b"buffer"], System.telemetry.packet("JSONDEMO", "STATUS").buffer)

    def test_limits_are_checked_by_normal_decom(self):
        self.request("POST", "/tlm/JSONDEMO/STATUS", {"type": "CONVERTED", "items": {"TEMP": 70.0}})
        self.pump()
        _, limits_state = CvtModel.get_tlm_values([["JSONDEMO", "STATUS", "TEMP", "CONVERTED"]], scope="DEFAULT")[0]
        self.assertEqual(limits_state, "YELLOW_HIGH")

    def test_batch_is_all_or_nothing(self):
        status, body = self.request("POST", "/tlm", {"packets": [
            {"target": "JSONDEMO", "packet": "STATUS", "items": {"COUNTER": 1}},
            {"target": "JSONDEMO", "packet": "STATUS", "items": {"COUNTER": "abc"}},
        ]})
        self.assertEqual(status, 400)
        self.assertIn("packets[1]", body["error"])
        self.assertEqual(self.interface.request_queue.qsize(), 0)

        status, body = self.request("POST", "/tlm", [
            {"target": "JSONDEMO", "packet": "STATUS", "items": {"COUNTER": 1}},
            {"target": "JSONDEMO", "packet": "STATUS", "items": {"COUNTER": 2}},
        ])
        self.assertEqual((status, body), (202, {"accepted": 2}))
        self.pump(2)
        self.assertEqual(self.cvt("COUNTER"), 2)

    def test_auth_and_errors(self):
        self.assertEqual(self.request("POST", "/tlm/JSONDEMO/STATUS", {"COUNTER": 1}, token=None)[0], 401)
        self.assertEqual(self.request("POST", "/tlm/JSONDEMO/STATUS", {"COUNTER": 1}, token="wrong")[0], 401)
        self.assertEqual(self.request("POST", "/tlm/JSONDEMO/NOPE", {"COUNTER": 1})[0], 404)
        self.assertEqual(self.request("POST", "/tlm/OTHER/STATUS", {"COUNTER": 1})[0], 403)
        self.assertEqual(self.request("GET", "/health", token=None)[0], 200)

    def test_flat_body_and_schema(self):
        self.assertEqual(self.request("POST", "/tlm/JSONDEMO/STATUS", {"COUNTER": 77})[0], 202)
        self.pump()
        self.assertEqual(self.cvt("COUNTER"), 77)
        status, schema = self.request("GET", "/tlm/JSONDEMO/STATUS")
        self.assertEqual(status, 200)
        voltage = next(i for i in schema["items"] if i["name"] == "VOLTAGE")
        self.assertEqual((voltage["read_conversion"], voltage["write_conversion"]), (True, False))
        self.assertNotIn("TEMP_X2", [i["name"] for i in schema["items"]])

    def test_disabled_mapping_rejects_with_409(self):
        self.interface.tlm_target_enabled["JSONDEMO"] = False
        status, body = self.request("POST", "/tlm/JSONDEMO/STATUS", {"COUNTER": 1})
        self.assertEqual(status, 409)

    def test_queue_backpressure(self):
        self.interface.max_queue = 1
        self.assertEqual(self.request("POST", "/tlm/JSONDEMO/STATUS", {"COUNTER": 1})[0], 202)
        self.assertEqual(self.request("POST", "/tlm/JSONDEMO/STATUS", {"COUNTER": 2})[0], 503)

    def test_server_uses_larger_connection_backlog(self):
        # Raised from socketserver's default of 5 so connection bursts queue in
        # the OS backlog instead of being refused (and 502ing through Traefik).
        self.assertEqual(self.interface.server.request_queue_size, 128)
        self.assertTrue(self.interface.server.daemon_threads)
        # The listening socket is actually up and accepting
        self.assertIsNotNone(self.interface.server.socket.getsockname())


if __name__ == "__main__":
    unittest.main()
