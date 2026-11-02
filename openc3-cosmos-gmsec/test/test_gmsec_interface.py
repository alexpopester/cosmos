# Tests run against the real OpenC3 COSMOS Python library (openc3/python in the
# COSMOS repo) using the repo's fakeredis harness, with a fake GMSEC binding
# (test/fake_gmsec) in place of libgmsec_python3. Received messages go through
# COSMOS's own InterfaceMicroservice.handle_packet and
# DecomMicroservice.decom_packet into the current value table.
#
#   ./test/run_tests.sh

import os
import struct
import sys
import threading
import time
import unittest
from types import SimpleNamespace


PLUGIN_DIR = os.environ.get("COSMOS_PLUGIN_DIR", os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.join(PLUGIN_DIR, "lib"))
sys.path.insert(0, os.path.join(PLUGIN_DIR, "test", "fake_gmsec"))

import gmsec_interface  # noqa: E402
import libgmsec_python3 as gmsec  # noqa: E402  (the fake; FAKE_GMSEC_API picks 4.x or 5.x)
from gmsec_interface import GmsecInterface  # noqa: E402

from openc3.microservices.decom_microservice import DecomMicroservice  # noqa: E402
from openc3.microservices.interface_microservice import InterfaceMicroservice  # noqa: E402
from openc3.models.cvt_model import CvtModel  # noqa: E402
from openc3.models.target_model import TargetModel  # noqa: E402
from openc3.system.system import System  # noqa: E402
from openc3.utilities.logger import Logger  # noqa: E402
from openc3.utilities.store import Store  # noqa: E402
from test.test_helper import mock_redis  # noqa: E402  (COSMOS repo test harness)


SUBJECT = "GMSEC.DEMO.SAT1.MSG.TLM.CCSDSPKT"


def ccsds(apid, seq, payload):
    return struct.pack(">HHH", apid, 0xC000 | seq, len(payload) - 1) + payload


def health(temp1=21.5, temp2=-3.25, mode=2, seq=1):
    return ccsds(100, seq, struct.pack(">hhB", round(temp1 * 100), round(temp2 * 100), mode))


def tlm_message(data, subject=SUBJECT, data_field="DATA", **fields):
    # Roughly a COMPAT C2 / C2MS TLM CCSDSPKT message
    msg = gmsec.Message(subject, gmsec.Message.PUBLISH)
    msg.add_field("MESSAGE-TYPE", "MSG")
    msg.add_field("MESSAGE-SUBTYPE", "TLM")
    msg.add_field("FORMAT", "CCSDSPKT")
    msg.add_field("STREAM-MODE", "RT")
    for name, value in fields.items():
        msg.add_field(name.replace("_", "-"), value)
    if data is not None:
        msg.add_field(data_field, data, len(data))
    return msg


def load_system():
    System.instance_obj = None
    TargetModel.clear_packet_cache()
    System.instance(["GMSECDEMO"], os.path.join(PLUGIN_DIR, "targets"))


class GmsecTestCase(unittest.TestCase):
    def setUp(self):
        self.redis = mock_redis(self)
        load_system()
        Logger.stdout = False
        while not gmsec.FakeConnection.bus.empty():
            gmsec.FakeConnection.bus.get_nowait()
        gmsec.FakeConnection.instances.clear()

    def make_interface(self, *subjects, **options):
        interface = GmsecInterface("bolt", "localhost:9100", *(subjects or (SUBJECT,)))
        interface.name = "GMSEC_INT"
        interface.target_names = ["GMSECDEMO"]
        interface.tlm_target_names = ["GMSECDEMO"]
        interface.tlm_target_enabled = {"GMSECDEMO": True}
        interface.set_option("RECEIVE_TIMEOUT", ["0.05"])
        for name, values in options.items():
            interface.set_option(name, values)
        self.addCleanup(interface.disconnect)
        return interface

    def publish(self, *messages):
        for msg in messages:
            gmsec.FakeConnection.bus.put(msg)


class TestConnection(GmsecTestCase):
    def test_connect_builds_config_and_subscribes(self):
        interface = self.make_interface(
            SUBJECT,
            "GMSEC.OTHER.>",
            SUBSCRIBE=["GMSEC.THIRD"],
            CONFIG=["gmsec-schema-level", "0"],
            MW_PASSWORD=["s3cret"],
        )
        interface.connect()
        self.assertTrue(interface.connected())
        conn_mgr = gmsec.FakeConnection.instances[-1]
        self.assertTrue(conn_mgr.started)
        self.assertEqual(conn_mgr.subscribed, [SUBJECT, "GMSEC.OTHER.>", "GMSEC.THIRD"])
        self.assertEqual(conn_mgr.config.values["mw-id"], "bolt")
        self.assertEqual(conn_mgr.config.values["server"], "localhost:9100")
        self.assertEqual(conn_mgr.config.values["gmsec-schema-level"], "0")
        self.assertEqual(conn_mgr.config.values["mw-password"], "s3cret")

    def test_detects_the_api_version(self):
        self.make_interface().connect()
        self.assertEqual(gmsec_interface.api.major, gmsec.API)

    def test_no_server_leaves_it_to_gmsec_defaults(self):
        interface = GmsecInterface("mb", "None", SUBJECT)
        interface.connect()
        self.addCleanup(interface.disconnect)
        self.assertNotIn("server", gmsec.FakeConnection.instances[-1].config.values)

    def test_disconnect_unsubscribes_and_cleans_up(self):
        interface = self.make_interface()
        interface.connect()
        conn_mgr = gmsec.FakeConnection.instances[-1]
        interface.disconnect()
        self.assertFalse(interface.connected())
        self.assertEqual(conn_mgr.subscribed, [])
        self.assertTrue(conn_mgr.closed)

    def test_failed_initialize_cleans_up_and_raises(self):
        interface = GmsecInterface("broken", "nowhere", SUBJECT)
        with self.assertRaises(gmsec.GmsecError):
            interface.connect()
        self.assertFalse(interface.connected())
        self.assertTrue(gmsec.FakeConnection.instances[-1].closed)

    def test_requires_a_subject(self):
        with self.assertRaisesRegex(RuntimeError, "subject"):
            GmsecInterface("bolt", "localhost").connect()

    def test_disconnect_unblocks_a_waiting_read(self):
        interface = self.make_interface()
        interface.connect()
        result = {}
        reader = threading.Thread(target=lambda: result.setdefault("data", interface.read_interface()))
        reader.start()
        time.sleep(0.1)
        interface.disconnect()
        reader.join(2)
        self.assertFalse(reader.is_alive())
        self.assertEqual(result["data"], (None, None))

    def test_is_telemetry_only(self):
        interface = self.make_interface()
        self.assertFalse(interface.write_allowed)
        self.assertFalse(interface.write_raw_allowed)
        with self.assertRaisesRegex(RuntimeError, "telemetry only"):
            interface.write_interface(b"\x00")


class TestExtract(GmsecTestCase):
    def test_raw_bytes_and_extra_fields(self):
        interface = self.make_interface(EXTRA_FIELD=["STREAM-MODE", "COLLECTION-POINT", "MISSING"])
        interface.connect()
        raw = health()
        msg = tlm_message(raw, COLLECTION_POINT="WALLOPS")
        self.publish(msg)
        data, extra = interface.read_interface()
        self.assertEqual(data, raw)
        self.assertIsInstance(data, bytes)
        self.assertEqual(extra, {"GMSEC_SUBJECT": SUBJECT, "STREAM-MODE": "RT", "COLLECTION-POINT": "WALLOPS"})
        self.assertTrue(msg.released)

    def test_messages_without_the_data_field_are_skipped(self):
        interface = self.make_interface()
        interface.connect()
        no_data = tlm_message(None)
        wrong_type = tlm_message(None, DATA="not binary")
        self.publish(no_data, wrong_type, tlm_message(health(seq=7)))
        data, _ = interface.read_interface()
        self.assertEqual(data, health(seq=7))
        self.assertTrue(no_data.released and wrong_type.released)

    def test_custom_and_auto_data_field(self):
        raw = health(seq=3)
        interface = self.make_interface(DATA_FIELD=["PKT-BYTES"])
        interface.connect()
        self.publish(tlm_message(raw, data_field="PKT-BYTES"))
        self.assertEqual(interface.read_interface()[0], raw)
        interface.disconnect()

        interface = self.make_interface(DATA_FIELD=["AUTO"])
        interface.connect()
        self.publish(tlm_message(raw, data_field="WHATEVER"))
        self.assertEqual(interface.read_interface()[0], raw)

    def test_filters_must_all_match(self):
        interface = self.make_interface(FILTER=["MESSAGE-SUBTYPE", "TLM"])
        interface.set_option("FILTER", ["STREAM-MODE", "RT"])
        interface.connect()
        replay = tlm_message(health(seq=1))
        replay.add_field("STREAM-MODE", "RPY")
        log = tlm_message(health(seq=2))
        log.add_field("MESSAGE-SUBTYPE", "LOG")
        self.publish(replay, log, tlm_message(health(seq=3)))
        self.assertEqual(interface.read_interface()[0], health(seq=3))


class TestEndToEnd(GmsecTestCase):
    """GMSEC message -> interface -> COSMOS handle_packet -> raw stream -> COSMOS decom -> CVT."""

    def setUp(self):
        super().setUp()
        TargetModel(folder_name="GMSECDEMO", name="GMSECDEMO", scope="DEFAULT").create()
        self.interface = self.make_interface(SUBJECT, "GMSEC.DEMO.SAT1.MSG.TLM.EVENT", EXTRA_FIELD=["STREAM-MODE"])
        self.interface.connect()
        self.int_ms = SimpleNamespace(
            interface=self.interface, scope="DEFAULT", queued=False, cancel_thread=True, logger=Logger
        )
        self.int_ms._known_tlm_packet = lambda packet: InterfaceMicroservice._known_tlm_packet(self.int_ms, packet)
        self.decom_ms = SimpleNamespace(
            scope="DEFAULT",
            stored_limits_mode="PROCESS",
            error_count=0,
            error=None,
            logger=Logger,
            metric=SimpleNamespace(set=lambda **kw: None),
            target_names=["GMSECDEMO"],
            update_topic_lag=lambda *args, **kwargs: None,
        )

    def pump(self, packet_name):
        packet = self.interface.read()
        InterfaceMicroservice.handle_packet(self.int_ms, packet)
        topic = f"DEFAULT__TELEMETRY__{{GMSECDEMO}}__{packet_name}"
        msg_id, msg_hash = Store.xrange(topic)[-1]
        DecomMicroservice.decom_packet(self.decom_ms, topic, msg_id.decode(), msg_hash, None)
        self.assertEqual(self.decom_ms.error_count, 0, self.decom_ms.error)
        return packet

    def cvt(self, packet_name, item, value_type="CONVERTED"):
        return CvtModel.get_item("GMSECDEMO", packet_name, item, type=value_type, cache_timeout=0, scope="DEFAULT")

    def test_packets_identified_by_apid(self):
        self.publish(tlm_message(health(temp1=21.5, temp2=-3.25, mode=2, seq=42)))
        packet = self.pump("HEALTH")
        self.assertEqual(packet.extra["GMSEC_SUBJECT"], SUBJECT)
        self.assertEqual(self.cvt("HEALTH", "CCSDSSEQCNT"), 42)
        self.assertAlmostEqual(self.cvt("HEALTH", "TEMP1"), 21.5)
        self.assertAlmostEqual(self.cvt("HEALTH", "TEMP2"), -3.25)
        self.assertEqual(self.cvt("HEALTH", "MODE"), "SCIENCE")

        self.publish(tlm_message(ccsds(101, 5, struct.pack(">HH", 28000, 1500))))
        self.pump("POWER")
        self.assertAlmostEqual(self.cvt("POWER", "BUSVOLTAGE"), 28.0)
        self.assertAlmostEqual(self.cvt("POWER", "BUSCURRENT"), 1.5)

    def test_packet_identified_by_subject(self):
        # No ID items at all - the subject alone picks the packet
        self.publish(tlm_message(struct.pack(">H", 7) + b"SAFE MODE ENTERED", subject="GMSEC.DEMO.SAT1.MSG.TLM.EVENT"))
        packet = self.pump("EVENT")
        self.assertEqual(packet.packet_name, "EVENT")
        self.assertEqual(self.cvt("EVENT", "CODE"), 7)
        self.assertEqual(self.cvt("EVENT", "TEXT"), "SAFE MODE ENTERED")


if __name__ == "__main__":
    unittest.main()
