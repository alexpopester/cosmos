"""Contact gate: the radio link between COSMOS and the cFS "spacecraft", open only during passes.

cFS runs in this container's network namespace (docker --network container:cfs-gate):

    COSMOS --cmd--> gate :CMD_PORT --(in contact)--> cFS CI_LAB 127.0.0.1:1234
    COSMOS :TLM_DEST <--(in contact)-- gate :1235 <--tlm-- cFS TO_LAB (sends to 127.0.0.1:1235)

"In contact" is computed here, independently of the Flow Automator: skyfield's SGP4
for the satellite the cFS flies (its TLE from CelesTrak, refreshed every 6 h) seen from
the ground station, above its elevation mask. So the planner's predicted passes are
checked against a second implementation, the way a real link would check them.

HTTP on CONTROL_PORT:
    GET  /status   link state, elevation, next AOS/LOS, packet counts, fault
    POST /fault    {"mode": "auto" | "closed" | "open" | "window", "seconds": N, "drop": 0..1, "aos": t, "los": t}
                   closed = no link (early LOS, a dead pass); open = link regardless of geometry;
                   window = a staged pass: link only from aos to los (Unix seconds), then back to auto;
                   drop = lose that fraction of packets while the link is up. Back to auto after N s.

Environment: NORAD (catalog number), TLE1/TLE2 (instead of fetching), STATION_LAT, STATION_LON,
STATION_ALT_M, MIN_ELEVATION, CMD_PORT (2234), TLM_DEST_HOST (openc3-operator), TLM_DEST_PORT (1235),
CONTROL_PORT (8089).
"""

import json
import os
import random
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from skyfield.api import EarthSatellite, load, wgs84


TS = load.timescale(builtin=True)
CELESTRAK = "https://celestrak.org/NORAD/elements/gp.php"
TLE_REFRESH_S = 6 * 3600


def env(name, default):
    return os.environ.get(name, default)


class Link:
    def __init__(self):
        self.norad = env("NORAD", "")
        self.station = wgs84.latlon(
            float(env("STATION_LAT", "64.85")),
            float(env("STATION_LON", "-147.85")),
            elevation_m=float(env("STATION_ALT_M", "130")),
        )
        self.mask = float(env("MIN_ELEVATION", "10"))
        self.lock = threading.Lock()
        self.satellite = None
        self.tle_fetched = 0.0
        self.fault = {"mode": "auto", "until": None, "drop": 0.0, "aos": 0.0, "los": 0.0}
        self.counts = {"cmd_forwarded": 0, "cmd_dropped": 0, "tlm_forwarded": 0, "tlm_dropped": 0}
        self.last_error = None
        self.refresh_tle(force=True)

    def refresh_tle(self, force=False):
        if not force and time.time() - self.tle_fetched < TLE_REFRESH_S:
            return
        line1, line2 = env("TLE1", ""), env("TLE2", "")
        try:
            if not line1 and self.norad:
                text = requests.get(CELESTRAK, params={"CATNR": self.norad, "FORMAT": "TLE"}, timeout=30).text
                lines = [line.strip() for line in text.strip().splitlines()]
                line1, line2 = lines[-2], lines[-1]
            satellite = EarthSatellite(line1, line2, f"NORAD {self.norad}", TS)
            with self.lock:
                self.satellite, self.tle_fetched, self.last_error = satellite, time.time(), None
            print(f"TLE: {line1}", flush=True)
        except Exception as error:  # noqa: BLE001 - keep the old TLE and try again later
            self.last_error = f"TLE: {type(error).__name__}: {error}"
            self.tle_fetched = time.time() - TLE_REFRESH_S + 300
            print(self.last_error, flush=True)

    def elevation(self, t=None):
        if self.satellite is None:
            return None
        when = TS.from_datetime(_utc(t or time.time()))
        alt, _az, _dist = (self.satellite - self.station).at(when).altaz()
        return float(alt.degrees)

    def mode(self):
        with self.lock:
            if self.fault["until"] and time.time() >= self.fault["until"]:
                self.fault = {"mode": "auto", "until": None, "drop": 0.0, "aos": 0.0, "los": 0.0}
            return dict(self.fault)

    def up(self):
        fault = self.mode()
        if fault["mode"] == "closed":
            return False
        if fault["mode"] == "open":
            return True
        if fault["mode"] == "window":  # a staged pass: up only between its AOS and LOS
            return fault["aos"] <= time.time() < fault["los"]
        elevation = self.elevation()
        return elevation is not None and elevation >= self.mask

    def passes(self, packet_kind):
        if not self.up():
            self.counts[f"{packet_kind}_dropped"] += 1
            return False
        if random.random() < self.mode()["drop"]:  # noqa: S311 - a simulated fade, not security
            self.counts[f"{packet_kind}_dropped"] += 1
            return False
        self.counts[f"{packet_kind}_forwarded"] += 1
        return True

    def next_pass(self):
        if self.satellite is None:
            return None
        now = time.time()
        t0, t1 = TS.from_datetime(_utc(now - 1200)), TS.from_datetime(_utc(now + 2 * 86400))
        times, events = self.satellite.find_events(self.station, t0, t1, altitude_degrees=self.mask)
        aos = None
        for t, event in zip(times, events, strict=True):
            seconds = t.utc_datetime().timestamp()
            if event == 0:
                aos = seconds
            elif event == 2 and seconds > now:
                return {"aos": aos, "los": seconds}
        return None

    def status(self):
        elevation = self.elevation()
        return {
            "time": time.time(),
            "norad": self.norad,
            "up": self.up(),
            "elevation": None if elevation is None else round(elevation, 2),
            "mask": self.mask,
            "fault": self.mode(),
            "next_pass": self.next_pass(),
            "counts": dict(self.counts),
            "tle_age_h": round((time.time() - self.tle_fetched) / 3600, 2) if self.tle_fetched else None,
            "error": self.last_error,
        }


def _utc(t):
    import datetime  # noqa: PLC0415

    return datetime.datetime.fromtimestamp(t, tz=datetime.timezone.utc)


def forward(link, kind, listen, destination):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(listen)
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    while True:
        data, _source = sock.recvfrom(65535)
        if link.passes(kind):
            try:
                out.sendto(data, destination)
            except OSError as error:  # the other end isn't up (COSMOS restarting): the packet is lost, like RF
                link.last_error = f"{kind}: {error}"


def control_server(link, port):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/status"):
                self._send(200, link.status())
            else:
                self._send(404, {"error": "GET /status"})

        def do_POST(self):  # noqa: N802
            if not self.path.startswith("/fault"):
                self._send(404, {"error": "POST /fault"})
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                mode = str(body.get("mode", "auto"))
                if mode not in ("auto", "closed", "open", "window"):
                    raise ValueError("mode is auto, closed, open or window")
                seconds = float(body.get("seconds") or 0)
                drop = min(1.0, max(0.0, float(body.get("drop") or 0)))
                aos, los = float(body.get("aos") or 0), float(body.get("los") or 0)
                if mode == "window" and not aos < los:
                    raise ValueError("window needs aos < los (Unix seconds)")
            except (ValueError, TypeError) as error:
                self._send(400, {"error": str(error)})
                return
            with link.lock:
                until = time.time() + seconds if seconds else (los + 5 if mode == "window" else None)
                link.fault = {"mode": mode, "until": until, "drop": drop, "aos": aos, "los": los}
            print(f"fault: {link.fault}", flush=True)
            self._send(200, link.status())

        def log_message(self, *args):
            pass

    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()  # noqa: S104 - on the docker network


def main():
    link = Link()
    cmd_port = int(env("CMD_PORT", "2234"))
    tlm_dest = (env("TLM_DEST_HOST", "openc3-operator"), int(env("TLM_DEST_PORT", "1235")))
    threading.Thread(
        target=forward, args=(link, "cmd", ("0.0.0.0", cmd_port), ("127.0.0.1", 1234)), daemon=True
    ).start()  # noqa: S104
    threading.Thread(target=forward, args=(link, "tlm", ("0.0.0.0", 1235), tlm_dest), daemon=True).start()  # noqa: S104
    threading.Thread(target=control_server, args=(link, int(env("CONTROL_PORT", "8089"))), daemon=True).start()
    print(f"gate up: commands :{cmd_port} -> cFS :1234, telemetry :1235 -> {tlm_dest}", flush=True)
    while True:
        link.refresh_tle()
        time.sleep(60)


if __name__ == "__main__":
    main()
