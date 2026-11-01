# Runs the real GmsecInterface (real libgmsec_python3 + ActiveMQ) inside the
# GMSEC image and decodes what it receives with the GMSECDEMO definitions.
# Driven by run_activemq_e2e.sh.
import sys


sys.path.insert(0, "/plugin/lib")
from gmsec_interface import GmsecInterface

from openc3.system.system import System
from openc3.utilities.logger import Logger


Logger.stdout = True
System.instance(["GMSECDEMO"], "/plugin/targets")
interface = GmsecInterface("activemq39", "tcp://activemq:61616", "GMSEC.DEMO.SAT1.MSG.TLM.>")
interface.name = "GMSEC_INT"
interface.set_option("EXTRA_FIELD", ["STREAM-MODE", "COLLECTION-POINT"])
interface.connect()
print("CONNECTED", interface.connected(), flush=True)
seen = set()
for _ in range(25):
    packet = interface.read()
    if packet.packet_name is None:
        packet = System.telemetry.identify_and_define_packet(packet, ["GMSECDEMO"])
    packet.process()
    seen.add(packet.packet_name)
    if packet.packet_name == "HEALTH":
        print("HEALTH", packet.read("CCSDSSEQCNT"), packet.read("TEMP1"), packet.read("MODE"), packet.extra, flush=True)
    elif packet.packet_name == "POWER":
        print("POWER", packet.read("BUSVOLTAGE"), packet.read("BUSCURRENT"), flush=True)
    else:
        print(packet.packet_name, packet.read("CODE"), packet.read("TEXT"), flush=True)
    if seen >= {"HEALTH", "POWER", "EVENT"}:
        break
interface.disconnect()
print("SEEN", sorted(seen), "CONNECTED", interface.connected(), flush=True)
