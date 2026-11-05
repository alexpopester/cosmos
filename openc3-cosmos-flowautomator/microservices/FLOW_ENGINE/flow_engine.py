import os


# Script Runner calls authenticate with OPENC3_API_PASSWORD, which COSMOS
# itself sets to the service password when it starts scripts. It must be set
# before openc3 is imported because openc3.environment reads it at import time.
if not os.environ.get("OPENC3_API_PASSWORD") and os.environ.get("OPENC3_SERVICE_PASSWORD"):
    os.environ["OPENC3_API_PASSWORD"] = os.environ["OPENC3_SERVICE_PASSWORD"]

# Add any plugin lib directories to the Python search path so this microservice
# can import the engine from this plugin's /lib folder
import glob

from openc3.top_level import add_to_search_path


for path in glob.glob("/gems/gems/**/lib"):
    add_to_search_path(path, True)

import valkey
from flow_automator.cosmos_backend import CosmosBackend, TokenAuthorizer
from flow_automator.engine import FlowEngine
from flow_automator.http_api import FlowApi, FlowHttpServer
from flow_automator.store import FlowStore
from valkey.backoff import ExponentialBackoff
from valkey.exceptions import BusyLoadingError
from valkey.retry import Retry

from openc3.environment import (
    OPENC3_API_HOSTNAME,
    OPENC3_API_PORT,
    OPENC3_API_SCHEMA,
    OPENC3_REDIS_HOSTNAME,
    OPENC3_REDIS_PASSWORD,
    OPENC3_REDIS_PORT,
    OPENC3_REDIS_USERNAME,
)
from openc3.microservices.microservice import Microservice
from openc3.utilities.logger import Logger
from openc3.utilities.sleeper import Sleeper


VALKEY_TIMEOUT_SECONDS = 5


def valkey_client():
    """The engine's own Valkey client: like COSMOS's, but a call that gets no answer fails after a few seconds.

    COSMOS's shared client has no socket timeout and retries for up to ~9 s, so a paused or partitioned Valkey
    would hang the engine (and STOP ALL) instead of failing over to its outage buffer (D28).
    """
    return valkey.Valkey(
        host=OPENC3_REDIS_HOSTNAME.replace("SHARDNUM", "0"),
        port=OPENC3_REDIS_PORT,
        username=OPENC3_REDIS_USERNAME,
        password=OPENC3_REDIS_PASSWORD,
        socket_timeout=VALKEY_TIMEOUT_SECONDS,
        socket_connect_timeout=VALKEY_TIMEOUT_SECONDS,
        retry=Retry(ExponentialBackoff(cap=1, base=0.2), 1),
        retry_on_error=[BusyLoadingError, valkey.exceptions.ConnectionError, valkey.exceptions.TimeoutError],
        health_check_interval=30,
    )


class FlowEngineMicroservice(Microservice):
    def __init__(self, name):
        super().__init__(name)
        self.trigger_period = 0.25
        self.history = 200
        self.planner_options = {}
        planner_keys = {
            "CONTACT_HORIZON_DAYS": "horizon_days",
            "CONTACT_RECOMPUTE_HOURS": "recompute_hours",
            "CONTACT_RETIME_TOLERANCE": "retime_tolerance",
            "STALE_TLE_DAYS": "stale_tle_days",
            "ACTIVITY_RETENTION_DAYS": "retention_days",
        }
        for option in self.config.get("options", []):
            match option[0].upper():
                case "TRIGGER_PERIOD":
                    self.trigger_period = float(option[1])
                case "HISTORY":
                    self.history = int(option[1])
                case key if key in planner_keys:
                    self.planner_options[planner_keys[key]] = float(option[1])
                case _:
                    self.logger.error(f"Unknown option passed to microservice {name}: {option}")
        ports = self.config.get("ports") or [[7790]]
        self.port = int(ports[0][0])
        self.prefix = self.config.get("prefix") or "/flow-automator"
        self.sleeper = Sleeper()

        store = FlowStore(valkey_client(), self.scope, history_limit=self.history)
        self.engine = FlowEngine(
            store,
            CosmosBackend(self.scope, self.logger),
            self.logger,
            self.trigger_period,
            self.planner_options,
            notify=self.notify,
        )
        api_url = f"{OPENC3_API_SCHEMA}://{OPENC3_API_HOSTNAME}:{OPENC3_API_PORT}"
        api = FlowApi(self.engine, TokenAuthorizer(self.scope, api_url), self.prefix)
        self.http = FlowHttpServer(api, self.port, self.logger)

    def notify(self, level, message, url=None):
        """Post to the COSMOS notification bell (alerts also pop up as toasts in every tool)."""
        if level == "alert":
            self.logger.warn(message, scope=self.scope, type=Logger.ALERT, url=url)
        else:
            self.logger.info(message, scope=self.scope, type=Logger.NOTIFICATION, url=url)

    def run(self):
        # Valkey may still be starting (or briefly away): keep trying rather than exit and lose the flows' triggers
        while not self.cancel_thread:
            try:
                self.engine.start()
                break
            except Exception as error:  # noqa: BLE001
                self.state = f"WAITING FOR VALKEY ({error})"
                self.logger.error(f"Flow Automator engine couldn't load its flows, retrying in 10 s: {error}")
                if self.sleeper.sleep(10):
                    return
        self.http.start()
        self.logger.info(f"Flow Automator engine listening on port {self.port} at {self.prefix}")
        while not self.cancel_thread:
            scheduled = self.engine.planner.status()["scheduled"]
            self.state = (
                f"RUNNING ({len(self.engine.armed_flows())} armed, {len(self.engine.runs)} active runs, "
                f"{scheduled} scheduled activities)"
            )
            if self.sleeper.sleep(5):
                break
            self.count += 1

    def shutdown(self):
        self.sleeper.cancel()
        self.http.stop()
        self.engine.shutdown()
        super().shutdown()


if __name__ == "__main__":
    FlowEngineMicroservice.class_run()
