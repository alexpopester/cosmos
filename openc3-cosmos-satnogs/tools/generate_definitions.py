"""Generate the targets' telemetry definitions from real SatNOGS frames.

Developer tool (not part of the gem). For each satellite in SATELLITES it
downloads frames from recent observations, decodes them, groups them by the
satellite's packet field (an APID, a beacon type), and writes
targets/<TARGET>/cmd_tlm/tlm.txt:

- one packet per group that decodes past the common header, ``<PREFIX>_<value>``
- OTHER for frames that decode only as far as the header
- UNDECODED for frames the decoder can't read

Item types come from the values seen (integers, floats, strings). It also keeps
one real frame per packet in test/fixtures/<TARGET>/ for the tests.

    python tools/generate_definitions.py            # needs internet and satnogs-decoders
"""

import collections
import os
import re
import sys
import time


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..")
sys.path.insert(0, os.path.join(ROOT, "lib"))

import satnogs_frames as sf  # noqa: E402


# target, NORAD, decoder, packet field, packet prefix, description
SATELLITES = [
    ("CANVAS", 68635, "canvas", "application_process_id", "APID", "CANVAS (CU Boulder / LASP), CCSDS"),
    ("AEPEX", 68506, "aepex", "application_process_id", "APID", "AEPEX (CU Boulder / LASP), CCSDS"),
    ("CATSAT", 60246, "catsat", "type", "TYPE", "CatSat (University of Arizona), GomSpace beacons"),
]
OBSERVATIONS = 30
RESERVED = {
    "PACKET_TIME",
    "PACKET_TIMESECONDS",
    "PACKET_TIMEFORMATTED",
    "RECEIVED_TIME",
    "RECEIVED_TIMESECONDS",
    "RECEIVED_TIMEFORMATTED",
    "RECEIVED_COUNT",
}

META_ITEMS = [
    ("FRAME_TIME", "_frame_time", "64 FLOAT", "When the SatNOGS station received the frame (Unix seconds)"),
    ("OBSERVATION", "_observation", "32 UINT", "SatNOGS observation id"),
    ("STATION", "_station", "256 STRING", "SatNOGS station that received it"),
    ("STATION_ID", "_station_id", "32 UINT", "SatNOGS station id"),
    ("NORAD", "_norad", "32 UINT", "Satellite catalog number"),
    ("FRAME_BYTES", "_frame_bytes", "32 UINT", "Frame length (bytes)"),
    ("FRAME_HEX", "_frame_hex", "4096 STRING", "The raw frame, hex"),
]


def item_name(field):
    name = re.sub(r"[^A-Za-z0-9_]", "_", field).upper().strip("_") or "FIELD"
    if name[0].isdigit():
        name = f"F_{name}"
    if name in RESERVED or name in {m[0] for m in META_ITEMS}:
        name = f"SAT_{name}"
    return name


def item_type(values):
    kinds = {type(v) for v in values if v is not None}
    if kinds <= {int}:
        return "64 INT"
    if kinds <= {int, float}:
        return "64 FLOAT"
    if kinds <= {list}:
        return "8 ARRAY"
    return "1024 STRING"


def packet_lines(target, packet, description, fields):
    lines = [f'TELEMETRY {target} {packet} BIG_ENDIAN "{description}"', "  ACCESSOR JsonAccessor"]
    for name, key, kind, text in META_ITEMS:
        lines += [f'  APPEND_ITEM {name} {kind} "{text}"', f"    KEY $.{key}"]
        if name == "FRAME_TIME":
            lines += [
                '  ITEM PACKET_TIME 0 0 DERIVED "When the station received the frame"',
                "    READ_CONVERSION openc3/conversions/unix_time_conversion.py FRAME_TIME",
            ]
    used = set()
    for field in sorted(fields):
        name = item_name(field)
        while name in used:
            name += "_"
        used.add(name)
        lines += [f'  APPEND_ITEM {name} {item_type(fields[field])} "{field}"', f"    KEY $.{field}"]
    return lines


def main():
    import requests  # noqa: PLC0415

    session = requests.Session()
    feed = sf.SatnogsFeed([], {}, session=session, lookback_hours=24 * 30)
    for target, norad, decoder_name, field, prefix, description in SATELLITES:
        satellite = sf.Satellite(target, norad, decoder_name, field, prefix)
        decoder_class = satellite.decoder_class()
        url, params, observations = (
            f"{sf.NETWORK_API}/observations/",
            {"format": "json", "norad_cat_id": norad, "status": "good"},
            [],
        )
        while url and len(observations) < OBSERVATIONS:
            response = feed._get(url, params)
            observations += [o for o in response.json() if o["demoddata"]]
            url, params = response.links.get("next", {}).get("url"), None
        groups = collections.defaultdict(lambda: collections.defaultdict(list))  # value -> field -> values
        examples, undecoded, total = {}, None, 0
        for observation in observations[:OBSERVATIONS]:
            for item in observation["demoddata"][:40]:
                data = feed._download(item["payload_demod"])
                total += 1
                fields = sf.decode(decoder_class, data)
                if fields is None:
                    undecoded = undecoded or data
                    continue
                value = fields.get(field)
                for k, v in fields.items():
                    groups[value][k].append(v)
                examples.setdefault(value, data)
        smallest = min(len(g) for g in groups.values())
        own = {v: g for v, g in groups.items() if len(g) > smallest + 1 and v is not None}
        other = collections.defaultdict(list)
        for v, g in groups.items():
            if v not in own:
                for k, values in g.items():
                    other[k].extend(values)

        lines = [
            f"# {description}: generated by tools/generate_definitions.py from {total} real frames",
            f"# ({time.strftime('%Y-%m-%d')}, satnogs-decoders '{decoder_name}'). Raw values: the decoders",
            "# give no units or conversions.",
            "",
        ]
        for value in sorted(own, key=lambda v: (str(type(v)), v)):
            frames = len(next(iter(own[value].values())))
            lines += packet_lines(
                target, f"{prefix}_{value}", f"{description}: {field} {value} ({frames} frames seen)", own[value]
            ) + [""]
        lines += packet_lines(target, "OTHER", f"{description}: frames decoded only as far as the header", other) + [""]
        lines += packet_lines(target, "UNDECODED", f"{description}: frames the decoder can't read", {}) + [""]
        folder = os.path.join(ROOT, "targets", target, "cmd_tlm")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "tlm.txt"), "w") as file:
            file.write("\n".join(lines))

        fixtures = os.path.join(ROOT, "test", "fixtures", target)
        os.makedirs(fixtures, exist_ok=True)
        for value, data in examples.items():
            packet = f"{prefix}_{value}" if value in own else f"OTHER_{value}"
            with open(os.path.join(fixtures, f"{packet}.bin"), "wb") as file:
                file.write(data)
        if undecoded:
            with open(os.path.join(fixtures, "UNDECODED.bin"), "wb") as file:
                file.write(undecoded)
        print(f"{target}: {total} frames, packets {[f'{prefix}_{v}' for v in sorted(own, key=str)]} + OTHER, UNDECODED")


if __name__ == "__main__":
    main()
