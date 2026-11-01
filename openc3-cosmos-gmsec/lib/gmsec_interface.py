# GMSEC telemetry interface for OpenC3 COSMOS.
#
# Subscribes to one or more GMSEC subjects using the GMSEC API 4.9.1 Python3
# binding (libgmsec_python3), pulls the raw packet bytes out of a BIN field of
# each received message (COMPAT C2 / C2MS style TLM messages carry them in
# DATA), and feeds those bytes into the normal COSMOS telemetry pipeline.
#
# The bytes are identified the usual way: by the target's ID items, or
# directly by subject when a packet declares META GMSEC_SUBJECT <subject>.
#
# Usage in plugin.txt:
#   INTERFACE GMSEC_INT gmsec_interface.py <mw-id> <server> <subject> [<subject> ...]
#     MAP_TLM_TARGET MYTARGET
#     ENV GMSEC_API_HOME /opt/GMSEC_API
#     ENV LD_LIBRARY_PATH /opt/GMSEC_API/bin
#
# Supported OPTIONs (see set_option):
#   DATA_FIELD <name>          BIN field holding the raw packet (default DATA,
#                              AUTO = first BIN field in the message)
#   CONFIG <key> <value>       any extra GMSEC connection config entry,
#                              e.g. CONFIG gmsec-schema-level 0
#   SUBSCRIBE <subject>        additional subject to subscribe to
#   FILTER <field> <value>     only accept messages whose field equals value
#                              (repeatable, all must match)
#   EXTRA_FIELD <field>        copy a message field into the packet's extra
#                              data (repeatable)
#   RECEIVE_TIMEOUT <seconds>  receive poll period (default 1.0), bounds how
#                              long a disconnect waits for the read thread
#   GMSEC_API_HOME <path>      GMSEC install root, if not set via ENV
#   MW_USERNAME / MW_PASSWORD  broker credentials, typically via
#                              SECRET ENV <secret> <env var> MW_PASSWORD

import ctypes
import glob
import importlib
import os
import sys
import threading

from openc3.config.config_parser import ConfigParser
from openc3.interfaces.interface import Interface
from openc3.system.system import System
from openc3.utilities.logger import Logger


# The binding is loaded on first connect so the plugin installs (and the
# tests run) on machines without GMSEC
gmsec = None


def load_gmsec(gmsec_home=None, mw_id=None):
    global gmsec
    if gmsec is not None:
        return gmsec
    gmsec_home = gmsec_home or os.environ.get("GMSEC_API_HOME")
    if gmsec_home:
        bin_dir = os.path.join(gmsec_home, "bin")
        for path in (os.path.join(bin_dir, "lib", "GMSECAPI4"), bin_dir):
            if os.path.isdir(path) and path not in sys.path:
                sys.path.insert(0, path)
        # LD_LIBRARY_PATH is only read at process start, so preload the core
        # library and the middleware wrapper. GMSEC dlopen()s the wrapper by
        # bare soname, which glibc resolves to the already loaded copy.
        libs = [os.path.join(bin_dir, "libGMSECAPI.so")]
        if mw_id:
            libs.append(os.path.join(bin_dir, f"libgmsec_{mw_id}.so"))
        for lib in libs:
            for match in glob.glob(lib):
                try:
                    ctypes.CDLL(match, mode=ctypes.RTLD_GLOBAL)
                except OSError as error:
                    Logger.warn(f"GMSEC: could not preload {match}: {error}")
    try:
        gmsec = importlib.import_module("libgmsec_python3")
    except ImportError as error:
        raise RuntimeError(
            "GMSEC API Python3 binding (libgmsec_python3) not found. Set GMSEC_API_HOME "
            "to a GMSEC API 4.9.1 install whose binding is built for this Python "
            f"({sys.version.split()[0]}): {error}"
        ) from error
    return gmsec


class GmsecInterface(Interface):
    # @param mw_id [String] GMSEC middleware wrapper id, e.g. bolt, mb, activemq395, amqp
    # @param server [String] Middleware server/broker address, or None
    # @param subjects [Array<String>] Subjects to subscribe to (wildcards allowed)
    def __init__(self, mw_id, server=None, *subjects):
        super().__init__()
        self.mw_id = mw_id
        self.server = ConfigParser.handle_none(server)
        self.subjects = list(subjects)
        self.data_field = "DATA"
        self.config_entries = {}
        self.filters = {}
        self.extra_fields = []
        self.receive_timeout = 1.0
        self.gmsec_home = None
        self.write_allowed = False
        self.write_raw_allowed = False

        self.conn_mgr = None
        self.subscriptions = []
        self.stopping = False
        # Held across receive() so disconnect never cleans up the connection
        # out from under the read thread
        self.conn_lock = threading.Lock()

        # Packets that claim a subject are identified without their ID items
        self.read_packets_by_subject = {}
        for _, target_packets in System.telemetry.all().items():
            for _, packet in target_packets.items():
                for subject in packet.meta.get("GMSEC_SUBJECT", []):
                    self.read_packets_by_subject[subject] = packet

    def connection_string(self):
        return f"GMSEC {self.mw_id} {self.server or ''} {' '.join(self.subjects)}".strip()

    def build_config(self):
        config = gmsec.Config()
        config.add_value("mw-id", self.mw_id)
        if self.server:
            config.add_value("server", self.server)
        for key, value in self.config_entries.items():
            config.add_value(key, value)
        return config

    def connect(self):
        load_gmsec(self.gmsec_home, self.mw_id)
        if not self.subjects:
            raise RuntimeError("GmsecInterface requires at least one subject to subscribe to")
        conn_mgr = gmsec.ConnectionManager(self.build_config())
        try:
            conn_mgr.initialize()
            self.subscriptions = [conn_mgr.subscribe(subject) for subject in self.subjects]
        except Exception:
            self._cleanup(conn_mgr)
            raise
        Logger.info(f"{self.name}: GMSEC connected ({conn_mgr.get_library_version()}), subscribed to {self.subjects}")
        self.stopping = False
        self.conn_mgr = conn_mgr
        super().connect()

    def connected(self):
        return self.conn_mgr is not None

    def disconnect(self):
        self.stopping = True
        with self.conn_lock:
            conn_mgr = self.conn_mgr
            self.conn_mgr = None
            if conn_mgr is not None:
                self._cleanup(conn_mgr)
        super().disconnect()

    def _cleanup(self, conn_mgr):
        try:
            for info in self.subscriptions:
                conn_mgr.unsubscribe(info)
        except Exception as error:
            Logger.warn(f"{self.name}: GMSEC unsubscribe failed: {error}")
        self.subscriptions = []
        try:
            conn_mgr.cleanup()
        except Exception as error:
            Logger.warn(f"{self.name}: GMSEC cleanup failed: {error}")

    def read_interface(self):
        timeout_ms = int(self.receive_timeout * 1000)
        while not self.stopping:
            with self.conn_lock:
                conn_mgr = self.conn_mgr
                if conn_mgr is None:
                    break
                message = conn_mgr.receive(timeout_ms)
                if message is None:
                    continue
                try:
                    data, extra = self.extract(message)
                finally:
                    conn_mgr.release(message)
            if data is not None:
                self.read_interface_base(data, extra)
                return (data, extra)
        # None tells COSMOS the interface was disconnected
        return (None, None)

    # Pull the raw packet bytes and any requested fields out of a message.
    # Returns (None, None) for messages that should be skipped.
    def extract(self, message):
        subject = message.get_subject()
        for field_name, value in self.filters.items():
            if not message.has_field(field_name) or message.get_string_value(field_name) != value:
                return (None, None)

        field = self.find_data_field(message)
        if field is None:
            Logger.warn(f"{self.name}: GMSEC message on {subject} has no BIN field '{self.data_field}', skipped")
            return (None, None)
        data = bytes(field.get_value())

        extra = {"GMSEC_SUBJECT": subject}
        for field_name in self.extra_fields:
            if message.has_field(field_name):
                extra[field_name] = message.get_string_value(field_name)
        return (data, extra)

    def find_data_field(self, message):
        if self.data_field != "AUTO":
            if message.has_field(self.data_field) and message.get_field_type(self.data_field) == gmsec.Field.BIN_TYPE:
                return message.get_binary_field(self.data_field)
            return None
        iterator = message.get_field_iterator()
        while iterator.has_next():
            field = iterator.next()
            if field.get_type() == gmsec.Field.BIN_TYPE:
                return gmsec.to_BinaryField(field)
        return None

    def convert_data_to_packet(self, data, extra):
        packet = self.read_packets_by_subject.get((extra or {}).get("GMSEC_SUBJECT"))
        if packet is None:
            return super().convert_data_to_packet(data, extra)
        packet = packet.clone()
        packet.buffer = data
        packet.extra = extra
        return packet

    def write_interface(self, data, extra=None):
        raise RuntimeError(f"{self.name}: GmsecInterface is telemetry only")

    def set_option(self, option_name, option_values):
        super().set_option(option_name, option_values)
        match option_name.upper():
            case "DATA_FIELD":
                self.data_field = option_values[0]
            case "CONFIG":
                self.config_entries[option_values[0]] = " ".join(str(value) for value in option_values[1:])
            case "SUBSCRIBE":
                self.subjects.extend(option_values)
            case "FILTER":
                self.filters[option_values[0]] = " ".join(str(value) for value in option_values[1:])
            case "EXTRA_FIELD":
                self.extra_fields.extend(option_values)
            case "RECEIVE_TIMEOUT":
                self.receive_timeout = float(option_values[0])
            case "GMSEC_API_HOME":
                self.gmsec_home = option_values[0]
            # Separate options so SECRET ENV ... MW_PASSWORD can supply them
            case "MW_USERNAME":
                self.config_entries["mw-username"] = option_values[0]
            case "MW_PASSWORD":
                self.config_entries["mw-password"] = option_values[0]
