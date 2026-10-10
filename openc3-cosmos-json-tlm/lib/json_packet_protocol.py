# JSON -> COSMOS telemetry packet conversion.
#
# This module holds the one piece of real logic in the plugin: taking a JSON
# description of a telemetry packet and turning it into a fully-populated,
# pre-identified COSMOS Packet built from the target's EXISTING definition.
#
# The resulting packet goes through the normal interface pipeline, so raw
# logging, decom, limits, derived items, reingest and packet counts all behave
# exactly as if the data had arrived over a binary link. No target definition
# changes are required.
#
# It is used two ways:
#   1. Directly by JsonTlmServerInterface (the HTTP/REST interface).
#   2. As a READ protocol you can attach to ANY other interface (MQTT, TCP, ...)
#      whose payloads are JSON:
#         PROTOCOL READ json_packet_protocol.py RAW DEFAULTS
#
# JSON body format (all keys except "items" optional):
#   {
#     "target": "INST",            # optional if given by the URL / interface
#     "packet": "HEALTH_STATUS",   # optional if given by the URL / interface
#     "type": "RAW",               # RAW (default) or CONVERTED
#     "items": {"TEMP1": 12, "MODE": "SAFE"},
#     "received_time": 1760000000000000000,   # ns since epoch, or ISO-8601 string
#     "stored": false              # true = historical data (no CVT update)
#   }

import json
import math
from datetime import datetime, timezone

from openc3.config.config_parser import ConfigParser
from openc3.interfaces.protocols.protocol import Protocol
from openc3.system.system import System
from openc3.utilities.logger import Logger


VALUE_TYPES = ("RAW", "CONVERTED")
MISSING_ITEM_MODES = ("DEFAULTS", "LAST")
RESERVED_KEYS = ("target", "packet", "type", "items", "received_time", "stored")


class JsonTlmError(Exception):
    """Raised for bad input. status is the HTTP status the REST interface returns."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _parse_received_time(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise JsonTlmError("received_time must be ns since epoch or an ISO-8601 string")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1_000_000_000, tz=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise JsonTlmError(f"Invalid received_time '{value}': {error}") from error
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    raise JsonTlmError("received_time must be ns since epoch or an ISO-8601 string")


def _parse_bool(value, name):
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.upper() in ("TRUE", "FALSE"):
        return value.upper() == "TRUE"
    raise JsonTlmError(f"'{name}' must be true or false")


def build_packet_from_json(
    body,
    target_name=None,
    packet_name=None,
    allowed_targets=None,
    default_value_type="RAW",
    missing_items="DEFAULTS",
    strict_converted=True,
):
    """Build a pre-identified Packet from a decoded JSON object.

    Raises JsonTlmError on any problem, so nothing partially-written is ever published.
    """
    if not isinstance(body, dict):
        raise JsonTlmError("JSON body must be an object")

    target_name = str(body.get("target") or target_name or "").upper()
    packet_name = str(body.get("packet") or packet_name or "").upper()
    if not target_name or not packet_name:
        raise JsonTlmError("Target and packet must be given in the URL or the body")

    if allowed_targets is not None and target_name not in allowed_targets:
        raise JsonTlmError(f"Target {target_name} is not mapped to this interface", status=403)

    try:
        defined = System.telemetry.packet(target_name, packet_name)
    except Exception as error:
        raise JsonTlmError(f"Unknown telemetry packet {target_name} {packet_name}", status=404) from error

    value_type = str(body.get("type") or default_value_type).upper()
    if value_type not in VALUE_TYPES:
        raise JsonTlmError(f"type must be one of {', '.join(VALUE_TYPES)}")

    # Accept either {"items": {...}} or a flat object of item names
    if "items" in body:
        items = body["items"]
    else:
        items = {k: v for k, v in body.items() if k not in RESERVED_KEYS}
    if not isinstance(items, dict) or not items:
        raise JsonTlmError("No items given")

    # Work on a private copy so concurrent requests never share a buffer
    packet = defined.clone()
    if missing_items == "DEFAULTS":
        # Items the caller didn't send start from a clean buffer instead of whatever
        # the last packet happened to contain (the inject_tlm footgun). Telemetry
        # items have no DEFAULT in COSMOS, so zero the buffer, apply any template,
        # then write the ID values so the packet always identifies correctly.
        packet.buffer = bytes(packet.defined_length)
        packet.restore_defaults()
        for item in packet.id_items or []:
            packet.write_item(item, item.id_value, "RAW")

    errors = []
    for raw_name, value in items.items():
        name = str(raw_name).upper()
        try:
            item = packet.get_item(name)
        except Exception:
            errors.append(f"{name}: no such item")
            continue
        if item.data_type == "DERIVED":
            errors.append(f"{name}: DERIVED items are calculated by COSMOS and can't be set")
            continue
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)) and item.data_type != "FLOAT":
            errors.append(f"{name}: NaN/Infinity only valid for FLOAT items")
            continue
        if (
            value_type == "CONVERTED"
            and strict_converted
            and item.read_conversion is not None
            and item.write_conversion is None
            and not item.states
        ):
            # Writing an engineering value into the raw field would make COSMOS apply the
            # read conversion on top of it (double conversion). Refuse instead of corrupting data.
            errors.append(
                f"{name}: has a read conversion but no write conversion, so a CONVERTED value can't be "
                "stored correctly. Send the RAW value, or add a WRITE_CONVERSION to the item"
            )
            continue
        try:
            packet.write_item(item, value, value_type)
        except Exception as error:
            errors.append(f"{name}: {error}")

    if errors:
        raise JsonTlmError("; ".join(errors))

    packet.target_name = target_name
    packet.packet_name = packet_name
    packet.received_time = _parse_received_time(body.get("received_time"))
    packet.stored = _parse_bool(body.get("stored"), "stored")
    return packet


class JsonPacketProtocol(Protocol):
    """READ protocol that turns a JSON payload into a COSMOS packet.

    PROTOCOL READ json_packet_protocol.py <DEFAULT_TYPE> <MISSING_ITEMS> <STRICT_CONVERTED>
      DEFAULT_TYPE     RAW (default) or CONVERTED, used when the body has no "type"
      MISSING_ITEMS    DEFAULTS (default) or LAST
      STRICT_CONVERTED TRUE (default) rejects CONVERTED writes that would double-convert
    """

    def __init__(self, default_value_type="RAW", missing_items="DEFAULTS", strict_converted=True, allow_empty_data=None):
        super().__init__(allow_empty_data)
        self.default_value_type = str(default_value_type).upper()
        if self.default_value_type not in VALUE_TYPES:
            raise ValueError(f"default_value_type must be one of {VALUE_TYPES}")
        self.missing_items = str(missing_items).upper()
        if self.missing_items not in MISSING_ITEM_MODES:
            raise ValueError(f"missing_items must be one of {MISSING_ITEM_MODES}")
        self.strict_converted = ConfigParser.handle_true_false(strict_converted)
        self.dropped_count = 0

    def read_packet(self, packet):
        # Never raise here: an exception in a read protocol disconnects the whole interface.
        try:
            body = json.loads(packet.buffer)
            built = build_packet_from_json(
                body,
                target_name=packet.target_name,
                packet_name=packet.packet_name,
                allowed_targets=self.interface.tlm_target_names if self.interface else None,
                default_value_type=self.default_value_type,
                missing_items=self.missing_items,
                strict_converted=self.strict_converted,
            )
            built.extra = packet.extra
            return built
        except Exception as error:
            self.dropped_count += 1
            name = self.interface.name if self.interface else "JsonPacketProtocol"
            Logger.error(f"{name}: dropped JSON telemetry: {error}")
            return "STOP"

    def read_details(self):
        result = super().read_details()
        result["default_value_type"] = self.default_value_type
        result["missing_items"] = self.missing_items
        result["strict_converted"] = self.strict_converted
        result["dropped_count"] = self.dropped_count
        return result
