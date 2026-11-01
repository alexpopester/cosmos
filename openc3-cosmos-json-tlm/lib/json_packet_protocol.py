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
#
# Dynamic items work too:
#   - Legacy dynamic items (bit_size / array_size of 0 or negative, "the rest of
#     the packet") simply grow or shrink the buffer to fit the value.
#   - VARIABLE_BIT_SIZE items get their length item filled in from the value, and
#     everything after them shifts. Send the data, not the length: a length item
#     in the JSON is only checked against the computed value.
#   - BLOCK values can be a string, a list of byte values, or COSMOS's own JSON
#     binary form {"json_class": "String", "raw": [...]}.
#
# Packets defined with a non-binary accessor (ACCESSOR JsonAccessor etc.) work
# too: their buffer is the structured document itself, so the items are written
# into that document instead of being packed into bits.

import json
import math
from datetime import datetime, timezone

from openc3.accessors.binary_accessor import BinaryAccessor
from openc3.accessors.json_accessor import JsonAccessor, json_dumps, json_loads
from openc3.config.config_parser import ConfigParser
from openc3.interfaces.protocols.protocol import Protocol
from openc3.system.system import System
from openc3.utilities.logger import Logger


VALUE_TYPES = ("RAW", "CONVERTED")
MISSING_ITEM_MODES = ("DEFAULTS", "LAST")
EMPTY_VARIABLE_MODES = ("CHECK", "REJECT", "ALLOW")
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


class VariableSizeWriteAccessor(BinaryAccessor):
    """BinaryAccessor that sizes VARIABLE_BIT_SIZE items correctly on write.

    COSMOS's own write path computes a variable item's new size as len(value) * 8,
    which is right for BLOCKs and ASCII strings but wrong for arrays whose elements
    aren't 8 bits and for non-ASCII strings. Only ever installed on our private copy
    of a packet, so COSMOS's own packets and decom are untouched.
    """

    def write_item(self, item, value, buffer):
        # Strings are sized by character count when they grow, so hand COSMOS the
        # UTF-8 bytes. Bytes write identically for ASCII.
        if item.data_type == "STRING" and isinstance(value, str):
            value = value.encode("utf-8")
        return super().write_item(item, value, buffer)

    def _write_variable_other(self, item, value, buffer):
        variable = item.variable_bit_size
        length_name = variable["length_item_name"]
        bits_per_count = variable["length_bits_per_count"]
        bit_offset = variable["length_value_bit_offset"]

        current_bit_size = (self.packet.read(length_name, "CONVERTED") * bits_per_count) + bit_offset
        bits_after_item = (len(buffer) * 8) - (item.bit_offset + current_bit_size)

        if item.original_array_size is not None:
            new_bit_size = len(value) * item.bit_size
        else:
            new_bit_size = len(value) * 8
        length_value, remainder = divmod(new_bit_size - bit_offset, bits_per_count)
        if remainder or length_value < 0:
            raise ValueError(
                f"{new_bit_size} bits can't be described by {length_name} "
                f"(length * {bits_per_count} + {bit_offset} bits)"
            )

        # A negative size means "up to the bits after this item", so writing the
        # value moves everything after it.
        if item.original_array_size is not None:
            item.array_size = -bits_after_item
        else:
            item.bit_size = -bits_after_item
        self.packet.write(length_name, length_value)
        return new_bit_size - current_bit_size


def _variable_items(packet):
    return [
        item
        for item in packet.sorted_items
        if item.variable_bit_size and item.data_type != "DERIVED" and item.parent_item is None
    ]


def _reset_layout(packet):
    """Put every item back at its defined offset and size (undoing earlier shifts)."""
    for item in packet.sorted_items:
        item.bit_offset = item.original_bit_offset
        item.bit_size = item.original_bit_size
        item.array_size = item.original_array_size


def _is_quic(item):
    return item.data_type in ("INT", "UINT") and item.original_array_size is None


def _empty_size(item):
    """(length item value, bit size) of the smallest valid size of a variable item."""
    if _is_quic(item):
        return 0, 6
    bits_per_count = item.variable_bit_size["length_bits_per_count"]
    bit_offset = item.variable_bit_size["length_value_bit_offset"]
    length_value = max(0, -(bit_offset // bits_per_count))  # smallest length giving a size >= 0
    return length_value, (length_value * bits_per_count) + bit_offset


def _zero_buffer(packet):
    """Zeroed buffer with every variable item at its smallest size and its length item to match."""
    _reset_layout(packet)
    variable_items = _variable_items(packet)
    empty = {item.name: _empty_size(item) for item in variable_items}
    # Assign the internal buffer directly: the buffer setter would lay the packet
    # out from length items that are still zero, which may not be valid sizes.
    # Start oversized, since COSMOS can place items after a variable item further
    # out than the definition's own length.
    spare_bytes = sum((bits + 7) // 8 for _, bits in empty.values())
    packet._buffer = bytearray(packet.defined_length + spare_bytes)
    for item in variable_items:
        packet.write(item.variable_bit_size["length_item_name"], empty[item.name][0])
    packet.recalculate_bit_offsets()

    # Trim to exactly what COSMOS's layout needs, as decom will see it
    end_bits = 0
    for item in packet.sorted_items:
        if item.data_type == "DERIVED" or item.parent_item is not None or item.bit_offset < 0:
            continue
        if item.name in empty:
            bits = empty[item.name][1]
        elif item.array_size is not None:
            bits = max(item.array_size, 0)
        else:
            bits = max(item.bit_size, 0)
        end_bits = max(end_bits, item.bit_offset + bits)
    length = max(((end_bits + 7) // 8) + (packet.neg_bit_size // 8), packet.defined_length)
    packet.buffer = packet.buffer_no_copy()[:length]


def _load_buffer(packet, buffer):
    """Load a buffer (e.g. the last received one). False if its length items don't fit it."""
    _reset_layout(packet)
    packet._buffer = bytearray(buffer)
    try:
        packet.recalculate_bit_offsets()
        for item in _variable_items(packet):
            packet.read_item(item, "RAW")
    except (ValueError, RuntimeError, TypeError):
        return False
    packet.read_conversion_cache = {}
    return True


def _private_copy(packet, variable):
    """Copy a packet for one request, using our accessor for binary packets."""
    # Variable sized writes move item offsets, so those packets need their own
    # items. Fixed layouts share them: a deep copy costs ~65us per packet.
    copy = packet.deep_copy() if variable else packet.clone()
    if type(copy.accessor) is BinaryAccessor:
        copy.accessor = VariableSizeWriteAccessor(copy)
    return copy


def _empty_packet(defined, variable):
    """The packet DEFAULTS starts from, built once per definition and copied per request.

    Telemetry items have no DEFAULT in COSMOS, so it's a zero buffer (variable
    items at their smallest size), any template, and the ID values so the packet
    always identifies correctly. Cached on the definition, so reloading the
    target's definitions rebuilds it.
    """
    empty = getattr(defined, "_json_tlm_empty_packet", None)
    if empty is None:
        empty = _private_copy(defined, variable)
        if variable:
            _zero_buffer(empty)
        else:
            empty.buffer = bytes(empty.defined_length)
        empty.restore_defaults()
        for id_item in empty.id_items or []:
            # id_items still point at the definition's items, so write our copy
            empty.write_item(empty.get_item(id_item.name), id_item.id_value, "RAW")
        defined._json_tlm_empty_packet = empty
    return empty


def _decom_error(defined, buffer):
    """Decode a buffer from the definition the way COSMOS decom does. The error it hits, or None."""
    trial = defined.deep_copy()
    _reset_layout(trial)
    trial._buffer = bytearray(buffer)
    try:
        trial.recalculate_bit_offsets()
        for item in trial.sorted_items:
            if item.data_type != "DERIVED":
                trial.read_item(item, "RAW")
    except Exception as error:
        return str(error)
    return None


def _empty_variable_item_error(defined, packet, variable_items, mode):
    """Guard against a COSMOS core decom bug with empty VARIABLE_BIT_SIZE items.

    COSMOS decom reads a variable item whose size works out to 0 as "the rest of
    the packet". For strings, blocks and 8-bit arrays that item just reads wrong,
    but for wider arrays decom can raise and drop the whole packet from the
    current value table. Rejecting here gives the sender a 400 instead.
    CHECK rejects only packets COSMOS can't decode; REJECT any empty item.
    """
    empty = []
    for item in variable_items:
        if _is_quic(item):
            continue  # never 0 bits
        variable = item.variable_bit_size
        length_value = packet.read(variable["length_item_name"], "CONVERTED")
        if (length_value * variable["length_bits_per_count"]) + variable["length_value_bit_offset"] == 0:
            empty.append(item.name)
    if not empty:
        return None
    names = ", ".join(empty)
    if mode == "REJECT":
        return (
            f"{names}: empty, and COSMOS decodes an empty VARIABLE_BIT_SIZE item as the rest of the packet. "
            "Send at least one element (EMPTY_VARIABLE_ITEMS is REJECT)"
        )
    error = _decom_error(defined, packet.buffer_no_copy())
    if error is None:
        return None
    return (
        f"{names}: empty, and COSMOS decom can't decode this packet with an empty VARIABLE_BIT_SIZE item "
        f"(it reads it as the rest of the packet: {error}). Send at least one element, "
        "or set EMPTY_VARIABLE_ITEMS ALLOW to accept it anyway"
    )


def _structured_start(defined, missing_items):
    """Starting buffer for a packet with a non-binary accessor (JSON, CBOR, XML, ...).

    DEFAULTS starts from the definition's TEMPLATE (or an empty JSON object), so
    items the caller didn't send are simply absent and decom reads them as nil
    rather than 0. LAST starts from the last buffer received, if it parses.
    """
    json_packet = isinstance(defined.accessor, JsonAccessor)
    empty = defined.template or (b"{}" if json_packet else b"")
    start = empty
    if missing_items == "LAST" and defined.buffer_no_copy():
        start = defined.buffer_no_copy()
    if not json_packet:
        return bytearray(start)
    try:
        doc = json_loads(bytes(start))
    except ValueError:
        doc = json_loads(bytes(empty))
    if not isinstance(doc, (dict, list)):
        raise JsonTlmError(f"{defined.target_name} {defined.packet_name}: TEMPLATE is not a JSON object")
    return doc


def _coerce_block(value):
    """JSON has no bytes, so accept a list of byte values or COSMOS's JSON binary form."""
    if isinstance(value, dict) and value.get("json_class") == "String" and "raw" in value:
        value = value["raw"]
    if isinstance(value, list):
        return bytearray(value)
    return value


def build_packet_from_json(
    body,
    target_name=None,
    packet_name=None,
    allowed_targets=None,
    default_value_type="RAW",
    missing_items="DEFAULTS",
    strict_converted=True,
    empty_variable_items="CHECK",
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

    # Work on a private copy so concurrent requests never share a buffer.
    # Items the caller didn't send start from a clean packet (DEFAULTS) instead of
    # whatever the last packet happened to contain (the inject_tlm footgun). LAST
    # falls back to that when the last buffer can't be laid out (e.g. nothing has
    # been received yet and its length items are all zero).
    # Non-binary accessors: items go into the document (a decoded dict for JSON,
    # parsed once and serialised once), with no bit layout to manage
    structured = not isinstance(defined.accessor, BinaryAccessor)
    variable = not structured and bool(_variable_items(defined))
    target = None
    if structured:
        packet = defined.clone()
        target = _structured_start(defined, missing_items)
        if not isinstance(target, bytearray):
            for id_item in packet.id_items or []:
                packet.write_item(id_item, id_item.id_value, "RAW", target)
    elif missing_items == "DEFAULTS":
        packet = _private_copy(_empty_packet(defined, variable), variable)
    else:
        packet = _private_copy(defined, variable)
        if variable and not _load_buffer(packet, packet.buffer_no_copy()):
            packet = _private_copy(_empty_packet(defined, variable), variable)
    variable_items = [] if structured else _variable_items(packet)
    length_items = {item.variable_bit_size["length_item_name"]: item.name for item in variable_items}

    # Write variable sized items first, in packet order. Each one moves the bytes
    # after it, so fixed items written afterwards land at their final offsets.
    errors = []
    to_write = []
    given_lengths = {}
    for raw_name, value in items.items():
        name = str(raw_name).upper()
        try:
            item = packet.get_item(name)
        except Exception:
            errors.append(f"{name}: no such item")
            continue
        if name in length_items:
            # Lengths come from the data; a given value is only checked afterwards
            given_lengths[name] = value
            continue
        to_write.append((item, value))
    order = {item.name: index for index, item in enumerate(variable_items)}
    to_write.sort(key=lambda pair: order.get(pair[0].name, len(order)))

    for item, value in to_write:
        name = item.name
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
        if item.data_type == "BLOCK":
            value = _coerce_block(value)
        try:
            packet.write_item(item, value, value_type, target)
        except Exception as error:
            errors.append(f"{name}: {error}")

    if not errors:
        for name, value in given_lengths.items():
            actual = packet.read(name, value_type)
            if actual != value:
                errors.append(
                    f"{name}: is the length of {length_items[name]} and is set from its value; "
                    f"given {value} but the data needs {actual}"
                )

    if not errors and variable_items and empty_variable_items != "ALLOW":
        error = _empty_variable_item_error(defined, packet, variable_items, empty_variable_items)
        if error:
            errors.append(error)

    if errors:
        raise JsonTlmError("; ".join(errors))

    if structured:
        packet.buffer = target if isinstance(target, bytearray) else json_dumps(target).encode()
    packet.target_name = target_name
    packet.packet_name = packet_name
    packet.received_time = _parse_received_time(body.get("received_time"))
    packet.stored = _parse_bool(body.get("stored"), "stored")
    return packet


class JsonPacketProtocol(Protocol):
    """READ protocol that turns a JSON payload into a COSMOS packet.

    PROTOCOL READ json_packet_protocol.py <DEFAULT_TYPE> <MISSING_ITEMS> <STRICT_CONVERTED> <EMPTY_VARIABLE_ITEMS>
      DEFAULT_TYPE         RAW (default) or CONVERTED, used when the body has no "type"
      MISSING_ITEMS        DEFAULTS (default) or LAST
      STRICT_CONVERTED     TRUE (default) rejects CONVERTED writes that would double-convert
      EMPTY_VARIABLE_ITEMS CHECK (default), REJECT or ALLOW: see _empty_variable_item_error
    """

    def __init__(
        self,
        default_value_type="RAW",
        missing_items="DEFAULTS",
        strict_converted=True,
        empty_variable_items="CHECK",
        allow_empty_data=None,
    ):
        super().__init__(allow_empty_data)
        self.default_value_type = str(default_value_type).upper()
        if self.default_value_type not in VALUE_TYPES:
            raise ValueError(f"default_value_type must be one of {VALUE_TYPES}")
        self.missing_items = str(missing_items).upper()
        if self.missing_items not in MISSING_ITEM_MODES:
            raise ValueError(f"missing_items must be one of {MISSING_ITEM_MODES}")
        self.strict_converted = ConfigParser.handle_true_false(strict_converted)
        self.empty_variable_items = str(empty_variable_items).upper()
        if self.empty_variable_items not in EMPTY_VARIABLE_MODES:
            raise ValueError(f"empty_variable_items must be one of {EMPTY_VARIABLE_MODES}")
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
                empty_variable_items=self.empty_variable_items,
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
        result["empty_variable_items"] = self.empty_variable_items
        result["dropped_count"] = self.dropped_count
        return result
