"""The flow engine: owns the flows, starts runs, applies policies and safety controls."""

import collections
import copy
import threading
import time
import uuid

from .audit import AuditLog, user_name
from .contacts import ContactPlanner
from .graph import MODES, POLICIES, is_trigger, validate_flow
from .runner import FlowRun
from .templates import CONTACT_VARIABLES, fill_parameters
from .triggers import TriggerManager


MAX_ACTIVE_RUNS = 50
MAX_QUEUED_PER_FLOW = 10
MAX_SUBFLOW_DEPTH = 10


class EngineError(Exception):
    status = 400

    def __init__(self, message, problems=None):
        super().__init__(message)
        self.problems = problems or []


class NotFoundError(EngineError):
    status = 404


class FlowEngine:
    def __init__(self, store, backend, logger=None, trigger_period=0.25, planner_options=None):
        self.store = store
        self.backend = backend
        self.logger = logger
        self.flows = {}
        self.runs = {}
        self.queues = collections.defaultdict(collections.deque)
        self.lock = threading.RLock()
        self.triggers = TriggerManager(self, period=trigger_period)
        self.audit = AuditLog(store, self.log)
        self.planner = ContactPlanner(self, store, **(planner_options or {}))

    def start(self, start_triggers=True):
        with self.lock:
            self.flows = self.store.all_flows()
        armed = [flow["name"] for flow in self.flows.values() if flow.get("armed")]
        self.log("info", f"Flow engine loaded {len(self.flows)} flows ({len(armed)} armed)")
        try:
            self.planner.load()
        except Exception as error:  # noqa: BLE001 - flows still run without contact planning
            self.log("error", f"Contact planner could not load: {error}")
        if start_triggers:
            self.triggers.start()
            self.planner.start()

    def shutdown(self):
        self.triggers.stop()
        self.planner.stop()
        with self.lock:
            runs = list(self.runs.values())
        for run in runs:
            run.stop()
        for run in runs:
            if run.thread:
                run.thread.join(timeout=2)

    def log(self, level, message):
        if self.logger:
            getattr(self.logger, {"warning": "warn"}.get(level, level), self.logger.info)(message)

    # ---- flows -----------------------------------------------------------

    def list_flows(self):
        with self.lock:
            flows = [copy.deepcopy(flow) for flow in self.flows.values()]
            active = collections.Counter(run.flow.get("id") for run in self.runs.values() if run.parent is None)
        result = []
        for flow in sorted(flows, key=lambda f: str(f.get("name", "")).lower()):
            last = self.store.runs(flow["id"], 1)
            result.append(
                {
                    "id": flow["id"],
                    "name": flow.get("name"),
                    "description": flow.get("description", ""),
                    "armed": bool(flow.get("armed")),
                    "policy": flow.get("policy", "skip"),
                    "mode": flow.get("mode", "run"),
                    "template": bool(flow.get("template")),
                    "parameters": copy.deepcopy(flow.get("parameters") or []),
                    "node_count": len(flow.get("nodes") or []),
                    "triggers": [n["type"] for n in flow.get("nodes") or [] if is_trigger(n)],
                    "problems": len(validate_flow(flow)),
                    "active_runs": active.get(flow["id"], 0),
                    "queued": len(self.queues.get(flow["id"], ())),
                    "updated_at": flow.get("updated_at"),
                    "last_run": {k: last[0].get(k) for k in ("id", "status", "started_at", "ended_at", "message")}
                    if last
                    else None,
                }
            )
        return result

    def get_flow(self, flow_id):
        with self.lock:
            flow = self.flows.get(flow_id)
            if flow is None:
                raise NotFoundError(f"Flow '{flow_id}' not found")
            return copy.deepcopy(flow)

    def save_flow(self, flow, flow_id=None):
        """Create (flow_id None) or replace a flow. Returns (flow, problems). Saving never arms a flow."""
        if not isinstance(flow, dict):
            raise EngineError("Flow must be a JSON object")
        flow = copy.deepcopy(flow)
        with self.lock:
            if flow_id is not None:
                existing = self.flows.get(flow_id)
                if existing is None:
                    raise NotFoundError(f"Flow '{flow_id}' not found")
                flow["id"] = flow_id
                flow["armed"] = bool(existing.get("armed"))
            else:
                flow["id"] = uuid.uuid4().hex[:12]
                flow["armed"] = False
                flow["created_at"] = time.time()
            flow["name"] = str(flow.get("name") or "Untitled flow").strip()
            flow.setdefault("description", "")
            if flow.get("mode") not in MODES:
                flow["mode"] = "run"  # flows saved before modes existed keep running unattended
            if flow.get("policy") not in POLICIES:
                flow["policy"] = "skip"
            flow.setdefault("variables", {})
            flow["template"] = bool(flow.get("template"))
            flow["parameters"] = list(flow.get("parameters") or []) if isinstance(flow.get("parameters"), list) else []
            flow["nodes"] = list(flow.get("nodes") or [])
            flow["edges"] = list(flow.get("edges") or [])
            problems = validate_flow(flow)
            if flow["armed"] and problems:
                flow["armed"] = False
                self.log("warn", f"Flow '{flow['name']}' was disarmed because it no longer validates")
            flow["updated_at"] = time.time()
            self.store.save_flow(flow)
            self.flows[flow["id"]] = flow
            return copy.deepcopy(flow), problems

    def delete_flow(self, flow_id):
        with self.lock:
            flow = self.flows.pop(flow_id, None)
            if flow is None:
                raise NotFoundError(f"Flow '{flow_id}' not found")
            self.queues.pop(flow_id, None)
            runs = [run for run in self.runs.values() if run.flow.get("id") == flow_id]
            self.store.delete_flow(flow_id)
        for run in runs:
            run.stop()
        self.log("info", f"Flow '{flow.get('name')}' deleted")

    def set_armed(self, flow_id, armed):
        with self.lock:
            flow = self.flows.get(flow_id)
            if flow is None:
                raise NotFoundError(f"Flow '{flow_id}' not found")
            if armed:
                problems = validate_flow(flow)
                if problems:
                    raise EngineError("Fix the flow's problems before arming it", problems)
            flow["armed"] = bool(armed)
            self.store.save_flow(flow)
        self.log("info", f"Flow '{flow['name']}' {'armed' if armed else 'disarmed'}")
        return copy.deepcopy(flow)

    def armed_flows(self):
        with self.lock:
            return [flow for flow in self.flows.values() if flow.get("armed")]

    # ---- runs ------------------------------------------------------------

    def trigger(self, flow_id, node_id=None, reason="", variables=None, manual=False, mode=None):
        """Start a run of a flow from a trigger node, applying the flow's re-trigger policy.

        Returns {"result": "started" | "queued" | "skipped", "run": run json or None}.
        Automatic triggers only start armed flows; manual runs work while disarmed (for testing).
        """
        with self.lock:
            flow = self.flows.get(flow_id)
            if flow is None:
                raise NotFoundError(f"Flow '{flow_id}' not found")
            if not manual and not flow.get("armed"):
                return {"result": "skipped", "run": None, "message": "Flow is not armed"}
            nodes = {node["id"]: node for node in flow.get("nodes") or []}
            if node_id is None:
                manual_triggers = [n for n in nodes.values() if n["type"] == "trigger_manual"]
                candidates = manual_triggers or [n for n in nodes.values() if is_trigger(n)]
                if not candidates:
                    raise EngineError("Flow has no trigger to start from")
                node_id = candidates[0]["id"]
            if node_id not in nodes or not is_trigger(nodes[node_id]):
                raise EngineError(f"'{node_id}' is not a trigger node of this flow")
            if manual:
                problems = validate_flow(flow)
                if problems:
                    raise EngineError("Fix the flow's problems before running it", problems)
            if mode is not None and mode not in MODES:
                raise EngineError(f"Mode must be one of {', '.join(MODES)}")
            variables, param_problems = fill_parameters(flow.get("parameters"), variables)
            if param_problems:
                raise EngineError("Parameters: " + "; ".join(param_problems))
            if flow.get("template"):
                for name in CONTACT_VARIABLES:
                    variables.setdefault(name, None)
            running = [r for r in self.runs.values() if r.flow.get("id") == flow_id and r.parent is None]
            if running:
                policy = flow.get("policy", "skip")
                if policy == "skip":
                    message = "Already running (policy: skip)"
                    if not manual:
                        self.log("info", f"Flow '{flow['name']}' trigger skipped: {message}")
                    return {"result": "skipped", "run": None, "message": message}
                if policy == "queue":
                    queue = self.queues[flow_id]
                    if len(queue) >= MAX_QUEUED_PER_FLOW:
                        message = f"Queue full ({MAX_QUEUED_PER_FLOW}), trigger dropped"
                        self.log("warn", f"Flow '{flow['name']}': {message}")
                        return {"result": "skipped", "run": None, "message": message}
                    queue.append((node_id, reason, variables, mode))
                    return {"result": "queued", "run": None, "message": f"Queued ({len(queue)} waiting)"}
            return self._start_run(flow, node_id, reason, variables, mode)

    def _start_run(self, flow, node_id, reason, variables, mode=None):
        if len(self.runs) >= MAX_ACTIVE_RUNS:
            message = f"Too many active runs ({MAX_ACTIVE_RUNS}), run not started"
            self.log("error", f"Flow '{flow['name']}': {message}")
            return {"result": "skipped", "run": None, "message": message}
        run = FlowRun(self, flow, node_id, reason, variables, mode=mode)
        self.runs[run.id] = run
        self.log("info", f"Flow '{flow['name']}' run {run.id} started: {reason}")
        run.start_thread()
        return {"result": "started", "run": run.as_json(include_steps=False), "message": ""}

    def start_scheduled_run(self, flow, reason, variables, mode, approval_deadline=None, activity_id=None):
        """Start a run of a flow snapshot for a scheduled activity. Re-trigger policies don't apply."""
        problems = validate_flow(flow)
        if problems:
            raise EngineError(f"The flow has problems: {problems[0]['message']}")
        entry = next((n for n in flow.get("nodes") or [] if n["type"] == "trigger_manual"), None)
        if entry is None:
            raise EngineError("The flow needs a Manual trigger to be scheduled")
        with self.lock:
            if len(self.runs) >= MAX_ACTIVE_RUNS:
                raise EngineError(f"Too many active runs ({MAX_ACTIVE_RUNS})")
            run = FlowRun(self, flow, entry["id"], reason, variables, mode=mode)
            run.approval_deadline = approval_deadline
            run.activity_id = activity_id
            self.runs[run.id] = run
        self.log("info", f"Flow '{flow.get('name')}' run {run.id} started: {reason}")
        run.start_thread()
        return run

    def run_subflow(self, parent, flow_id, variables):
        if parent.depth + 1 > MAX_SUBFLOW_DEPTH:
            raise RuntimeError(f"Sub-flows nested more than {MAX_SUBFLOW_DEPTH} deep")
        with self.lock:
            flow = self.flows.get(flow_id)
            if flow is None:
                raise RuntimeError(f"Sub-flow '{flow_id}' not found")
            entry = next((n for n in flow.get("nodes") or [] if n["type"] == "trigger_manual"), None)
            if entry is None:
                raise RuntimeError(f"Sub-flow '{flow.get('name')}' needs a Manual trigger to be called")
            problems = validate_flow(flow)
            if problems:
                raise RuntimeError(f"Sub-flow '{flow.get('name')}' has problems: {problems[0]['message']}")
            child = FlowRun(self, flow, entry["id"], f"Called from '{parent.flow.get('name')}'", variables, parent)
            self.runs[child.id] = child
            parent.children.append(child.id)
        child.execute()  # synchronous: the parent's step waits for it
        return child

    def on_run_finished(self, run):
        with self.lock:
            self.runs.pop(run.id, None)
            next_start = None
            if run.parent is None:
                queue = self.queues.get(run.flow.get("id"))
                flow = self.flows.get(run.flow.get("id"))
                if queue and flow:
                    next_start = (flow, *queue.popleft())
        try:
            self.store.add_run(run.flow.get("id"), run.as_json())
        except Exception as error:  # noqa: BLE001 - history is best effort
            self.log("error", f"Could not save run {run.id} history: {error}")
        level = {"success": "info", "stopped": "warn"}.get(run.status, "error")
        detail = f": {run.message}" if run.message else ""
        self.log(level, f"Flow '{run.flow.get('name')}' run {run.id} {run.status}{detail}")
        if getattr(run, "activity_id", None):
            try:
                self.planner.on_run_finished(run)
            except Exception as error:  # noqa: BLE001
                self.log("error", f"Could not record activity result for run {run.id}: {error}")
        if next_start:
            with self.lock:
                self._start_run(*next_start)

    def approve(self, run_id, approval_id, decision, user=None):
        """An operator answers a run's pending approval (crawl/walk modes)."""
        with self.lock:
            run = self.runs.get(run_id)
        if run is None:
            raise NotFoundError(f"Run '{run_id}' is not active")
        try:
            answered = run.approve(approval_id, decision, user_name(user))
        except ValueError as error:
            raise EngineError(str(error)) from None
        if not answered:
            raise NotFoundError(f"Approval '{approval_id}' is not pending (already answered or timed out)")
        self.log("info", f"Flow '{run.flow.get('name')}' run {run.id}: {user_name(user)} chose '{decision}'")
        return {"run": run.as_json(include_steps=False)}

    def stop_run(self, run_id):
        with self.lock:
            run = self.runs.get(run_id)
        if run is None:
            raise NotFoundError(f"Run '{run_id}' is not active")
        run.stop()

    def stop_all(self):
        """Safety stop: disarm every flow, drop queued triggers, pause scheduled activities, stop every run."""
        with self.lock:
            disarmed = 0
            for flow in self.flows.values():
                if flow.get("armed"):
                    flow["armed"] = False
                    self.store.save_flow(flow)
                    disarmed += 1
            self.queues.clear()
            runs = list(self.runs.values())
        for run in runs:
            run.stop()
        self.planner.set_paused(True, "STOP ALL")
        self.log("warn", f"STOP ALL: disarmed {disarmed} flows, paused the contact scheduler, stopped {len(runs)} runs")
        return {"disarmed": disarmed, "stopped": len(runs), "scheduler_paused": True}

    def get_run(self, run_id):
        with self.lock:
            run = self.runs.get(run_id)
            flow_ids = list(self.flows.keys())
        if run is not None:
            return run.as_json()
        for flow_id in flow_ids:
            for record in self.store.runs(flow_id):
                if record.get("id") == run_id:
                    return record
        raise NotFoundError(f"Run '{run_id}' not found")

    def list_runs(self, flow_id=None, limit=50):
        with self.lock:
            active = [r.as_json(include_steps=False) for r in self.runs.values()]
            flow_ids = [flow_id] if flow_id else list(self.flows.keys())
        if flow_id:
            active = [r for r in active if r["flow_id"] == flow_id]
        history = []
        for fid in flow_ids:
            for record in self.store.runs(fid, limit):
                record.pop("steps", None)
                history.append(record)
        active_ids = {r["id"] for r in active}
        runs = active + [r for r in history if r["id"] not in active_ids]
        runs.sort(key=lambda r: r.get("started_at") or 0, reverse=True)
        return runs[:limit]

    def status(self):
        contacts = self.planner.status()  # outside the engine lock: the planner calls into the engine
        with self.lock:
            return {
                "flows": len(self.flows),
                "armed": sum(1 for flow in self.flows.values() if flow.get("armed")),
                "active_runs": [run.as_json(include_steps=False) for run in self.runs.values()],
                "approvals": [a for run in self.runs.values() for a in run.pending_approvals()],
                "queued": {flow_id: len(queue) for flow_id, queue in self.queues.items() if queue},
                "triggers": self.triggers.status(),
                "contacts": contacts,
                "time": time.time(),
            }
