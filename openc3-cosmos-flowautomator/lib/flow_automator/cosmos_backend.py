"""Connects the engine to COSMOS.

Commands and telemetry go through ``openc3.api`` directly (the same functions
the COSMOS API server runs, talking to Redis), so commands are checked,
logged and attributed to this microservice like any other. Scripts go through
the Script Runner API over HTTP, authenticated with the microservice's
service password (the same way COSMOS starts scripts).
"""

import threading
import time

import requests

from .http_api import AuthError


TOKEN_CACHE_SECONDS = 30


class CosmosBackend:
    def __init__(self, scope, logger):
        self.scope = scope
        self.logger = logger

    def tlm(self, item, value_type="CONVERTED"):
        from openc3.api.tlm_api import tlm

        return tlm(item, type=value_type, scope=self.scope)

    def tlm_values(self, refs):
        """refs: [(normalized 'TGT PKT ITEM', type)] -> [[value, limits_state], ...]"""
        from openc3.api.tlm_api import get_tlm_values

        items = ["__".join([*item.split(), value_type]) for item, value_type in refs]
        return get_tlm_values(items, scope=self.scope)

    def tlm_counts(self, packets):
        from openc3.api.tlm_api import get_tlm_cnts

        return get_tlm_cnts([list(packet) for packet in packets], scope=self.scope)

    def cmd_hazardous(self, command):
        """True if the command, or a state chosen for one of its parameters, is hazardous.

        Same rules as get_cmd_hazardous, which in COSMOS 7.4 raises KeyError for
        commands that leave out a parameter that has states.
        """
        from openc3.models.target_model import TargetModel
        from openc3.utilities.extract import extract_fields_from_cmd_text

        target_name, cmd_name, params = extract_fields_from_cmd_text(command)
        params = {k.upper(): v for k, v in params.items()}
        packet = TargetModel.packet(target_name.upper(), cmd_name.upper(), type="CMD", scope=self.scope)
        if packet.get("hazardous") is not None:
            return True
        for item in packet["items"]:
            if item["name"] not in params or not item.get("states"):
                continue
            value = params[item["name"]]
            if isinstance(value, str):
                value = value.replace('"', "").replace("'", "")
            for state_name, state in item["states"].items():
                if state.get("hazardous") is not None and value in (state_name, state.get("value")):
                    return True
        return False

    def send_cmd(self, command, allow_hazardous=False):
        from openc3.api.cmd_api import cmd, cmd_no_hazardous_check
        from openc3.top_level import HazardousError

        try:
            cmd(command, scope=self.scope)
        except HazardousError as error:
            if not allow_hazardous:
                raise RuntimeError(
                    f"Hazardous command blocked: {command}. Turn on 'Allow hazardous' on this node to send it."
                ) from error
            self.logger.warn(f"Flow Automator sending hazardous command: {command}")
            cmd_no_hazardous_check(command, scope=self.scope)

    def set_tlm(self, item, value, value_type="CONVERTED"):
        from openc3.api.tlm_api import set_tlm

        target, packet, name = item.split()
        set_tlm(target, packet, name, value, type=value_type, scope=self.scope)

    def script_start(self, filename, environment):
        from openc3.script.script_runner import script_run

        return script_run(filename, environment=environment or None, scope=self.scope)

    def script_state(self, script_id):
        from openc3.script.script_runner import script_get

        return (script_get(script_id, scope=self.scope) or {}).get("state")

    def script_stop(self, script_id):
        from openc3.script.script_runner import running_script_stop

        running_script_stop(script_id, scope=self.scope)


class TokenAuthorizer:
    """Checks a user's COSMOS token by making a harmless API call with it.

    Core has a single user, so any valid token may read and write. In
    Enterprise the call must pass COSMOS's own role check for 'tlm'.
    TODO: Enterprise should require a 'cmd' style permission for writes.
    """

    def __init__(self, scope, api_url, timeout=5):
        self.scope = scope
        self.api_url = api_url.rstrip("/") + "/openc3-api/api"
        self.timeout = timeout
        self.cache = {}
        self.lock = threading.Lock()

    def __call__(self, token, write):
        if not token:
            raise AuthError("Token is required", 401)
        key = (token, write)
        now = time.monotonic()
        with self.lock:
            if self.cache.get(key, 0) > now:
                return
        try:
            response = requests.post(
                self.api_url,
                json={
                    "jsonrpc": "2.0",
                    "method": "get_target_names",
                    "params": [],
                    "id": 1,
                    "keyword_params": {"scope": self.scope},
                },
                headers={"Authorization": token, "Content-Type": "application/json-rpc"},
                timeout=self.timeout,
            )
        except requests.RequestException as error:
            raise AuthError(f"Could not verify token: {error}", 503) from None
        if response.status_code in (401, 403):
            raise AuthError("Not authorized", response.status_code)
        try:
            body = response.json()
        except ValueError:
            raise AuthError(f"Could not verify token (HTTP {response.status_code})", 503) from None
        error = body.get("error")
        if error:
            message = error.get("message", "Not authorized") if isinstance(error, dict) else str(error)
            raise AuthError(message, 403 if "permission" in message.lower() or "forbidden" in message.lower() else 401)
        with self.lock:
            if len(self.cache) > 1000:
                self.cache.clear()
            self.cache[key] = now + TOKEN_CACHE_SECONDS
