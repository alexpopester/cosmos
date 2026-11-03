import os
import sys
import threading
import time

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from flow_automator.engine import FlowEngine  # noqa: E402
from flow_automator.store import FlowStore, MemoryRedis  # noqa: E402


class FakeBackend:
    """Stands in for COSMOS: a dict of telemetry, recorded commands and scripted Script Runner states."""

    def __init__(self):
        self.values = {}  # "TGT PKT ITEM" -> value
        self.limits = {}  # "TGT PKT ITEM" -> limits state
        self.counts = {}  # (TGT, PKT) -> received count
        self.commands = []
        self.hazardous = set()  # command names that are hazardous
        self.fail_commands = set()
        self.scripts = {}  # id -> list of states to report, last one sticks
        self.started_scripts = []
        self.stopped_scripts = []
        self.set_calls = []
        self.lock = threading.Lock()

    def tlm(self, item, value_type="CONVERTED"):
        if item not in self.values:
            raise RuntimeError(f"Item '{item}' does not exist")
        return self.values[item]

    def tlm_values(self, refs):
        return [[self.tlm(item, value_type), self.limits.get(item)] for item, value_type in refs]

    def tlm_counts(self, packets):
        return [self.counts.get(tuple(packet), 0) for packet in packets]

    def send_cmd(self, command, allow_hazardous=False):
        name = command.split()[1] if len(command.split()) > 1 else command
        if name in self.fail_commands:
            raise RuntimeError(f"Command {name} failed")
        if name in self.hazardous and not allow_hazardous:
            raise RuntimeError(f"Hazardous command blocked: {command}")
        with self.lock:
            self.commands.append(command)

    def set_tlm(self, item, value, value_type="CONVERTED"):
        self.set_calls.append((item, value, value_type))
        self.values[item] = value

    def script_start(self, filename, environment):
        script_id = len(self.started_scripts) + 1
        self.started_scripts.append((filename, environment))
        self.scripts.setdefault(script_id, ["running", "completed"])
        return script_id

    def script_state(self, script_id):
        states = self.scripts[script_id]
        return states.pop(0) if len(states) > 1 else states[0]

    def script_stop(self, script_id):
        self.stopped_scripts.append(script_id)
        self.scripts[script_id] = ["stopped"]


class ListLogger:
    def __init__(self):
        self.messages = []

    def _log(self, level, message):
        self.messages.append((level, message))

    def info(self, message):
        self._log("info", message)

    def warn(self, message):
        self._log("warn", message)

    def error(self, message):
        self._log("error", message)


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def engine(backend):
    engine = FlowEngine(FlowStore(MemoryRedis(), "DEFAULT"), backend, ListLogger(), trigger_period=0.05)
    engine.start(start_triggers=False)
    yield engine
    engine.shutdown()


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def node(node_id, node_type, **data):
    return {"id": node_id, "type": node_type, "position": {"x": 0, "y": 0}, "data": data}


def edge(source, target, handle="out"):
    return {"id": f"{source}-{handle}-{target}", "source": source, "sourceHandle": handle, "target": target}


def make_flow(nodes, edges, **extra):
    flow = {"name": extra.pop("name", "Test flow"), "nodes": nodes, "edges": edges}
    flow.update(extra)
    return flow


def run_to_end(engine, flow, timeout=5.0, **kwargs):
    """Save a flow, run it manually and wait for the run to finish. Returns the run record."""
    saved, problems = engine.save_flow(flow)
    assert problems == [], problems
    result = engine.trigger(saved["id"], manual=True, **kwargs)
    assert result["result"] == "started", result
    run_id = result["run"]["id"]
    assert wait_until(lambda: run_id not in engine.runs, timeout), "run did not finish"
    return engine.get_run(run_id)
