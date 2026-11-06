"""COSMOS interface: replay recorded real passes (satnogs_replay.py) into REPLAY_* targets.

    INTERFACE REPLAY_INT satnogs_replay_interface.py
      MAP_TARGET REPLAY
      MAP_TARGET REPLAY_CANVAS
      OPTION SATELLITE REPLAY_CANVAS 68635 canvas application_process_id APID

Controlled with commands to the REPLAY target:

    REPLAY PLAY with RECORDING 'CANVAS_15365914', AS_TARGET 'REPLAY_CANVAS', AOS_TIME 1793925000,
                     SPEED 10, DELIVERY 60, OVERRIDES '{"bcn_cmd_rjct_cnt": 3}', DROP 0
    REPLAY STOP

AOS_TIME 0 starts the pass now. REPLAY STATUS says what's playing. Replays go to
their own targets, never the live ones, so real data is never mixed with them.
"""

import collections
import json
import threading
import time

import satnogs_replay
from satnogs_frames import Satellite

from openc3.interfaces.interface import Interface


class SatnogsReplayInterface(Interface):
    def __init__(self):
        super().__init__()
        self.satellites = {}
        self.known = {}
        self.decoders = {}
        self.pending = {}  # target -> deque of (deliver at, target, packet, json bytes): one replay per target
        self.outbox = collections.deque()  # STATUS packets to send now
        self.status = {"state": "IDLE", "recording": "", "as_target": "", "frames_sent": 0, "frames_total": 0}
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self._connected = False

    def set_option(self, option_name, option_values):
        if option_name.upper() == "SATELLITE":
            satellite = Satellite(*option_values)
            self.satellites[satellite.target] = satellite
        else:
            super().set_option(option_name, option_values)

    def connection_string(self):
        return f"SatNOGS replay: {', '.join(satnogs_replay.list_recordings()) or 'no recordings'}"

    def connect(self):
        super().connect()
        from openc3.system.system import System  # noqa: PLC0415

        for target in self.satellites:
            try:
                self.known[target] = set(System.telemetry.packets(target).keys())
            except Exception:  # noqa: BLE001
                self.known[target] = set()
        self._connected = True
        self._status()

    def connected(self):
        return self._connected

    def disconnect(self):
        self._connected = False
        self.wake.set()
        super().disconnect()

    def _status(self, **changes):
        with self.lock:
            self.status.update(changes)
            self.status["recordings"] = ",".join(satnogs_replay.list_recordings())
            self.status["playing"] = ",".join(sorted(t for t, q in self.pending.items() if q))
            self.status["time"] = time.time()
            self.outbox.append(("REPLAY", "STATUS", json.dumps(self.status).encode()))
        self.wake.set()

    def write_interface(self, data, extra=None):
        self.write_interface_base(data, extra)
        command = json.loads(bytes(data))
        name = str(command.get("command") or "").upper()
        if name == "STOP":
            target = str(command.get("as_target") or "").upper()
            with self.lock:
                for key in [target] if target else list(self.pending):
                    self.pending.pop(key, None)
            self._status(state="STOPPED", as_target=target)
        elif name == "PLAY":
            self._play(command)
        else:
            raise ValueError(f"Unknown replay command {name!r}")

    def _play(self, command):
        recording = satnogs_replay.load(command.get("recording"))
        target = str(command.get("as_target") or f"REPLAY_{recording['target']}").upper()
        satellite = self.satellites.get(target)
        if satellite is None:
            raise ValueError(f"{target} isn't a replay target here (have: {', '.join(self.satellites)})")
        if target not in self.decoders:
            self.decoders[target] = satellite.decoder_class()
        aos = float(command.get("aos_time") or 0) or time.time()
        overrides = command.get("overrides") or {}
        if isinstance(overrides, str):
            overrides = json.loads(overrides or "{}")
        entries, los = satnogs_replay.schedule(
            recording,
            satellite,
            self.decoders[target],
            self.known.get(target, set()),
            aos,
            speed=command.get("speed") or 1.0,
            delivery=command.get("delivery", 60.0),
            overrides=overrides,
            drop=command.get("drop") or 0.0,
        )
        with self.lock:
            self.pending[target] = collections.deque(
                (when, t, p, json.dumps(doc, separators=(",", ":")).encode()) for when, t, p, doc in entries
            )
        self._status(
            state="PLAYING" if entries else "DONE",
            recording=recording_name(recording),
            as_target=target,
            frames_sent=0,
            frames_total=len(entries),
            aos_time=aos,
            los_time=los,
            overrides=json.dumps(overrides),
        )

    def read_interface(self):
        while self._connected:
            with self.lock:
                if self.outbox:
                    target, packet, data = self.outbox.popleft()
                    break
                queues = [q for q in self.pending.values() if q]
                first = min(queues, key=lambda q: q[0][0]) if queues else None
                due = first[0][0] if first else None
                if due is not None and due <= time.time():
                    _when, target, packet, data = first.popleft()
                    self.status["frames_sent"] += 1
                    finished = not first
                    break
            self.wake.wait(1.0 if due is None else min(1.0, max(0.0, due - time.time())))
            self.wake.clear()
        else:
            return None, None
        if packet != "STATUS" and finished:
            self._status(state="DONE")
        extra = {"target": target, "packet": packet}
        self.read_interface_base(data, extra)
        return data, extra

    def convert_data_to_packet(self, data, extra):
        packet = super().convert_data_to_packet(data, None)
        if extra:
            packet.target_name = extra["target"]
            packet.packet_name = extra["packet"]
        return packet


def recording_name(recording):
    return f"{recording['target']}_{recording['observation']}"
