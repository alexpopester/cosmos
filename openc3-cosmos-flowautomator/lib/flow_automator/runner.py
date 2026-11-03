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
from .graph import ERROR_HANDLE, MODES, NODE_TYPES, Graph, is_trigger


MAX_STEPS_RECORDED = 1000
DEFAULT_MAX_STEPS = 100_000
SCRIPT_DONE_STATES = ("completed", "completed_errors", "stopped", "crashed", "killed")
SCRIPT_POLL_SECONDS = 1.0
APPROVAL_DECISIONS = ("go", "run", "abort")


class RunStoppedError(Exception):
    """The run was stopped by a user, STOP ALL or engine shutdown."""


class RunFailedError(Exception):
    """The run failed: an unhandled node error or an End node with status failed."""


class RunEndedError(Exception):
    """An End node finished the run successfully."""


def node_label(node):
    data = node.get("data") or {}
    return data.get("label") or node.get("type")


def strictest_mode(*modes):
    """The most cautious of the given execution modes (crawl > walk > run)."""
    return min((m for m in modes if m in MODES), key=MODES.index, default="run")


class FlowRun:
    def __init__(self, engine, flow, trigger_node_id, reason="", variables=None, parent=None, mode=None):
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
        # Execution mode: an explicit override for this run, else the flow's own.
        # A sub-flow runs at least as cautiously as the run that called it.
        self.mode = mode if mode in MODES else self.flow.get("mode", "run")
        if parent is not None:
            self.mode = strictest_mode(self.mode, parent.mode)
        self.approval_timeout = float(self.flow.get("approval_timeout") or 0)
        self.approvals = {}  # id -> pending approval awaiting an operator
        # Set when an operator chooses "go and run the rest": routine steps stop
        # asking, hazardous commands still do
        self.auto_approve = False
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
            self._gate(node, step)
            step["status"] = "running"
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

    # ---- operator approval (crawl / walk) --------------------------------

    def _gate(self, node, step):
        """Pause for operator approval if this run's mode requires it for this node."""
        if is_trigger(node):
            return
        data = node.get("data") or {}
        hazardous = False
        preview = ""
        if node["type"] == "send_cmd":
            # Rendering or the hazard lookup may fail; the executor reports that
            try:
                preview = self.evaluator().render(data.get("command"))
                hazardous = self.mode != "run" and bool(self.engine.backend.cmd_hazardous(preview))
            except Exception:  # noqa: BLE001
                hazardous = False
        reasons = []
        if self.auto_approve:
            pass
        elif self.mode == "crawl":
            reasons.append("crawl mode: every step is approved")
        elif self.mode == "walk" and data.get("require_approval"):
            reasons.append("marked for approval")
        if hazardous:
            reasons.append("HAZARDOUS command")
        if not reasons:
            return
        decision = self._wait_for_approval(node, step, "; ".join(reasons), preview or _preview(self, node, data))
        if hazardous:
            # The operator's approval is the hazardous acknowledgement
            step["approved_hazardous"] = True
        if decision == "run":
            self.auto_approve = True
            self.log("info", "operator chose to run the rest (hazardous commands still ask)")

    def _wait_for_approval(self, node, step, reason, preview):
        approval = {
            "id": uuid.uuid4().hex[:8],
            "run_id": self.id,
            "flow_id": self.flow.get("id"),
            "flow_name": self.flow.get("name"),
            "node": node["id"],
            "label": node_label(node),
            "type": node["type"],
            "reason": reason,
            "preview": preview,
            "requested_at": time.time(),
            "event": threading.Event(),
            "decision": None,
        }
        step["status"] = "waiting"
        step["message"] = f"Waiting for operator: {reason}"
        with self._lock:
            self.approvals[approval["id"]] = approval
        self.log("info", f"waiting for operator approval at '{approval['label']}' ({reason})")
        deadline = time.monotonic() + self.approval_timeout if self.approval_timeout > 0 else None
        try:
            while not approval["event"].wait(0.1):
                if self.cancelled():
                    raise RunStoppedError()
                if deadline is not None and time.monotonic() > deadline:
                    raise RunFailedError(
                        f"No operator approval for '{approval['label']}' within {self.approval_timeout:g}s"
                    )
        finally:
            with self._lock:
                self.approvals.pop(approval["id"], None)
        decision = approval["decision"]
        step["message"] = f"Operator chose {decision}"
        if decision == "abort":
            self.message = f"Aborted by operator at '{approval['label']}'"
            self._cancel.set()
            raise RunStoppedError()
        return decision

    def approve(self, approval_id, decision):
        """Answer a pending approval: 'go', 'run' (go, and stop asking) or 'abort'."""
        if decision not in APPROVAL_DECISIONS:
            raise ValueError(f"Decision must be one of {', '.join(APPROVAL_DECISIONS)}")
        with self._lock:
            approval = self.approvals.get(approval_id)
        if approval is None or approval["decision"] is not None:
            return False
        approval["decision"] = decision
        if decision == "run":
            # Release other branches waiting on routine steps right now too
            self.auto_approve = True
            with self._lock:
                others = [
                    a
                    for a in self.approvals.values()
                    if a is not approval and a["decision"] is None and "HAZARDOUS" not in a["reason"]
                ]
            for other in others:
                other["decision"] = "go"
                other["event"].set()
        approval["event"].set()
        return True

    def pending_approvals(self):
        with self._lock:
            # Answered approvals linger until their thread wakes; they're no longer pending
            return [
                {k: v for k, v in a.items() if k != "event"} for a in self.approvals.values() if a["decision"] is None
            ]

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
                "mode": self.mode,
                "auto_approve": self.auto_approve,
                "approvals": self.pending_approvals(),
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


def _preview(run, node, data):
    """What a node is about to do, shown to the operator approving it."""
    try:
        kind = node["type"]
        if kind == "run_script":
            return f"Run script {run.evaluator().render(data.get('filename'))}"
        if kind == "set_tlm":
            return f"Set {run.evaluator().render(data.get('item'))} = {run.render_value(data.get('value'))!r}"
        if kind == "set_var":
            return f"{data.get('name')} = {data.get('expression')}"
        if kind in ("if", "wait_tlm", "wait_check") or (kind == "loop" and data.get("mode") == "while"):
            return str(data.get("condition"))
        if kind == "switch":
            return str(data.get("expression"))
        if kind == "wait_time":
            return f"Wait {data.get('seconds')}s"
        if kind == "log":
            return run.evaluator().render(data.get("message") or "")
        if kind == "subflow":
            return f"Run sub-flow {data.get('flow_id')}"
        if kind == "end":
            return f"End run ({data.get('status', 'success')})"
    except Exception as error:  # noqa: BLE001 - a preview must never block approval
        return f"(preview unavailable: {error})"
    return node_label(node)


def exec_send_cmd(run, node, data, step):
    command = run.evaluator().render(data.get("command"))
    step["message"] = command
    allow = bool(data.get("allow_hazardous")) or bool(step.get("approved_hazardous"))
    run.engine.backend.send_cmd(command, allow_hazardous=allow)
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
