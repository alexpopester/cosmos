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

import requests

from openc3.config.config_parser import ConfigParser
from openc3.interfaces.interface import Interface
from openc3.packets.packet import Packet


class HttpJsonClientInterface(Interface):
    """
    HttpJsonClientInterface makes HTTP requests where packet fields are
    serialized to a flat JSON body. The response is routed to a configured
    response or error packet based on the HTTP status code.
    """

    def __init__(
        self,
        hostname,
        port=80,
        protocol="http",
        write_timeout=5,
        read_timeout=None,
        connect_timeout=5,
    ):
        """
        Initializes the HttpJsonClientInterface.

        Args:
            hostname (str): The hostname of the server.
            port (int, optional): The port number to connect to. Defaults to 80.
            protocol (str, optional): The protocol to use ('http' or 'https'). Defaults to "http".
            write_timeout (float or None, optional): Write timeout in seconds. Defaults to 5.
            read_timeout (float or None, optional): Read timeout in seconds. Defaults to None.
            connect_timeout (float or None, optional): Connect timeout in seconds. Defaults to 5.
        """
        super().__init__()
        self.hostname = hostname
        self.port = int(port)
        self.protocol = protocol
        if (self.port == 80 and self.protocol == "http") or (self.port == 443 and self.protocol == "https"):
            self.url = f"{self.protocol}://{self.hostname}"
        else:
            self.url = f"{self.protocol}://{self.hostname}:{self.port}"

        self.write_timeout = ConfigParser.handle_none(write_timeout)
        if self.write_timeout is not None:
            self.write_timeout = float(self.write_timeout)
        self.read_timeout = ConfigParser.handle_none(read_timeout)
        if self.read_timeout is not None:
            self.read_timeout = float(self.read_timeout)
        self.connect_timeout = ConfigParser.handle_none(connect_timeout)
        if self.connect_timeout is not None:
            self.connect_timeout = float(self.connect_timeout)

        self.http = None
        self.response_queue = queue.Queue()

        # Interface-level configuration
        self.headers = {}
        self.path = None
        self.http_method = None
        self.response_target_name = None
        self.response_packet_name = None
        self.error_target_name = None
        self.error_packet_name = None

    def connection_string(self):
        """Returns the URL."""
        return self.url

    def set_option(self, option_name, option_values):
        """
        Set an interface-specific option.

        Args:
            option_name (str): Name of the option.
            option_values (list): Option values.
        """
        option_name_upper = option_name.upper()
        if option_name_upper == "PATH":
            self.path = option_values[0]
        elif option_name_upper == "METHOD":
            self.http_method = option_values[0]
        elif option_name_upper == "HEADER":
            self.headers[option_values[0]] = option_values[1]
        elif option_name_upper == "RESPONSE_PACKET":
            self.response_target_name = option_values[0]
            self.response_packet_name = option_values[1]
        elif option_name_upper == "ERROR_PACKET":
            self.error_target_name = option_values[0]
            self.error_packet_name = option_values[1]
        else:
            super().set_option(option_name, option_values)

    def connect(self):
        """Initializes an HTTP session and calls the parent connect method."""
        self.http = requests.Session()
        super().connect()

    def connected(self):
        """Returns True if the HTTP client is connected."""
        return bool(self.http)

    def disconnect(self):
        """Disconnects the HTTP client interface."""
        if self.http:
            self.http.close()
        self.http = None
        while not self.response_queue.empty():
            self.response_queue.get_nowait()
        super().disconnect()
        self.response_queue.put((None, None))

    def convert_packet_to_data(self, packet):
        """
        Converts a packet to a flat JSON string and builds HTTP metadata extra.

        Args:
            packet: The packet to be converted.

        Returns:
            tuple: (json_string, extra) where json_string is the JSON-encoded
                   packet fields and extra contains HTTP_METHOD, HTTP_HEADERS,
                   and HTTP_URI.
        """
        # Build flat dict from all packet fields, skipping COSMOS internal reserved items
        fields = {}
        for item in packet.sorted_items:
            if item.name not in Packet.RESERVED_ITEM_NAMES:
                fields[item.name] = packet.read(item.name)
        json_string = json.dumps(fields)

        # Build extra with HTTP metadata from interface-level config
        extra = {}
        extra["HTTP_METHOD"] = self.http_method
        extra["HTTP_HEADERS"] = dict(self.headers)
        extra["HTTP_URI"] = f"{self.url}{self.path}"

        return json_string, extra

    def convert_data_to_packet(self, data, extra=None):
        """
        Converts response data into a Packet object.

        Routes to RESPONSE_PACKET or ERROR_PACKET based on the HTTP status code.

        Args:
            data (str): Raw JSON response data.
            extra (dict, optional): Contains HTTP_STATUS and other metadata.

        Returns:
            Packet: OpenC3 Packet with buffer set to the raw response data.
        """
        if isinstance(data, str):
            data = data.encode("utf-8")
        packet = Packet(None, None, "BIG_ENDIAN", None, data)

        # Route based on HTTP status code and stored packet config
        status = int(extra["HTTP_STATUS"]) if extra and "HTTP_STATUS" in extra else 0
        if status >= 300 and self.error_packet_name is not None:
            packet.target_name = self.error_target_name
            packet.packet_name = self.error_packet_name
        else:
            packet.target_name = self.response_target_name
            packet.packet_name = self.response_packet_name

        packet.extra = extra
        return packet

    def write_interface(self, data, extra=None):
        """Sends data to the target using an HTTP request."""
        extra = extra or {}
        params = extra.get("HTTP_QUERIES")
        headers = extra.get("HTTP_HEADERS") or {}
        uri = extra["HTTP_URI"]
        method = extra["HTTP_METHOD"]

        resp = self.http.request(
            method,
            uri,
            params=params,
            headers=headers,
            data=data,
            stream=False,
            timeout=(self.connect_timeout, self.read_timeout),
        )

        response_data = None
        response_extra = {}
        if resp.headers and len(resp.headers) > 0:
            response_extra["HTTP_HEADERS"] = dict(resp.headers)
        response_extra["HTTP_STATUS"] = resp.status_code
        response_data = bytearray(resp.text, encoding="utf-8")

        self.response_queue.put((response_data, response_extra))
        self.write_interface_base(data, extra)
        return data, extra

    def read_interface(self):
        """Returns response data queued by write_interface."""
        data, extra = self.response_queue.get(block=True)
        if data is None:
            return data, extra
        self.read_interface_base(data, extra)
        return data, extra
