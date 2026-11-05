"""Shared pieces of the real-pass soak (docs/REAL-DATA-TESTING.md step 2).

The soak lives in the local COSMOS's Flow Automator: real SatNOGS stations,
real satellites kept current from CelesTrak, a harmless template and one rule
that schedules it into every pass. Everything it creates is named with
PREFIX so it can be found and removed again.
"""

import json
import os
import time

import requests


HERE = os.path.dirname(os.path.abspath(__file__))
SOAK_DIR = os.path.join(HERE, "cache", "soak")
MANIFEST = os.path.join(SOAK_DIR, "manifest.json")
TOKEN_FILE = os.path.join(SOAK_DIR, "token")
PREFIX = "Soak: "

# Satellites with many decoded frames in SatNOGS (step 1 sample, 2026-11-05), named, still transmitting.
# KNACKSAT-2 decays fast (TLEs age quickly); METEOR M2-3 is higher (~830 km); ISS is the familiar one.
SATELLITES = [
    (69015, "FRONTIERSAT"),
    (60528, "ERNST"),
    (98330, "PEARL-1B"),
    (99299, "SPRITE"),
    (62391, "LASARSAT"),
    (68635, "CANVAS"),
    (44530, "TAURUS-1"),
    (68460, "COSMO"),
    (68506, "AEPEX"),
    (63235, "OTP-2"),
    (98267, "REPLICATOR-2"),
    (98273, "KOYO"),
    (98238, "CRIMSON-1"),
    (98293, "MARINA"),
    (98395, "KOSTKA"),
    (67683, "KNACKSAT-2"),
    (57166, "METEOR M2-3"),
    (25544, "ISS"),
]

# Busy, online SatNOGS stations, spread over six continents and from 41 S to 65 N
STATIONS = [91, 1864, 2029, 2134, 2051, 1936, 1861, 3020, 223, 657, 2107, 2380, 2987]


def now_iso(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t if t is not None else time.time()))


def load_json(path, default=None):
    try:
        with open(path) as file:
            return json.load(file)
    except FileNotFoundError:
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.tmp"
    with open(temporary, "w") as file:
        json.dump(data, file, indent=1)
    os.replace(temporary, path)


class FlowAutomator:
    """A thin client for the Flow Automator HTTP API on a local COSMOS."""

    def __init__(self, url="http://localhost:2900", scope="DEFAULT", token=None, password=None):
        self.url = url.rstrip("/")
        self.scope = scope
        self.session = requests.Session()
        if token is None and password:
            token = self.login(password)
        if token is None:
            token = read_token()
        if not token:
            raise SystemExit(
                f"No COSMOS token: set COSMOS_PASSWORD once (soak_setup.py stores a session token in {TOKEN_FILE})"
            )
        self.token = token

    def login(self, password):
        response = self.session.post(f"{self.url}/openc3-api/auth/verify", json={"password": password}, timeout=30)
        response.raise_for_status()
        return response.text.strip().strip('"')

    def request(self, method, path, body=None, **params):
        response = self.session.request(
            method,
            f"{self.url}/flow-automator/api{path}",
            params={"scope": self.scope, **params},
            json=body,
            headers={"Authorization": self.token},
            timeout=120,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"{method} {path}: {response.status_code} {response.text[:300]}")
        return response.json() if response.content else None

    def get(self, path, **params):
        return self.request("GET", path, **params)

    def post(self, path, body=None):
        return self.request("POST", path, body or {})

    def put(self, path, body):
        return self.request("PUT", path, body)

    def delete(self, path):
        return self.request("DELETE", path)


def read_token():
    try:
        with open(TOKEN_FILE) as file:
            return file.read().strip() or None
    except FileNotFoundError:
        return None


def write_token(token):
    os.makedirs(SOAK_DIR, exist_ok=True)
    descriptor = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as file:
        file.write(token)
