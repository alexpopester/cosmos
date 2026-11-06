"""COSMOS interface: real satellite telemetry from SatNOGS.

    INTERFACE SATNOGS_INT satnogs_interface.py <poll seconds> <lookback hours>
      MAP_TARGET CANVAS
      OPTION SATELLITE CANVAS 68635 canvas application_process_id APID

``OPTION SATELLITE <target> <norad> <decoder> <field> <prefix>`` (one per
satellite): decode its frames with that satnogs-decoders decoder, and file them
under packet ``<prefix>_<value of field>``, else OTHER, else UNDECODED (see
satnogs_frames.py). An optional SatNOGS API token (env SATNOGS_TOKEN, from a
COSMOS secret) raises the API's rate limit.

Frames arrive minutes to hours after the pass (stations upload them after the
observation). Each packet's PACKET_TIME is when the station received the frame,
so graphs and the data extractor put it in the right place; the received time is
when COSMOS got it. Telemetry only: commands are refused.
"""

import collections
import os
import threading
import time

from satnogs_frames import Satellite, SatnogsFeed

from openc3.interfaces.interface import Interface


class SatnogsInterface(Interface):
    def __init__(self, poll_seconds=600, lookback_hours=6):
        super().__init__()
        self.poll_seconds = max(60.0, float(poll_seconds))
        self.lookback_hours = float(lookback_hours)
        self.satellites = []
        self.feed = None
        self.queue = collections.deque()
        self.next_poll = 0.0
        self.stop = threading.Event()
        self.polling = None  # the poll in progress (a generator of frame batches)
        self._connected = False

    def set_option(self, option_name, option_values):
        if option_name.upper() == "SATELLITE":
            if len(option_values) != 5:
                raise ValueError("OPTION SATELLITE <target> <norad> <decoder> <field> <prefix>")
            self.satellites.append(Satellite(*option_values))
        else:
            super().set_option(option_name, option_values)

    def connection_string(self):
        return f"SatNOGS: {', '.join(f'{s.target} ({s.norad})' for s in self.satellites)}"

    def connect(self):
        super().connect()
        known = {s.target: _packet_names(s.target) for s in self.satellites}
        self.feed = SatnogsFeed(
            self.satellites,
            known,
            lookback_hours=self.lookback_hours,
            token=os.environ.get("SATNOGS_TOKEN") or None,
            log=_log,
        )
        # Don't deliver again what an earlier run of this interface already did
        for satellite in self.satellites:
            newest = _newest_frame_time(satellite.target, known[satellite.target])
            if newest:
                self.feed.newest[satellite.target] = newest
        self.stop.clear()
        self.next_poll = 0.0
        self.polling = None
        self._connected = True

    def connected(self):
        return self._connected

    def disconnect(self):
        self._connected = False
        self.stop.set()
        super().disconnect()

    def read_interface(self):
        while self._connected:
            if self.queue:
                frame = self.queue.popleft()
                data = frame.json()
                extra = {"target": frame.target, "packet": frame.packet}
                self.read_interface_base(data, extra)
                return data, extra
            if self.polling is not None:
                # One observation at a time: COSMOS gets each one's frames while the rest download
                batch = next(self.polling, None)
                if batch is None:
                    self.polling = None
                    _log("info", f"SatNOGS poll done: {self.feed.stats}")
                else:
                    self.queue.extend(batch)
                continue
            now = time.time()
            if now >= self.next_poll:
                self.next_poll = now + self.poll_seconds
                self.polling = self.feed.poll_iter()
                continue
            self.stop.wait(min(1.0, self.next_poll - now))
        return None, None

    def convert_data_to_packet(self, data, extra):
        packet = super().convert_data_to_packet(data, None)
        if extra:
            packet.target_name = extra["target"]
            packet.packet_name = extra["packet"]
        return packet

    def write_interface(self, data, extra=None):
        raise RuntimeError("SatNOGS is receive only: this interface can't send commands")


def _packet_names(target):
    try:
        from openc3.system.system import System  # noqa: PLC0415

        return set(System.telemetry.packets(target).keys())
    except Exception:  # noqa: BLE001
        return set()


def _newest_frame_time(target, packets):
    """The newest _frame_time COSMOS already has for this target (its current values), or None."""
    try:
        from openc3.api import tlm  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    newest = None
    for packet in packets:
        try:
            value = tlm(f"{target} {packet} NEWEST_FRAME_TIME", type="RAW") or tlm(
                f"{target} {packet} FRAME_TIME", type="RAW"
            )
        except Exception:  # noqa: BLE001 - never received yet
            continue
        if value:
            newest = max(newest or 0.0, float(value))
    return newest


def _log(level, message):
    try:
        from openc3.utilities.logger import Logger  # noqa: PLC0415

        getattr(Logger, {"warn": "warn", "error": "error"}.get(level, "info"))(message)
    except Exception:  # noqa: BLE001
        print(f"{level.upper()}: {message}")
