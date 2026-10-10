# REST interface that accepts telemetry as JSON and feeds it into the normal
# COSMOS telemetry pipeline, using the targets' existing packet definitions.
#
#   INTERFACE JSON_TLM_INT json_tlm_server_interface.py <port> [route_prefix]
#     MAP_TLM_TARGET INST
#     SECRET ENV JSON_TLM_TOKEN JSON_TLM_TOKEN AUTH_TOKEN
#     PORT <port>
#     ROUTE_PREFIX /json-tlm
#
# Endpoints (paths are relative to route_prefix):
#   GET  /health               liveness + queue depth + mapped targets
#   GET  /tlm/<TGT>/<PKT>      item list for a packet (names, types, states, conversions)
#   POST /tlm/<TGT>/<PKT>      one packet:   {"items": {...}, "type": "RAW", ...}
#   POST /tlm                  batch:        {"packets": [{"target":..,"packet":..,"items":{..}}, ...]}
#
# Options:
#   AUTH_MODE TOKEN|OIDC            TOKEN (default) = static shared secret.
#                                   OIDC = validate Keycloak JWTs (Enterprise).
#
#   TOKEN mode:
#     AUTH_TOKEN <token>            Required "Authorization: Bearer <token>" (or X-Api-Key header)
#     ALLOW_UNAUTHENTICATED TRUE    Run without a token (local testing only)
#
#   OIDC mode (external service posts a Keycloak Bearer JWT):
#     OIDC_REQUIRED_ROLE <role>     Realm role the token must carry to write telemetry
#     OIDC_REQUIRED_SCOPE <scope>   OAuth scope the token must carry (alternative/addition to role)
#     OIDC_AUDIENCE <aud>           Expected audience (default: env OPENC3_API_CLIENT, "api")
#     OIDC_ISSUER <url>             Override issuer (default: OPENC3_KEYCLOAK_URL/realms/OPENC3_KEYCLOAK_REALM)
#     OIDC_JWKS_URL <url>           Override JWKS url (default: <issuer>/protocol/openid-connect/certs)
#     OIDC_LEEWAY <seconds>         Allowed clock skew for exp/nbf (default 30)
#
#   LISTEN_ADDRESS <ip>             Default 0.0.0.0
#   DEFAULT_TYPE RAW|CONVERTED      Value type when the body has no "type" (default RAW)
#   MISSING_ITEMS DEFAULTS|LAST     What unsent items contain (default DEFAULTS)
#   STRICT_CONVERTED TRUE|FALSE     Reject CONVERTED writes that would double-convert (default TRUE)
#   MAX_BODY_BYTES <n>              Default 1048576
#   MAX_QUEUE <n>                   Backpressure limit, returns 503 when full (default 10000)

import hmac
import json
import os
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from json_oidc_auth import OidcValidator
from json_packet_protocol import (
    MISSING_ITEM_MODES,
    VALUE_TYPES,
    JsonTlmError,
    build_packet_from_json,
)

from openc3.config.config_parser import ConfigParser
from openc3.interfaces.interface import Interface
from openc3.packets.packet import Packet
from openc3.system.system import System
from openc3.utilities.logger import Logger


AUTH_MODES = ("TOKEN", "OIDC")

META_KEY = "_JSON_TLM_META"


class _Handler(BaseHTTPRequestHandler):
    server_version = "OpenC3JsonTlm/1.0"
    interface = None  # Set on the generated subclass

    def log_message(self, format, *args):  # noqa: A002 - silence per-request stderr logging
        pass

    def _send(self, status, payload):
        body = json.dumps(payload, allow_nan=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self):
        path = self.path.split("?", 1)[0]
        prefix = self.interface.route_prefix
        if prefix and (path == prefix or path.startswith(prefix + "/")):
            path = path[len(prefix):]
        return [p for p in path.split("/") if p]

    def _authorized(self):
        if self.interface.auth_mode == "OIDC":
            auth = self.headers.get("Authorization", "")
            if not auth.lower().startswith("bearer "):
                return False
            return self.interface.oidc.valid(auth[7:].strip())
        token = self.interface.auth_token
        if not token:
            return self.interface.allow_unauthenticated
        given = self.headers.get("X-Api-Key")
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            given = auth[7:].strip()
        return given is not None and hmac.compare_digest(given.encode(), token.encode())

    def do_GET(self):  # noqa: N802
        parts = self._route()
        if parts == ["health"]:
            return self._send(200, self.interface.health())
        if not self._authorized():
            return self._send(401, {"error": "Unauthorized"})
        if len(parts) == 3 and parts[0] == "tlm":
            try:
                return self._send(200, self.interface.packet_schema(parts[1], parts[2]))
            except JsonTlmError as error:
                return self._send(error.status, {"error": str(error)})
        return self._send(404, {"error": "Not found"})

    def do_POST(self):  # noqa: N802
        parts = self._route()
        if not parts or parts[0] != "tlm" or len(parts) not in (1, 3):
            return self._send(404, {"error": "Not found"})
        if not self._authorized():
            return self._send(401, {"error": "Unauthorized"})

        length = int(self.headers.get("Content-Length") or 0)
        if length > self.interface.max_body_bytes:
            return self._send(413, {"error": f"Body larger than {self.interface.max_body_bytes} bytes"})
        try:
            body = json.loads(self.rfile.read(length) or b"null")
        except ValueError as error:
            return self._send(400, {"error": f"Invalid JSON: {error}"})

        if len(parts) == 3:
            entries = [(body, parts[1], parts[2])]
        else:
            packets = body.get("packets") if isinstance(body, dict) else body
            if not isinstance(packets, list) or not packets:
                return self._send(400, {"error": 'Batch body must be {"packets": [...]} or a JSON array'})
            entries = [(entry, None, None) for entry in packets]

        try:
            accepted = self.interface.accept(entries)
        except JsonTlmError as error:
            return self._send(error.status, {"error": str(error)})
        return self._send(202, {"accepted": accepted})


class _TunedThreadingHTTPServer(ThreadingHTTPServer):
    # socketserver's default listen backlog is only 5, so a burst of new
    # connections beyond that is refused by the kernel and surfaces as Traefik
    # 502s. Raise it so connection storms queue in the OS accept backlog instead
    # of failing. This does NOT raise per-core throughput (the handler is still
    # one GIL-bound process) - it only smooths out bursty connects. The OS caps
    # the effective value at net.core.somaxconn.
    request_queue_size = 128
    daemon_threads = True


class JsonTlmServerInterface(Interface):
    def __init__(self, port=8080, route_prefix=None):
        super().__init__()
        self.port = int(port)
        prefix = ConfigParser.handle_none(route_prefix)
        self.route_prefix = "/" + prefix.strip("/") if prefix else ""
        self.listen_address = "0.0.0.0"
        self.auth_mode = "TOKEN"
        self.auth_token = None
        self.allow_unauthenticated = False
        # OIDC (set via OPTION in OIDC mode; resolved against env in connect())
        self.oidc = None
        self.oidc_required_role = None
        self.oidc_required_scope = None
        self.oidc_audience = None
        self.oidc_issuer = None
        self.oidc_jwks_url = None
        self.oidc_leeway = 30
        self.default_value_type = "RAW"
        self.missing_items = "DEFAULTS"
        self.strict_converted = True
        self.max_body_bytes = 1_048_576
        self.max_queue = 10_000
        self.server = None
        self.server_thread = None
        self.request_queue = queue.Queue()
        self.rejected_count = 0
        # Commands are not supported, this interface is telemetry only
        self.write_allowed = False
        self.write_raw_allowed = False

    def set_option(self, option_name, option_values):
        super().set_option(option_name, option_values)
        name = option_name.upper()
        value = option_values[0] if option_values else None
        if name == "LISTEN_ADDRESS":
            self.listen_address = value
        elif name == "AUTH_MODE":
            if str(value).upper() not in AUTH_MODES:
                raise ValueError(f"AUTH_MODE must be one of {AUTH_MODES}")
            self.auth_mode = str(value).upper()
        elif name == "AUTH_TOKEN":
            self.auth_token = value
        elif name == "ALLOW_UNAUTHENTICATED":
            self.allow_unauthenticated = ConfigParser.handle_true_false(value)
        elif name == "OIDC_REQUIRED_ROLE":
            self.oidc_required_role = value
        elif name == "OIDC_REQUIRED_SCOPE":
            self.oidc_required_scope = value
        elif name == "OIDC_AUDIENCE":
            self.oidc_audience = value
        elif name == "OIDC_ISSUER":
            self.oidc_issuer = value
        elif name == "OIDC_JWKS_URL":
            self.oidc_jwks_url = value
        elif name == "OIDC_LEEWAY":
            self.oidc_leeway = int(value)
        elif name == "DEFAULT_TYPE":
            if str(value).upper() not in VALUE_TYPES:
                raise ValueError(f"DEFAULT_TYPE must be one of {VALUE_TYPES}")
            self.default_value_type = str(value).upper()
        elif name == "MISSING_ITEMS":
            if str(value).upper() not in MISSING_ITEM_MODES:
                raise ValueError(f"MISSING_ITEMS must be one of {MISSING_ITEM_MODES}")
            self.missing_items = str(value).upper()
        elif name == "STRICT_CONVERTED":
            self.strict_converted = ConfigParser.handle_true_false(value)
        elif name == "MAX_BODY_BYTES":
            self.max_body_bytes = int(value)
        elif name == "MAX_QUEUE":
            self.max_queue = int(value)

    def connection_string(self):
        return f"listening on {self.listen_address}:{self.port}{self.route_prefix}"

    # Build the OIDC validator from the OPTIONs and COSMOS's standard Keycloak
    # env vars. Called from connect() so a bad config (missing Keycloak url, or
    # PyJWT not installed) fails the interface loudly instead of 401ing silently.
    def _setup_oidc(self):
        if self.oidc is None:
            if self.oidc_issuer:
                issuer = self.oidc_issuer
            else:
                base = os.environ.get("OPENC3_KEYCLOAK_URL")
                if not base:
                    raise RuntimeError(
                        f"{self.name}: AUTH_MODE OIDC needs OPENC3_KEYCLOAK_URL "
                        "(set in COSMOS Enterprise) or OPTION OIDC_ISSUER"
                    )
                realm = os.environ.get("OPENC3_KEYCLOAK_REALM", "openc3")
                issuer = f"{base.rstrip('/')}/realms/{realm}"
            jwks_url = self.oidc_jwks_url or f"{issuer}/protocol/openid-connect/certs"
            audience = self.oidc_audience or os.environ.get("OPENC3_API_CLIENT", "api")
            if not self.oidc_required_role and not self.oidc_required_scope:
                Logger.warn(
                    f"{self.name}: OIDC with no OIDC_REQUIRED_ROLE/OIDC_REQUIRED_SCOPE - "
                    "any valid realm token can write telemetry"
                )
            self.oidc = OidcValidator(
                issuer=issuer,
                jwks_url=jwks_url,
                audience=audience,
                required_role=self.oidc_required_role,
                required_scope=self.oidc_required_scope,
                leeway=self.oidc_leeway,
            )
        self.oidc.preflight()
        Logger.info(f"{self.name}: OIDC enabled, issuer {self.oidc.issuer}, audience {self.oidc.audience}")

    def connect(self):
        if self.auth_mode == "OIDC":
            self._setup_oidc()
        else:
            if not self.auth_token and not self.allow_unauthenticated:
                raise RuntimeError(
                    f"{self.name}: set OPTION AUTH_TOKEN (or SECRET ... AUTH_TOKEN), "
                    "or OPTION ALLOW_UNAUTHENTICATED TRUE for local testing"
                )
            if not self.auth_token:
                Logger.warn(f"{self.name}: running WITHOUT authentication")
        self.request_queue = queue.Queue()
        handler = type("JsonTlmHandler", (_Handler,), {"interface": self})
        self.server = _TunedThreadingHTTPServer((self.listen_address, self.port), handler)
        self.server_thread = Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        super().connect()

    def connected(self):
        return self.server is not None

    def disconnect(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server_thread.join()
        self.server = None
        self.request_queue.put((None, None))  # Unblock read_interface
        super().disconnect()

    # Called from HTTP threads. Validates and builds every packet before queuing any,
    # so a batch is all-or-nothing.
    def accept(self, entries):
        if not self.connected():
            raise JsonTlmError("Interface not connected", status=503)
        built = []
        for index, (body, target_name, packet_name) in enumerate(entries):
            try:
                packet = build_packet_from_json(
                    body,
                    target_name=target_name,
                    packet_name=packet_name,
                    allowed_targets=self.tlm_target_names,
                    default_value_type=self.default_value_type,
                    missing_items=self.missing_items,
                    strict_converted=self.strict_converted,
                )
            except JsonTlmError as error:
                self.rejected_count += 1
                where = f"packets[{index}]: " if len(entries) > 1 else ""
                raise JsonTlmError(f"{where}{error}", status=error.status) from error
            if not self.tlm_target_enabled.get(packet.target_name, True):
                self.rejected_count += 1
                raise JsonTlmError(f"Target {packet.target_name} is disabled on {self.name}", status=409)
            built.append(packet)

        if self.request_queue.qsize() + len(built) > self.max_queue:
            self.rejected_count += len(built)
            raise JsonTlmError("Queue full, retry later", status=503)
        for packet in built:
            meta = {
                "target_name": packet.target_name,
                "packet_name": packet.packet_name,
                "received_time": packet.received_time,
                "stored": packet.stored,
            }
            self.request_queue.put((packet.buffer, {META_KEY: meta}))
        return len(built)

    def read_interface(self):
        data, extra = self.request_queue.get(block=True)
        if data is None:
            return None, None
        self.read_interface_base(data, extra)
        return data, extra

    def convert_data_to_packet(self, data, extra=None):
        meta = extra.pop(META_KEY) if extra else {}
        packet = Packet(meta.get("target_name"), meta.get("packet_name"), "BIG_ENDIAN", None, data)
        packet.received_time = meta.get("received_time")
        packet.stored = meta.get("stored", False)
        packet.extra = extra or None
        return packet

    def write_interface(self, data, extra=None):
        raise RuntimeError("Commands cannot be sent to JsonTlmServerInterface")

    def convert_packet_to_data(self, packet):
        raise RuntimeError("Commands cannot be sent to JsonTlmServerInterface")

    def health(self):
        return {
            "status": "ok" if self.connected() else "disconnected",
            "interface": self.name,
            "queue": self.request_queue.qsize(),
            "targets": self.tlm_target_names,
        }

    def packet_schema(self, target_name, packet_name):
        target_name = target_name.upper()
        packet_name = packet_name.upper()
        if target_name not in self.tlm_target_names:
            raise JsonTlmError(f"Target {target_name} is not mapped to this interface", status=403)
        try:
            packet = System.telemetry.packet(target_name, packet_name)
        except Exception as error:
            raise JsonTlmError(f"Unknown telemetry packet {target_name} {packet_name}", status=404) from error
        items = []
        for item in packet.sorted_items:
            if item.data_type == "DERIVED":
                continue
            items.append(
                {
                    "name": item.name,
                    "data_type": item.data_type,
                    "bit_size": item.bit_size,
                    "array_size": item.array_size,
                    "states": list(item.states.keys()) if item.states else None,
                    "read_conversion": item.read_conversion is not None,
                    "write_conversion": item.write_conversion is not None,
                    "units": item.units,
                }
            )
        return {"target": target_name, "packet": packet_name, "items": items}

    def details(self):
        result = super().details()
        result["listen_address"] = self.listen_address
        result["port"] = self.port
        result["route_prefix"] = self.route_prefix
        result["auth_mode"] = self.auth_mode
        if self.auth_mode == "OIDC":
            result["authenticated"] = True
            result["oidc"] = self.oidc.details() if self.oidc else None
        else:
            result["authenticated"] = bool(self.auth_token)
        result["request_queue_length"] = self.request_queue.qsize()
        result["rejected_count"] = self.rejected_count
        return result
