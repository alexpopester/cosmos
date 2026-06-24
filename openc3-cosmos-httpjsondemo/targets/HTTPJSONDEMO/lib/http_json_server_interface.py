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
import queue
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from openc3.accessors.json_accessor import JsonAccessor
from openc3.interfaces.interface import Interface
from openc3.packets.packet import Packet
from openc3.system.system import System
from openc3.utilities.logger import Logger

_OK_BODY = b'{"status":"ok"}'
_OK_HEADERS = [
    ("Content-Type", "application/json"),
    ("Content-Length", str(len(_OK_BODY))),
    ("Connection", "close"),
]


def _error_body(message):
    body = json.dumps({"status": "error", "message": message}).encode("utf-8")
    headers = [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
        ("Connection", "close"),
    ]
    return body, headers


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # suppress default stdout access log
        pass

    def do_POST(self):  # noqa: N802
        # Expect path /targetname/packetname (case-insensitive; mapped to uppercase)
        parts = self.path.split("?")[0].strip("/").split("/")
        if len(parts) != 2:
            body, headers = _error_body(f"Path must be /target/packet, got: {self.path}")
            self.send_response(400)
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
            return

        target_name = parts[0].upper()
        packet_name = parts[1].upper()

        length = int(self.headers.get("content-length", 0))
        data = self.rfile.read(length) if length else b""

        extra = {
            "HTTP_HEADERS": {k.lower(): v for k, v in self.headers.items()},
            "HTTP_REQUEST_TARGET_NAME": target_name,
            "HTTP_REQUEST_PACKET_NAME": packet_name,
        }
        query_string = self.path.partition("?")[2]
        if query_string:
            queries = {}
            for pair in query_string.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    queries[k] = v
            if queries:
                extra["HTTP_QUERIES"] = queries

        self.server.request_queue.put((data, extra))

        self.send_response(200)
        for k, v in _OK_HEADERS:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(_OK_BODY)


class HttpJsonServerInterface(Interface):
    """HTTP server interface that receives inbound JSON POSTs as telemetry.

    Path format: POST /targetname/packetname
    The path segments are uppercased and used to identify the telemetry packet.
    Item names in the packet definition must match the incoming JSON key names.
    JsonAccessor is set automatically at connect time — no ACCESSOR keyword
    needed in telemetry definitions.

    Supported options:
      LISTEN_ADDRESS <ip>  — bind address (default 0.0.0.0)
    """

    def __init__(self, port=80):
        super().__init__()
        self.listen_address = "0.0.0.0"
        self.port = int(port)
        self.server = None
        self._request_queue = queue.Queue()

    def set_option(self, option_name, option_values):
        super().set_option(option_name, option_values)
        if option_name.upper() == "LISTEN_ADDRESS":
            self.listen_address = option_values[0]

    def connection_string(self):
        return f"listening on {self.listen_address}:{self.port}"

    def connect(self):
        # Install JsonAccessor on every telemetry packet for our targets so
        # callers don't need ACCESSOR JsonAccessor in their .txt definitions.
        for target_name in self.target_names:
            try:
                for _packet_name, packet in System.telemetry.packets(target_name).items():
                    packet.accessor = JsonAccessor(packet)
            except Exception:
                Logger.error(
                    f"HttpJsonServerInterface: failed to set accessor for {target_name}\n{traceback.format_exc()}"
                )

        self._request_queue = queue.Queue()
        self.server = ThreadingHTTPServer((self.listen_address, self.port), _Handler)
        self.server.request_queue = self._request_queue
        self._server_thread = Thread(target=self.server.serve_forever, daemon=True)
        self._server_thread.start()
        super().connect()

    def connected(self):
        return self.server is not None

    def disconnect(self):
        if self.server:
            self.server.shutdown()
            self._server_thread.join()
        self.server = None
        super().disconnect()
        self._request_queue.put((None, None))  # unblock read_interface

    def convert_packet_to_data(self, packet):
        raise RuntimeError("Commands cannot be sent to HttpJsonServerInterface")

    def write_interface(self, data, extra=None):
        raise RuntimeError("Commands cannot be sent to HttpJsonServerInterface")

    def read_interface(self):
        data, extra = self._request_queue.get(block=True)
        if data is None:
            return None, None
        self.read_interface_base(data, extra)
        return data, extra

    def convert_data_to_packet(self, data, extra=None):
        packet = Packet(None, None, "BIG_ENDIAN", None, data)
        if extra:
            target_name = extra.pop("HTTP_REQUEST_TARGET_NAME", None)
            packet_name = extra.pop("HTTP_REQUEST_PACKET_NAME", None)
            if target_name and packet_name:
                packet.target_name = target_name
                packet.packet_name = packet_name
            packet.extra = extra
        return packet

    def details(self):
        result = super().details()
        result["listen_address"] = self.listen_address
        result["port"] = self.port
        result["request_queue_length"] = self._request_queue.qsize()
        return result
