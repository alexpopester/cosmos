# Publishes GMSECDEMO telemetry as COMPAT C2 / C2MS style TLM messages with the
# raw CCSDS packet in a DATA BIN field, using the real GMSEC API 4.9.1 binding.
#
#   export GMSEC_API_HOME=/opt/GMSEC_API
#   export PYTHONPATH=$GMSEC_API_HOME/bin:$GMSEC_API_HOME/bin/lib/GMSECAPI4
#   export LD_LIBRARY_PATH=$GMSEC_API_HOME/bin
#   python3 publish_demo_tlm.py mw-id=bolt server=localhost:9100 [rate=1] [count=0]
#
# For a quick broker: $GMSEC_API_HOME/bin/bolt.jar (java -jar bolt.jar)

import math
import struct
import sys
import time

import libgmsec_python3 as gmsec


SUBJECT = "GMSEC.DEMO.SAT1.MSG.TLM.CCSDSPKT"
EVENT_SUBJECT = "GMSEC.DEMO.SAT1.MSG.TLM.EVENT"


def ccsds(apid, seq, payload):
    return struct.pack(">HHH", apid, 0xC000 | (seq & 0x3FFF), len(payload) - 1) + payload


def tlm_message(subject, data):
    msg = gmsec.Message(subject, gmsec.Message.PUBLISH)
    msg.add_field(gmsec.F32Field("HEADER-VERSION", 2010.0))
    msg.add_field(gmsec.StringField("MESSAGE-TYPE", "MSG"))
    msg.add_field(gmsec.StringField("MESSAGE-SUBTYPE", "TLM"))
    msg.add_field(gmsec.StringField("FORMAT", "CCSDSPKT"))
    msg.add_field(gmsec.StringField("STREAM-MODE", "RT"))
    msg.add_field(gmsec.StringField("COLLECTION-POINT", "DEMO"))
    msg.add_field(gmsec.I32Field("LENGTH", len(data)))
    msg.add_field(gmsec.BinaryField("DATA", bytearray(data), len(data)))
    return msg


def main():
    config = gmsec.Config(sys.argv)
    config.add_value("gmsec-msg-content-validate-send", "false")
    rate = float(config.get_value("rate", "1"))
    count = int(config.get_value("count", "0"))

    conn_mgr = gmsec.ConnectionManager(config)
    conn_mgr.initialize()
    print(conn_mgr.get_library_version())
    seq = 0
    try:
        while count == 0 or seq < count:
            t = seq / 10
            health = struct.pack(">hhB", round(2000 + 500 * math.sin(t)), round(-300 + 50 * math.cos(t)), seq // 20 % 3)
            power = struct.pack(">HH", round(28000 + 1000 * math.sin(t / 3)), round(1500 + 200 * math.cos(t)))
            conn_mgr.publish(tlm_message(SUBJECT, ccsds(100, seq, health)))
            conn_mgr.publish(tlm_message(SUBJECT, ccsds(101, seq, power)))
            if seq % 10 == 0:
                conn_mgr.publish(tlm_message(EVENT_SUBJECT, struct.pack(">H", seq) + f"EVENT {seq}".encode()))
            seq += 1
            time.sleep(1 / rate)
    except KeyboardInterrupt:
        pass
    finally:
        conn_mgr.cleanup()


if __name__ == "__main__":
    main()
