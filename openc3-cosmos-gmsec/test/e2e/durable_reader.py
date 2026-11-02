# Runs the real GmsecInterface inside the GMSEC image, connects, reads until
# the bus has been quiet for a few seconds, then disconnects and prints how many
# packets arrived and their HEALTH sequence counts. Driven by run_durable_e2e.sh.
#
#   durable_reader.py <durable name, or none>
import sys
import threading
import time


sys.path.insert(0, "/plugin/lib")
from gmsec_interface import GmsecInterface

from openc3.system.system import System
from openc3.utilities.logger import Logger


QUIET_SECONDS = 4

Logger.stdout = True
System.instance(["GMSECDEMO"], "/plugin/targets")
interface = GmsecInterface("activemq39", "tcp://activemq:61616", "GMSEC.DEMO.SAT1.MSG.TLM.>")
interface.name = "GMSEC_INT"
if sys.argv[1] != "none":
    interface.set_option("DURABLE", [sys.argv[1]])
interface.set_option("RECEIVE_TIMEOUT", ["0.2"])
interface.connect()
print("CONNECTED", interface.connected(), flush=True)

received = []
last = time.time()


def read_loop():
    global last
    while True:
        packet = interface.read()
        if packet is None:
            return
        if packet.packet_name is None:
            packet = System.telemetry.identify_and_define_packet(packet, ["GMSECDEMO"])
        received.append((packet.packet_name, packet.read("CCSDSSEQCNT") if packet.packet_name == "HEALTH" else None))
        last = time.time()


thread = threading.Thread(target=read_loop, daemon=True)
thread.start()
while time.time() - last < QUIET_SECONDS:
    time.sleep(0.2)
interface.disconnect()
thread.join(5)

seqs = [seq for name, seq in received if name == "HEALTH"]
print("RECEIVED", len(received), flush=True)
print("HEALTH_SEQ", seqs, flush=True)
print("NAMES", sorted({str(name) for name, _ in received}), [name for name, _ in received].count("EVENT"), flush=True)
