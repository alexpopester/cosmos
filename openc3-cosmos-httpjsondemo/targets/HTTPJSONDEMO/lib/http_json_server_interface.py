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
from urllib.parse import parse_qsl

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

    def _send(self, status, body, headers, extra_headers=None):
        self.send_response(status)
        if extra_headers:
            for k, v in extra_headers:
                self.send_header(k, v)
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        # 1. Authentication
        api_key = getattr(self.server, "api_key", None)
        if api_key is not None and self.headers.get("x-api-key") != api_key:
            body, headers = _error_body("Unauthorized")
            self._send(401, body, headers)
            return

        # 2. Path — expect /targetname/packetname
        parts = self.path.split("?")[0].strip("/").split("/")
        if len(parts) != 2 or not all(parts):
            body, headers = _error_body(f"Path must be /target/packet, got: {self.path}")
            self._send(400, body, headers)
            return

        target_name = parts[0].upper()
        packet_name = parts[1].upper()

        # 3. Known packet validation
        valid_packets = getattr(self.server, "valid_packets", None)
        if valid_packets is not None and (target_name, packet_name) not in valid_packets:
            body, headers = _error_body(f"Unknown packet: {target_name}/{packet_name}")
            self._send(404, body, headers)
            return

        # 4. Content-Type enforcement
        allowed = getattr(self.server, "allowed_content_types", {"application/json"})
        content_type = self.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type not in allowed:
            body, headers = _error_body(
                f"Unsupported Content-Type '{content_type}', expected one of: {', '.join(sorted(allowed))}"
            )
            self._send(415, body, headers)
            return

        # 5. Content-Length
        try:
            length = int(self.headers.get("content-length", 0))
        except ValueError:
            body, headers = _error_body("Invalid Content-Length header")
            self._send(400, body, headers)
            return
        if length < 0:
            body, headers = _error_body("Negative Content-Length")
            self._send(400, body, headers)
            return
        data = self.rfile.read(length) if length else b""

        # 6. Build extra metadata
        extra = {
            "HTTP_HEADERS": {k.lower(): v for k, v in self.headers.items()},
            "HTTP_REQUEST_TARGET_NAME": target_name,
            "HTTP_REQUEST_PACKET_NAME": packet_name,
        }
        query_string = self.path.partition("?")[2]
        if query_string:
            queries = dict(parse_qsl(query_string))
            if queries:
                extra["HTTP_QUERIES"] = queries

        # 7. Enqueue with backpressure
        try:
            self.server.request_queue.put_nowait((data, extra))
        except queue.Full:
            body, headers = _error_body("Server queue full — retry later")
            self._send(503, body, headers, extra_headers=[("Retry-After", "5")])
            return

        self._send(200, _OK_BODY, _OK_HEADERS)


class HttpJsonServerInterface(Interface):
    """HTTP server interface that receives inbound JSON POSTs as telemetry.

    Path format: POST /targetname/packetname
    The path segments are uppercased and used to identify the telemetry packet.
    Item names in the packet definition must match the incoming JSON key names.
    Telemetry definitions for packets received by this interface must declare
    ACCESSOR JsonAccessor so that all COSMOS microservices parse the buffer correctly.

    Supported options:
      LISTEN_ADDRESS <ip>  — bind address (default 0.0.0.0)
    """

    def __init__(self, port=80):
        super().__init__()
        self.listen_address = "0.0.0.0"
        self.port = int(port)
        self.server = None
        self.api_key = None
        self.max_queue_depth = 1000
        self.allowed_content_types = {"application/json"}
        self._request_queue = queue.Queue()

    def set_option(self, option_name, option_values):
        super().set_option(option_name, option_values)
        match option_name.upper():
            case "LISTEN_ADDRESS":
                self.listen_address = option_values[0]
            case "API_KEY":
                self.api_key = option_values[0]
            case "MAX_QUEUE_DEPTH":
                self.max_queue_depth = int(option_values[0])
            case "ALLOW_CONTENT_TYPE":
                self.allowed_content_types.add(option_values[0].lower())

    def connection_string(self):
        return f"listening on {self.listen_address}:{self.port}"

    def connect(self):
        self._request_queue = queue.Queue(maxsize=self.max_queue_depth)

        self.server = ThreadingHTTPServer((self.listen_address, self.port), _Handler)
        self.server.request_queue = self._request_queue
        self.server.api_key = self.api_key
        self.server.allowed_content_types = self.allowed_content_types

        # Build the valid-packet lookup so the handler can reject unknown paths before 200 OK.
        valid_packets = set()
        for target_name in self.target_names:
            try:
                for packet_name in System.telemetry.packets(target_name):
                    valid_packets.add((target_name.upper(), packet_name.upper()))
            except Exception:
                Logger.error(
                    f"HttpJsonServerInterface: failed to enumerate packets for {target_name}\n{traceback.format_exc()}"
                )
        self.server.valid_packets = valid_packets

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
        # Drain first so the sentinel always fits in a bounded queue.
        while True:
            try:
                self._request_queue.get_nowait()
            except queue.Empty:
                break
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
