"""Connects the engine to COSMOS.

Commands and telemetry go through ``openc3.api`` directly (the same functions
the COSMOS API server runs, talking to Redis), so commands are checked,
logged and attributed to this microservice like any other. Scripts go through
the Script Runner API over HTTP, authenticated with the microservice's
service password (the same way COSMOS starts scripts).
"""

import base64
import json
import threading
import time

import requests

from .http_api import AuthError


TOKEN_CACHE_SECONDS = 30


class CosmosBackend:
    def __init__(self, scope, logger):
        self.scope = scope
        self.logger = logger
        self._secrets = None

    def get_secret(self, name):
        """A COSMOS secret (Admin Console → Secrets), read when needed so a change applies without a restart."""
        from openc3.utilities.secrets import Secrets

        if self._secrets is None:
            self._secrets = Secrets.get_client()
        return self._secrets.get(name, scope=self.scope)

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
            if not item.get("states"):
                continue
            # A parameter left out takes its default, which may itself be a hazardous state (as in cmd())
            value = params[item["name"]] if item["name"] in params else item.get("default")
            if isinstance(value, str):
                value = value.replace('"', "").replace("'", "").upper()
            for state_name, state in item["states"].items():
                if state.get("hazardous") is not None and value in (state_name.upper(), state.get("value")):
                    return True
        return False

    def tlm_packet(self, target, packet, value_type="CONVERTED"):
        """Every item of a packet as {item: value}."""
        from openc3.api.tlm_api import get_tlm_packet

        return {
            name: value for name, value, *_limits in get_tlm_packet(target, packet, type=value_type, scope=self.scope)
        }

    def put_script(self, path, text):
        """Save a script file in the target's modified files (what Script Runner edits)."""
        from openc3.script.storage import put_target_file

        put_target_file(path, text, scope=self.scope)

    def stash_set(self, key, value):
        from openc3.api.stash_api import stash_set

        stash_set(key, value, scope=self.scope)

    def read_target_file(self, path):
        """A target file's text from the bucket (modified, else original), or None if there isn't one."""
        from openc3.script.storage import get_target_file

        try:
            file = get_target_file(path, scope=self.scope)
        except Exception as error:  # noqa: BLE001
            if "not found" in str(error).lower() or "nosuchkey" in str(error).lower() or "404" in str(error):
                return None
            raise
        if file is None:
            return None
        with file:
            data = file.read()
        return data.decode(errors="replace") if isinstance(data, bytes) else data

    def stash_take(self, key):
        """Read and remove a value a script left in the COSMOS stash (None if there isn't one).

        Atomic where possible: one MULTI/EXEC transaction reads and deletes the stash entry, so a value written
        between a read and a delete can't be lost. (COSMOS's Valkey user may not run Lua.) Falls back to get,
        get again, delete if the stash isn't laid out as expected (another COSMOS version).
        """
        from openc3.api.stash_api import stash_delete, stash_get

        try:
            from openc3.models.stash_model import StashModel
            from openc3.utilities.authorization import authorize
            from openc3.utilities.store import Store

            authorize(permission="script_run", scope=self.scope)
            hash_key = f"{self.scope}__{StashModel.PRIMARY_KEY}"
            with Store.instance().redis_pool.get() as redis:
                pipe = redis.pipeline(transaction=True)
                pipe.hget(hash_key, key)
                pipe.hdel(hash_key, key)
                raw, _deleted = pipe.execute()
            if raw is None:
                return None
            model = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            return json.loads(model["value"])
        except Exception as error:  # noqa: BLE001 - use the API instead
            self.logger.debug(f"Atomic stash take unavailable ({type(error).__name__}); using the stash API")

        value = stash_get(key, scope=self.scope)
        if value is None:
            return None
        # Only delete what was read: if a producer wrote a new value meanwhile, leave it for the next poll
        # (the stash has no atomic take; this narrows the window to between these two calls)
        if stash_get(key, scope=self.scope) != value:
            return None
        stash_delete(key, scope=self.scope)
        return value

    def check_cmd(self, command):
        """Problems COSMOS would have with a command, without sending it (for dry runs)."""
        from openc3.models.target_model import TargetModel
        from openc3.utilities.extract import extract_fields_from_cmd_text

        try:
            target_name, cmd_name, params = extract_fields_from_cmd_text(command)
        except Exception as error:  # noqa: BLE001
            return [f"Can't parse the command: {error}"]
        params = {k.upper(): v for k, v in params.items()}
        try:
            packet = TargetModel.packet(target_name.upper(), cmd_name.upper(), type="CMD", scope=self.scope)
        except Exception:  # noqa: BLE001
            return [f"Unknown command {target_name} {cmd_name}"]
        items = {item["name"]: item for item in packet["items"]}
        problems = [f"Unknown parameter {name}" for name in params if name not in items]
        for name, item in items.items():
            if item.get("required") and name not in params:
                problems.append(f"Missing required parameter {name}")
            if name not in params:
                continue
            value = params[name]
            if isinstance(value, str):
                value = value.strip("'\"")
            states = item.get("states") or {}
            if states and isinstance(value, str):
                if value not in states:
                    problems.append(f"{name} = {value!r} is not one of {', '.join(states)}")
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                low, high = item.get("minimum"), item.get("maximum")
                if low is not None and high is not None and not (low <= value <= high):
                    problems.append(f"{name} = {value} is outside {low}..{high}")
        return problems

    def send_cmd(self, command, allow_hazardous=False):
        from openc3.api.cmd_api import cmd, cmd_no_hazardous_check
        from openc3.top_level import HazardousError

        from .runner import HazardousCommandError

        try:
            cmd(command, scope=self.scope)
        except HazardousError as error:
            if not allow_hazardous:
                raise HazardousCommandError(
                    f"Hazardous command blocked: {command}. In run mode, turn on 'Allow hazardous' on this node "
                    "to send it; in crawl and walk an operator approves it."
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


# A stash key nobody uses: deleting it is harmless, but COSMOS checks script_run first
WRITE_PROBE_KEY = "__flow_automator_permission_check__"


def token_identity(token):
    """The user a token belongs to. Enterprise tokens are JWTs naming the user; Core has one user."""
    parts = str(token or "").split(".")
    if len(parts) == 3:
        try:
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
            username = claims.get("preferred_username") or claims.get("sub")
            if username:
                return {"username": username, "name": claims.get("name") or username}
        except (ValueError, TypeError):
            pass
    return {"username": "anonymous", "name": "Anonymous"}


class TokenAuthorizer:
    """Checks a user's COSMOS token by making a harmless API call with it, and says who they are.

    Reads need COSMOS's 'tlm' permission (get_target_names). Changes need
    'script_run', the Script Runner role, because flows send commands and run
    scripts: the check deletes a stash key that doesn't exist. Core has a
    single user, so any valid token passes both. The token is verified by
    COSMOS before its claims are trusted for the username.
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
        key = (token, bool(write))
        now = time.monotonic()
        with self.lock:
            cached = self.cache.get(key)
            if cached and cached[0] > now:
                return cached[1]
        method, params = ("stash_delete", [WRITE_PROBE_KEY]) if write else ("get_target_names", [])
        try:
            response = requests.post(
                self.api_url,
                json={
                    "jsonrpc": "2.0",
                    "method": method,
                    "params": params,
                    "id": 1,
                    "keyword_params": {"scope": self.scope},
                },
                headers={"Authorization": token, "Content-Type": "application/json-rpc"},
                timeout=self.timeout,
            )
        except requests.RequestException:
            # Not the exception text: it names internal hosts and ports
            raise AuthError("Could not verify the token: the COSMOS API didn't answer", 503) from None
        if response.status_code in (401, 403):
            raise AuthError(
                "Not authorized" if not write else "Changing flows needs the script_run permission",
                response.status_code,
            )
        try:
            body = response.json()
        except ValueError:
            raise AuthError(f"Could not verify token (HTTP {response.status_code})", 503) from None
        error = body.get("error")
        if error:
            message = error.get("message", "Not authorized") if isinstance(error, dict) else str(error)
            forbidden = "permission" in message.lower() or "forbidden" in message.lower()
            if forbidden and write:
                message = f"Changing flows needs the script_run permission ({message})"
            raise AuthError(message, 403 if forbidden else 401)
        user = token_identity(token)
        with self.lock:
            if len(self.cache) > 1000:
                self.cache.clear()
            self.cache[key] = (now + TOKEN_CACHE_SECONDS, user)
        return user
