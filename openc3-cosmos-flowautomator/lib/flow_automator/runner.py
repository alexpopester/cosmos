"""Executes one run of a flow, node by node.

A run starts at a trigger node and follows wires. Each node returns the name
of the output handle to follow. Errors follow the node's ``error`` output if
it is wired, otherwise they fail the run. A ``parallel`` node runs each of its
outgoing wires in its own thread until every branch reaches the same ``join``
node (or ends), then carries on from that join.
"""

import contextlib
import copy
import threading
import time
import uuid

from .expressions import _TEMPLATE_RE, Evaluator, normalize_item
from .graph import ERROR_HANDLE, NODE_TYPES, Graph


MAX_STEPS_RECORDED = 1000
DEFAULT_MAX_STEPS = 100_000
SCRIPT_DONE_STATES = ("completed", "completed_errors", "stopped", "crashed", "killed")
SCRIPT_POLL_SECONDS = 1.0


class RunStoppedError(Exception):
    """The run was stopped by a user, STOP ALL or engine shutdown."""


class RunFailedError(Exception):
    """The run failed: an unhandled node error or an End node with status failed."""


class RunEndedError(Exception):
    """An End node finished the run successfully."""


def node_label(node):
    data = node.get("data") or {}
    return data.get("label") or node.get("type")


class FlowRun:
    def __init__(self, engine, flow, trigger_node_id, reason="", variables=None, parent=None):
        self.engine = engine
        self.flow = copy.deepcopy(flow)
        self.graph = Graph(self.flow)
        self.id = uuid.uuid4().hex[:12]
        self.trigger_node_id = trigger_node_id
        self.reason = reason
        self.parent = parent
        self.depth = parent.depth + 1 if parent else 0
        self.variables = dict(self.flow.get("variables") or {})
        self.variables.update(variables or {})
        self.status = "pending"
        self.message = ""
        self.started_at = None
        self.ended_at = None
        self.steps = []
        self.steps_dropped = 0
        self.step_count = 0
        self.max_steps = int(self.flow.get("max_steps") or DEFAULT_MAX_STEPS)
        self.active = {}
        self.children = []
        self.loop_counters = {}
        self._cancel = threading.Event()
        self._lock = threading.RLock()
        self.thread = None

    # ---- control -------------------------------------------------------

    def stop(self):
        self._cancel.set()

    def cancelled(self):
        return self._cancel.is_set() or (self.parent is not None and self.parent.cancelled())

    def sleep(self, seconds):
        """Sleep that wakes up early (raising RunStoppedError) when the run is stopped."""
        deadline = time.monotonic() + max(0.0, float(seconds))
        while True:
            if self.cancelled():
                raise RunStoppedError()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._cancel.wait(min(remaining, 0.1))

    def start_thread(self):
        self.thread = threading.Thread(target=self.execute, name=f"flow-run-{self.id}", daemon=True)
        self.thread.start()

    # ---- execution -----------------------------------------------------

    def execute(self):
        self.started_at = time.time()
        self.status = "running"
        try:
            trigger = self.graph.nodes.get(self.trigger_node_id)
            if trigger is None:
                raise RunFailedError(f"Start node '{self.trigger_node_id}' not found")
            self._walk(trigger, in_parallel=False)
            self.status = "success"
        except RunEndedError as ended:
            self.status = "success"
            self.message = str(ended)
        except RunStoppedError:
            self.status = "stopped"
            self.message = self.message or "Stopped"
        except RunFailedError as error:
            self.status = "failed"
            self.message = str(error)
        except Exception as error:  # noqa: BLE001 - a run must never take down the engine
            self.status = "failed"
            self.message = f"Engine error: {type(error).__name__}: {error}"
        finally:
            self.ended_at = time.time()
            with self._lock:
                self.active.clear()
            self.engine.on_run_finished(self)

    def _walk(self, node, in_parallel):
        """Follow wires from node. Returns the join node reached when in a parallel branch, else None."""
        while node is not None:
            if self.cancelled():
                raise RunStoppedError()
            if node["type"] == "join" and in_parallel:
                return node
            self.step_count += 1
            if self.step_count > self.max_steps:
                raise RunFailedError(f"Run exceeded {self.max_steps} steps (is there an endless loop?)")
            if node["type"] == "parallel":
                self._record_instant(node, "out")
                join = self._run_parallel(node)
                if join is None:
                    return None
                self._record_instant(join, "out")
                node = self._next(join, "out")
                continue
            handle = self._execute_node(node)
            node = self._next(node, handle)
        return None

    def _next(self, node, handle):
        targets = self.graph.next_nodes(node["id"], handle)
        return targets[0] if targets else None

    def _run_parallel(self, node):
        branches = self.graph.next_nodes(node["id"], "out")
        results = [None] * len(branches)
        errors = [None] * len(branches)

        def run_branch(index, start):
            try:
                results[index] = self._walk(start, in_parallel=True)
            except BaseException as error:  # noqa: BLE001 - collected and re-raised below
                errors[index] = error
                if not isinstance(error, RunStoppedError):
                    # One branch failing or ending finishes the whole run
                    self._cancel.set()

        threads = [
            threading.Thread(target=run_branch, args=(i, start), name=f"flow-run-{self.id}-b{i}", daemon=True)
            for i, start in enumerate(branches)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # A real failure wins over an End node, which wins over the stops it caused
        for error in errors:
            if error is not None and not isinstance(error, (RunEndedError, RunStoppedError)):
                raise error
        for kind in (RunEndedError, RunStoppedError):
            for error in errors:
                if isinstance(error, kind):
                    raise error
        joins = {join["id"]: join for join in results if join is not None}
        if len(joins) > 1:
            raise RunFailedError(f"Branches of '{node_label(node)}' end at different Join nodes")
        return next(iter(joins.values()), None)

    def _execute_node(self, node):
        step = self._begin_step(node)
        try:
            handle = EXECUTORS[node["type"]](self, node, node.get("data") or {}, step)
            step["status"] = "done"
            step["output"] = handle
            return handle
        except (RunStoppedError, RunEndedError):
            step["status"] = "stopped" if self.cancelled() else "done"
            raise
        except RunFailedError as error:
            step["status"] = "error"
            step["message"] = str(error)
            raise
        except Exception as error:  # noqa: BLE001 - node errors are routed, not fatal to the engine
            message = str(error) or type(error).__name__
            step["status"] = "error"
            step["message"] = message
            if NODE_TYPES[node["type"]].get("errors") and self.graph.has_output(node["id"], ERROR_HANDLE):
                self.variables["error"] = message
                step["output"] = ERROR_HANDLE
                return ERROR_HANDLE
            raise RunFailedError(f"{node_label(node)}: {message}") from None
        finally:
            step["ended_at"] = time.time()
            with self._lock:
                count = self.active.get(node["id"], 0) - 1
                if count > 0:
                    self.active[node["id"]] = count
                else:
                    self.active.pop(node["id"], None)

    def _begin_step(self, node):
        step = {
            "node": node["id"],
            "type": node["type"],
            "label": node_label(node),
            "started_at": time.time(),
            "ended_at": None,
            "status": "running",
            "output": None,
            "message": "",
        }
        with self._lock:
            self.active[node["id"]] = self.active.get(node["id"], 0) + 1
            self.steps.append(step)
            if len(self.steps) > MAX_STEPS_RECORDED:
                self.steps.pop(0)
                self.steps_dropped += 1
        return step

    def _record_instant(self, node, output):
        step = self._begin_step(node)
        step.update(status="done", output=output, ended_at=step["started_at"])
        with self._lock:
            self.active.pop(node["id"], None)

    # ---- helpers for executors -------------------------------------------

    def evaluator(self):
        return Evaluator(self.variables, self.engine.backend.tlm)

    def render_value(self, template):
        """Render a template; a lone ``{{ expr }}`` keeps the expression's type, otherwise numbers are parsed."""
        if not isinstance(template, str):
            return template
        stripped = template.strip()
        match = _TEMPLATE_RE.fullmatch(stripped)
        if match:
            return self.evaluator().evaluate(match.group(1))
        text = self.evaluator().render(template)
        for convert in (int, float):
            try:
                return convert(text)
            except ValueError:
                pass
        return text

    def log(self, level, message):
        self.engine.log(level, f"Flow '{self.flow.get('name')}' run {self.id}: {message}")

    def as_json(self, include_steps=True):
        with self._lock:
            result = {
                "id": self.id,
                "flow_id": self.flow.get("id"),
                "flow_name": self.flow.get("name"),
                "parent_id": self.parent.id if self.parent else None,
                "trigger_node": self.trigger_node_id,
                "reason": self.reason,
                "status": self.status,
                "message": self.message,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "active_nodes": list(self.active.keys()),
                "children": list(self.children),
                "variables": _json_safe(self.variables),
                "step_count": self.step_count,
                "steps_dropped": self.steps_dropped,
            }
            if include_steps:
                result["steps"] = [dict(step) for step in self.steps]
        return result


def _json_safe(value):
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


# ---- node executors ---------------------------------------------------------
# Each takes (run, node, data, step) and returns the output handle to follow.


def _float(data, key, default):
    value = data.get(key)
    if value in (None, ""):
        return float(default)
    return float(value)


def exec_trigger(run, node, data, step):
    step["message"] = run.reason
    return "out"


def exec_if(run, node, data, step):
    result = run.evaluator().truthy(data.get("condition"))
    step["message"] = f"{data.get('condition')} -> {result}"
    return "true" if result else "false"


def exec_switch(run, node, data, step):
    evaluator = run.evaluator()
    value = evaluator.evaluate(data.get("expression"))
    for index, case in enumerate(data.get("cases") or []):
        if evaluator.evaluate(str(case)) == value:
            step["message"] = f"{value!r} matched case {case}"
            return f"case_{index}"
    step["message"] = f"{value!r} matched no case"
    return "default"


def exec_loop(run, node, data, step):
    node_id = node["id"]
    iteration = run.loop_counters.get(node_id, 0)
    variable = data.get("variable") or "i"
    max_iterations = int(data.get("max_iterations") or 10_000)
    if data.get("mode", "count") == "while":
        keep_going = run.evaluator().truthy(data.get("condition"))
        if keep_going and iteration >= max_iterations:
            raise RuntimeError(f"Loop exceeded {max_iterations} iterations")
    else:
        keep_going = iteration < int(float(data.get("count") or 0))
    if keep_going:
        run.loop_counters[node_id] = iteration + 1
        run.variables[variable] = iteration
        step["message"] = f"{variable} = {iteration}"
        return "body"
    run.loop_counters.pop(node_id, None)
    step["message"] = f"done after {iteration} iterations"
    return "done"


def exec_passthrough(run, node, data, step):
    return "out"


def exec_set_var(run, node, data, step):
    value = run.evaluator().evaluate(data.get("expression"))
    run.variables[data["name"]] = value
    step["message"] = f"{data['name']} = {value!r}"
    return "out"


def exec_send_cmd(run, node, data, step):
    command = run.evaluator().render(data.get("command"))
    step["message"] = command
    run.engine.backend.send_cmd(command, allow_hazardous=bool(data.get("allow_hazardous")))
    run.variables["last_command"] = command
    return "out"


def exec_run_script(run, node, data, step):
    backend = run.engine.backend
    filename = run.evaluator().render(data.get("filename"))
    environment = {str(k): str(run.render_value(v)) for k, v in (data.get("environment") or {}).items()}
    script_id = backend.script_start(filename, environment)
    run.variables["script_id"] = script_id
    step["message"] = f"Started {filename} as script {script_id}"
    if not data.get("wait", True):
        return "out"
    timeout = _float(data, "timeout", 0)
    deadline = time.monotonic() + timeout if timeout > 0 else None
    while True:
        try:
            run.sleep(SCRIPT_POLL_SECONDS)
        except RunStoppedError:
            _stop_script(backend, script_id)
            raise
        state = backend.script_state(script_id)
        if state in SCRIPT_DONE_STATES:
            break
        if deadline is not None and time.monotonic() > deadline:
            _stop_script(backend, script_id)
            raise RuntimeError(f"Script {filename} ({script_id}) timed out after {timeout:g}s")
    run.variables["script_state"] = state
    step["message"] = f"{filename} ({script_id}) {state}"
    if state == "completed" or (state == "completed_errors" and data.get("allow_errors")):
        return "out"
    raise RuntimeError(f"Script {filename} ({script_id}) finished as {state}")


def _stop_script(backend, script_id):
    # Best effort: the script may already be done
    with contextlib.suppress(Exception):
        backend.script_stop(script_id)


def exec_set_tlm(run, node, data, step):
    item = normalize_item(run.evaluator().render(data.get("item")))
    value = run.render_value(data.get("value"))
    value_type = (data.get("value_type") or "CONVERTED").upper()
    run.engine.backend.set_tlm(item, value, value_type)
    step["message"] = f"{item} = {value!r} ({value_type})"
    return "out"


def exec_log(run, node, data, step):
    message = run.evaluator().render(data.get("message") or "")
    level = (data.get("level") or "info").lower()
    run.log(level, message)
    step["message"] = message
    return "out"


def exec_wait_time(run, node, data, step):
    seconds = _float(data, "seconds", 0)
    step["message"] = f"{seconds:g}s"
    run.sleep(seconds)
    return "out"


def _wait_for(run, data, step):
    """Poll a condition until true or timeout. Returns True if it became true."""
    condition = data.get("condition")
    timeout = _float(data, "timeout", 10)
    poll = max(0.05, _float(data, "poll", 0.25))
    deadline = time.monotonic() + timeout
    start = time.monotonic()
    while True:
        if run.evaluator().truthy(condition):
            step["message"] = f"{condition} true after {time.monotonic() - start:.2f}s"
            return True
        if time.monotonic() >= deadline:
            step["message"] = f"{condition} still false after {timeout:g}s"
            return False
        run.sleep(min(poll, max(0.0, deadline - time.monotonic())))


def exec_wait_tlm(run, node, data, step):
    return "ok" if _wait_for(run, data, step) else "timeout"


def exec_wait_check(run, node, data, step):
    if _wait_for(run, data, step):
        return "out"
    raise RuntimeError(f"Wait check failed: {step['message']}")


def exec_subflow(run, node, data, step):
    child = run.engine.run_subflow(run, data["flow_id"], dict(run.variables))
    step["message"] = f"Sub-flow '{child.flow.get('name')}' run {child.id} {child.status}"
    if child.status == "stopped":
        raise RunStoppedError()
    if child.status != "success":
        raise RuntimeError(f"Sub-flow '{child.flow.get('name')}' failed: {child.message}")
    if data.get("pass_variables", True):
        run.variables.update(child.variables)
    return "out"


def exec_end(run, node, data, step):
    message = run.evaluator().render(data.get("message") or "")
    step["message"] = message
    if data.get("status", "success") == "failed":
        raise RunFailedError(message or f"Ended at '{node_label(node)}'")
    raise RunEndedError(message)


EXECUTORS = {
    "trigger_manual": exec_trigger,
    "trigger_tlm": exec_trigger,
    "trigger_schedule": exec_trigger,
    "trigger_packet": exec_trigger,
    "trigger_limits": exec_trigger,
    "if": exec_if,
    "switch": exec_switch,
    "loop": exec_loop,
    "parallel": exec_passthrough,
    "join": exec_passthrough,
    "set_var": exec_set_var,
    "send_cmd": exec_send_cmd,
    "run_script": exec_run_script,
    "set_tlm": exec_set_tlm,
    "log": exec_log,
    "wait_time": exec_wait_time,
    "wait_tlm": exec_wait_tlm,
    "wait_check": exec_wait_check,
    "subflow": exec_subflow,
    "end": exec_end,
}
