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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from openc3.interfaces.http_server_interface import HttpServerInterface
from openc3.system.system import System


class _JsonHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        pass  # suppress access log noise

    def do_POST(self):  # noqa: N802
        self._handle()

    def do_GET(self):  # noqa: N802
        self.send_error_response(405, {"Allow": "POST"})

    def do_PUT(self):  # noqa: N802
        self.send_error_response(405, {"Allow": "POST"})

    def do_DELETE(self):  # noqa: N802
        self.send_error_response(405, {"Allow": "POST"})

    def do_PATCH(self):  # noqa: N802
        self.send_error_response(405, {"Allow": "POST"})

    def send_error_response(self, status, extra_headers=None):
        self.send_response(status)
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle(self):
        api_key = self.server.api_key
        if api_key is not None:
            auth = self.headers.get("Authorization", "")
            token = auth[7:] if auth.startswith("Bearer ") else None
            if token != api_key:
                self.send_response(401)
                self.send_header("WWW-Authenticate", "Bearer")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

        parts = [p for p in self.path.split("?")[0].split("/") if p]
        if len(parts) != 2:
            self.send_error_response(404)
            return

        target_name, packet_name = parts[0].upper(), parts[1].upper()
        try:
            System.telemetry.packet(target_name, packet_name)
        except Exception:
            self.send_error_response(404)
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        try:
            json.loads(body)
        except (json.JSONDecodeError, ValueError):
            self.send_error_response(400)
            return

        extra = {
            "HTTP_REQUEST_TARGET_NAME": target_name,
            "HTTP_REQUEST_PACKET_NAME": packet_name,
        }
        self.server.request_queue.put((body, extra))

        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


class JsonTelemetryServerInterface(HttpServerInterface):
    """HTTP server that accepts POST /<target>/<packet> with a flat JSON body
    and maps JSON keys to packet items by name (case-insensitive). No
    HTTP-specific fields required in packet definitions."""

    def __init__(self, port=8765):
        super().__init__(port)
        self._api_key = None

    def set_option(self, option_name, option_values):
        super().set_option(option_name, option_values)
        if option_name.upper() == "API_KEY":
            self._api_key = option_values[0]

    def connect(self):
        self.server = ThreadingHTTPServer((self.listen_address, self.port), _JsonHandler)
        self.server.request_queue = queue.Queue()
        self.server.api_key = self._api_key
        self.server_thread = Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        # Call Interface.connect() directly (skip HttpServerInterface.connect which
        # tries to build the path lookup and mount routes from command packets)
        from openc3.interfaces.interface import Interface

        Interface.connect(self)

    def convert_data_to_packet(self, data, extra=None):
        from openc3.packets.packet import Packet

        if not extra:
            return Packet(None, None, "BIG_ENDIAN", None, data)

        target_name = extra.get("HTTP_REQUEST_TARGET_NAME")
        packet_name = extra.get("HTTP_REQUEST_PACKET_NAME")

        if not (target_name and packet_name):
            return Packet(None, None, "BIG_ENDIAN", None, data)

        try:
            packet = System.telemetry.packet(target_name, packet_name)
            packet = packet.clone()
        except Exception:
            return Packet(None, None, "BIG_ENDIAN", None, data)

        try:
            json_data = json.loads(data)
        except (json.JSONDecodeError, ValueError):
            return packet

        self._map_json_to_packet(json_data, packet)
        return packet

    def _map_json_to_packet(self, json_data, packet):
        """Write json_data values into packet items by case-insensitive name match."""
        item_lookup = {name.upper(): name for name in packet.items}
        for key, value in json_data.items():
            item_name = item_lookup.get(key.upper())
            if item_name:
                packet.write(item_name, value)
