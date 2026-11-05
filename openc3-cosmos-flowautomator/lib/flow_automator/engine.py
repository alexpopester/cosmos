"""The flow engine: owns the flows, starts runs, applies policies and safety controls."""

import collections
import copy
import threading
import time
import uuid

from .alerts import Alerter
from .audit import AuditLog, user_name
from .contacts import ContactPlanner
from .graph import MODES, POLICIES, _shape_problem, is_trigger, validate_flow
from .migrations import FORMAT_VERSION, NewerFormatError, upgrade
from .runner import SCRIPT_DONE_STATES, FlowRun
from .templates import CONTACT_VARIABLES, fill_parameters
from .triggers import TriggerManager
from .versions import FlowVersions, content, diff_flows


MAX_ACTIVE_RUNS = 50
MAX_QUEUED_PER_FLOW = 10
MAX_SUBFLOW_DEPTH = 10
# Threads running Parallel branches across every run; with MAX_ACTIVE_RUNS (which counts sub-flow runs too)
# this bounds what a flow can fan out to, e.g. a sub-flow that calls itself from a Parallel
MAX_BRANCH_THREADS = 200
SHUTDOWN_WAIT_SECONDS = 5.0  # all runs together, not each
STORE_FLUSH_SECONDS = 2.0  # how often writes Valkey refused are retried
SCRIPT_STOP_WAIT_SECONDS = 10.0  # STOP ALL waits this long (in all) for Script Runner to stop scripts
DETACHED_PRUNE_SECONDS = 60.0  # how often finished fire-and-forget scripts are forgotten
MAX_DETACHED_SCRIPTS = 1000
RECENTLY_FINISHED = 50  # finished runs kept in memory until their history is surely in Valkey


# Nodes that act on the system: adding one to an armed flow is a change someone should arm knowingly
ACTING_NODE_TYPES = ("send_cmd", "run_script", "set_tlm", "subflow")
# What each acting node does, by data key: changing it on a node allowed to bypass checks needs a re-arm
ACTION_KEYS = {"send_cmd": "command", "run_script": "filename", "set_tlm": "item", "subflow": "flow_id"}
# Flow keys only the engine (or the planner) sets: never taken from a client
SERVER_KEYS = ("subflow_snapshots", "created_by", "disarmed_reason")


def less_safe_changes(old, new):
    """What a save makes less safe, for an armed flow.

    A less cautious mode; dry run turned off; *Allow hazardous* or *Allow raw values* turned on; a new node
    that acts on the system; or a different action on a node allowed to bypass checks.
    """
    changes = []
    if MODES.index(new.get("mode", "run")) > MODES.index(old.get("mode", "run") if old.get("mode") in MODES else "run"):
        changes.append(f"mode {old.get('mode', 'run')} -> {new.get('mode')}")
    if old.get("dry_run") and not new.get("dry_run"):
        changes.append("dry run turned off")
    old_nodes = {n.get("id"): n for n in old.get("nodes") or []}
    for node in new.get("nodes") or []:
        data = node.get("data") or {}
        label = data.get("label") or node.get("id")
        previous = old_nodes.get(node.get("id"))
        before = (previous or {}).get("data") or {}
        kind = node.get("type")
        if kind in ACTING_NODE_TYPES and (previous is None or previous.get("type") != kind):
            changes.append(f"new {kind} node '{label}'")
            continue
        for key, name in (("allow_hazardous", "Allow hazardous"), ("allow_raw_values", "Allow raw values")):
            if data.get(key) and not before.get(key):
                changes.append(f"{name} on '{label}'")
        action = ACTION_KEYS.get(kind)
        bypasses = data.get("allow_hazardous") or data.get("allow_raw_values")
        if action and (bypasses or kind == "subflow") and data.get(action) != before.get(action):
            changes.append(f"{action} changed on '{label}'")
    return changes


class EngineError(Exception):
    status = 400

    def __init__(self, message, problems=None):
        super().__init__(message)
        self.problems = problems or []


class NotFoundError(EngineError):
    status = 404


class ConflictError(EngineError):
    """Saving over a version someone else saved after you opened the flow."""

    status = 409

    def __init__(self, message, **details):
        super().__init__(message)
        self.details = details


class FlowEngine:
    def __init__(self, store, backend, logger=None, trigger_period=0.25, planner_options=None, notify=None):
        """notify(level, message, url) posts a COSMOS notification (see alerts.py)."""
        self.store = store
        self.backend = backend
        self.logger = logger
        self.flows = {}
        self.runs = {}
        self.queues = collections.defaultdict(collections.deque)
        self.detached_scripts = []  # scripts runs started without waiting (pruned once they finish)
        self.finished = collections.OrderedDict()  # run id -> json of runs that just finished
        # Set by shutdown: nothing starts any more (queued runs included)
        self.halted = False
        self.branch_threads = 0  # Parallel branch threads running now, across every run
        self.lock = threading.RLock()
        self.triggers = TriggerManager(self, period=trigger_period)
        self.audit = AuditLog(store, self.log)
        self.versions = FlowVersions(store)
        self.alerts = Alerter(store, notify, self.log)
        self.planner = ContactPlanner(self, store, **(planner_options or {}))
        self._store_stop = threading.Event()
        self._store_thread = None
        self._store_failing = False

    def start(self, start_triggers=True):
        with self.lock:
            self.flows = self.store.all_flows()
            self._ensure_versions()
        self._check_format()
        self.alerts.load()
        self._recover_interrupted_runs()
        armed = [flow["name"] for flow in self.flows.values() if flow.get("armed")]
        self.log("info", f"Flow engine loaded {len(self.flows)} flows ({len(armed)} armed)")
        try:
            self.planner.load()
        except Exception as error:  # noqa: BLE001 - flows still run without contact planning
            self.log("error", f"Contact planner could not load: {error}")
        if start_triggers:
            # The store thread makes buffered writes from now on: a caller holding the engine lock never
            # waits on Valkey for a run's bookkeeping
            self.store.inline_flush = False
            self.triggers.start()
            self.planner.start()
            self.alerts.start()
            self._store_stop.clear()
            self._store_thread = threading.Thread(target=self._store_loop, name="flow-store", daemon=True)
            self._store_thread.start()

    def shutdown(self):
        # Nothing new starts, then runs stop (recording their history and alerts), then alerts stop
        with self.lock:
            self.halted = True
            self.queues.clear()
        self.triggers.stop()
        self.planner.stop()
        with self.lock:
            runs = list(self.runs.values())
        for run in runs:
            run.stop()
        deadline = time.monotonic() + SHUTDOWN_WAIT_SECONDS
        for run in runs:
            if run.thread:
                run.thread.join(timeout=max(0.0, deadline - time.monotonic()))
        self.alerts.stop()
        self._store_stop.set()
        self.store._wake.set()
        if self._store_thread:
            self._store_thread.join(timeout=5)
        self.store.flush()  # one last try at anything Valkey refused

    # ---- storage format and outages ----------------------------------------------

    def _check_format(self):
        stored = self.store.stored_format()
        if stored > FORMAT_VERSION:
            self.alerts.emit(
                "storage_format",
                "Flow Automator data is from a newer version",
                f"This scope's data was written by a Flow Automator that stores format {stored}; this one reads up "
                f"to {FORMAT_VERSION}. Records in the newer format are left untouched and aren't loaded: install "
                "the newer plugin again to use them.",
                key=f"storage_format:{stored}",
            )
        else:
            self.store.note_format()
        unreadable = self.store.unreadable
        if unreadable:
            kinds = collections.Counter(kind for kind, _id in unreadable)
            summary = ", ".join(f"{count} {kind}{'s' if count > 1 else ''}" for kind, count in sorted(kinds.items()))
            self.log("error", f"Not loaded, saved by a newer Flow Automator: {summary}")

    def _store_loop(self):
        last_prune = time.monotonic()
        while not self._store_stop.is_set():
            self.store.wait_for_writes(STORE_FLUSH_SECONDS)
            if self._store_stop.is_set():
                return
            try:
                self.store.flush()
            except Exception as error:  # noqa: BLE001 - keep retrying
                self.log("error", f"Retrying buffered writes failed: {error}")
            self._note_store_health()
            if time.monotonic() - last_prune >= DETACHED_PRUNE_SECONDS:
                last_prune = time.monotonic()
                self.prune_detached_scripts()

    def _note_store_health(self):
        health = self.store.health()
        failing = bool(health["error"])
        if failing and not self._store_failing:
            self.log("error", f"Valkey refused a write; keeping it in memory to retry: {health['error']}")
            self.alerts.emit(
                "storage_unavailable",
                "Flow Automator can't write to Valkey",
                "Run history, audit entries and schedule updates are kept in memory and written when Valkey is "
                "back. Restarting the engine before then loses them.",
                key=f"storage_unavailable:{health['failing_since']}",
            )
        elif not failing and self._store_failing:
            self.log("info", "Valkey is reachable again; buffered writes were saved")
            self.alerts.emit(
                "storage_recovered",
                "Flow Automator is writing to Valkey again",
                f"{health['dropped_writes']} buffered writes were dropped" if health["dropped_writes"] else "",
                key=f"storage_recovered:{time.time()}",
            )
        self._store_failing = failing

    # ---- runs interrupted by a crash ----------------------------------------

    def _remember_run(self, run):
        """Keep a small record of an active run, so a crash or restart can't make it vanish without trace.

        Deferred: callers hold the engine lock, and a run's start must not wait on Valkey.
        """
        try:
            self.store.save_record(
                "active_runs",
                run.id,
                {
                    "id": run.id,
                    "flow_id": run.flow.get("id"),
                    "flow_name": run.flow.get("name"),
                    "parent_id": run.parent.id if run.parent else None,
                    "started_at": run.started_at or time.time(),
                    "mode": run.mode,
                    "trigger": dict(run.trigger_info),
                    "flow_version": run.flow_version,
                    "dry_run": run.dry_run,
                    "activity_id": run.activity_id,
                    "scripts": list(run.scripts),
                },
                defer=True,
            )
        except Exception as error:  # noqa: BLE001 - best effort; the run itself goes on
            self.log("warn", f"Could not record active run {run.id}: {error}")

    def note_script(self, run, script_id):
        """A run started a Script Runner script: remember it so it can be stopped if the engine dies."""
        run.scripts.append(script_id)
        self._remember_run(run)

    def _recover_interrupted_runs(self):
        """Runs that were active when the engine last stopped without finishing them: record them as failed,
        stop the scripts they started, and alert about the unattended ones."""
        try:
            leftovers = self.store.records("active_runs")
        except Exception as error:  # noqa: BLE001
            self.log("error", f"Could not read interrupted runs: {error}")
            return
        now = time.time()
        # Stop every leftover script at once: one slow Script Runner call each would delay startup by minutes
        all_scripts = [sid for record in leftovers.values() for sid in record.get("scripts") or []]
        stopped = set(self._stop_scripts(all_scripts, collect=True)) if all_scripts else set()
        for record in leftovers.values():
            message = "The flow engine stopped while this run was active (restart or crash); it did not finish"
            for script_id in record.get("scripts") or []:
                if script_id in stopped:
                    message += f"; stopped script {script_id} it had started"
                else:
                    message += f"; couldn't confirm script {script_id} it had started was stopped (check Script Runner)"
            history = {
                "id": record["id"],
                "flow_id": record.get("flow_id"),
                "flow_name": record.get("flow_name"),
                "parent_id": record.get("parent_id"),
                "status": "failed",
                "message": message,
                "started_at": record.get("started_at"),
                "ended_at": now,
                "mode": record.get("mode"),
                "trigger": record.get("trigger") or {},
                "flow_version": record.get("flow_version"),
                "dry_run": bool(record.get("dry_run")),
                "activity_id": record.get("activity_id"),
                "interrupted": True,
                "active_nodes": [],
                "approvals": [],
                "children": [],
                "variables": {},
                "steps": [],
                "step_count": 0,
                "steps_dropped": 0,
            }
            try:
                self.store.add_run(record.get("flow_id"), history)
                self.store.delete_record("active_runs", record["id"])
            except Exception as error:  # noqa: BLE001
                self.log("error", f"Could not record interrupted run {record['id']}: {error}")
                continue
            self.log("warn", f"Flow '{record.get('flow_name')}' run {record['id']}: {message}")
            if record.get("parent_id") is None:
                self.alerts.emit(
                    "run_failed",
                    f"'{record.get('flow_name')}' was interrupted",
                    message,
                    path=run_path(record.get("flow_id"), record["id"]),
                    key=f"interrupted:{record['id']}",
                    data={"run_id": record["id"], "flow_id": record.get("flow_id")},
                    dry_run=bool(record.get("dry_run")),
                )

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
                    "dry_run": bool(flow.get("dry_run")),
                    "version": flow.get("version"),
                    "saved_by": flow.get("saved_by"),
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

    def snapshot_flow(self, flow_id):
        """A copy of a flow plus copies of every sub-flow it calls, directly or not (``subflow_snapshots``).

        A scheduled activity keeps this, so what was reviewed is what runs: its sub-flows run as they were
        when it was scheduled (or refreshed), not as they are when it starts (DECISIONS D11).
        """
        with self.lock:
            flow = self.flows.get(flow_id)
            if flow is None:
                raise NotFoundError(f"Flow '{flow_id}' not found")
            snapshot = copy.deepcopy(flow)
            snapshots = {}
            pending = subflow_ids(snapshot)
            while pending:
                child_id = pending.pop()
                child = self.flows.get(child_id)
                if child_id in snapshots or child is None:
                    continue  # a missing sub-flow fails the run when it's called, as it would unpinned
                snapshots[child_id] = copy.deepcopy(child)
                pending.extend(subflow_ids(child))
            if snapshots:
                snapshot["subflow_snapshots"] = snapshots
            return snapshot

    def save_flow(self, flow, flow_id=None, user=None, note="", base_version=None, force=False, restored_from=None):
        """Create (flow_id None) or replace a flow, recording a new version. Returns (flow, problems).

        Saving never arms a flow. With ``base_version`` (the version the editor opened), saving over a newer
        version someone else saved raises ConflictError unless ``force``. Saving unchanged content keeps the
        version.
        """
        if not isinstance(flow, dict):
            raise EngineError("Flow must be a JSON object")
        try:
            flow = upgrade("flow", flow)  # an export from an older version (or one with a format stamp)
        except NewerFormatError as error:
            raise EngineError(str(error)) from None
        shape = _shape_problem(flow)
        if shape:
            raise EngineError(shape)
        flow = copy.deepcopy(flow)
        for key in SERVER_KEYS:
            flow.pop(key, None)
        with self.lock:
            existing = None
            if flow_id is not None:
                existing = self.flows.get(flow_id)
                if existing is None:
                    raise NotFoundError(f"Flow '{flow_id}' not found")
                current = int(existing.get("version") or 0)
                try:
                    base_version = None if base_version in (None, "") else int(base_version)
                except (TypeError, ValueError):
                    raise EngineError("base_version must be a version number") from None
                if base_version is not None and base_version != current and not force:
                    raise ConflictError(
                        f"'{existing['name']}' was saved by {existing.get('saved_by') or 'someone'} "
                        f"(version {current}) after you opened version {base_version}",
                        current_version=current,
                        saved_by=existing.get("saved_by"),
                        saved_at=existing.get("updated_at"),
                    )
                flow["id"] = flow_id
                flow["armed"] = bool(existing.get("armed"))
                flow["created_at"] = existing.get("created_at")
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
            flow["dry_run"] = bool(flow.get("dry_run"))
            flow["parameters"] = list(flow.get("parameters") or []) if isinstance(flow.get("parameters"), list) else []
            flow["nodes"] = list(flow.get("nodes") or [])
            flow["edges"] = list(flow.get("edges") or [])
            problems = validate_flow(flow)
            flow.pop("disarmed_reason", None)
            if flow["armed"] and problems:
                flow["armed"] = False
                flow["disarmed_reason"] = "The saved flow has problems"
                self.log("warn", f"Flow '{flow['name']}' was disarmed because it no longer validates")
            elif flow["armed"] and existing is not None and less_safe_changes(existing, flow):
                # An armed flow's triggers would pick up the less safe settings on their next fire unreviewed:
                # disarm so someone re-arms it knowingly (and the re-arm is in the audit log)
                changes = "; ".join(less_safe_changes(existing, flow))
                flow["armed"] = False
                flow["disarmed_reason"] = f"Safety settings were relaxed ({changes})"
                self.log("warn", f"Flow '{flow['name']}' was disarmed: {flow['disarmed_reason']}")
            if existing is not None and less_safe_changes(existing, flow):
                # Armed flows that call this one (directly or not) would run the relaxed version unreviewed
                self._disarm_callers(flow["id"], flow["name"], "; ".join(less_safe_changes(existing, flow)))
            if existing is not None and content(existing) == content(flow):
                existing["armed"] = flow["armed"]
                existing.pop("disarmed_reason", None)
                return copy.deepcopy(existing), problems  # nothing changed: no new version
            latest = self.versions.latest(flow["id"]) if existing is not None or restored_from else None
            flow["version"] = (
                int(existing.get("version") or 0) if existing else int((latest or {}).get("version") or 0)
            ) + 1
            flow["updated_at"] = time.time()
            flow["saved_by"] = user_name(user)
            flow["saved_note"] = note or ""
            self.store.save_flow(flow)
            self.flows[flow["id"]] = flow
            self.versions.record(
                flow, flow["saved_by"], note, previous=(latest or {}).get("flow"), restored_from=restored_from
            )
            self.store.delete_record("deleted_flows", flow["id"])
            return copy.deepcopy(flow), problems

    def _disarm_callers(self, flow_id, name, changes):
        """Disarm every armed flow whose sub-flows include flow_id (at any depth). Caller holds the lock."""
        for caller in self.flows.values():
            if caller.get("id") == flow_id or not caller.get("armed"):
                continue
            if flow_id in self._subflow_closure(caller):
                caller["armed"] = False
                caller["disarmed_reason"] = f"Its sub-flow '{name}' was relaxed ({changes})"
                self.store.save_flow(caller, defer=True)
                self.log("warn", f"Flow '{caller.get('name')}' was disarmed: {caller['disarmed_reason']}")

    def _subflow_closure(self, flow):
        seen = set()
        stack = list(subflow_ids(flow))
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            child = self.flows.get(current)
            if child is not None:
                stack.extend(subflow_ids(child))
        return seen

    def _ensure_versions(self):
        """Flows saved before version history get their current state as version 1."""
        for flow in self.flows.values():
            if self.versions.latest(flow["id"]) is None:
                flow["version"] = int(flow.get("version") or 1)
                flow.setdefault("updated_at", time.time())
                flow.setdefault("saved_by", "unknown")
                self.store.save_flow(flow)
                self.versions.record(flow, flow["saved_by"], "Saved before version history")

    def list_versions(self, flow_id):
        versions = self.versions.list(flow_id)
        if not versions and flow_id not in self.flows:
            raise NotFoundError(f"Flow '{flow_id}' not found")
        return versions

    def get_version(self, flow_id, version):
        entry = self.versions.get(flow_id, int(version))
        if entry is None:
            raise NotFoundError(f"Flow '{flow_id}' has no version {version}")
        return entry

    def diff_versions(self, flow_id, old, new=None):
        """What changed from version old to version new (default: the current flow)."""
        before = self.get_version(flow_id, old)["flow"]
        after = self.get_version(flow_id, new)["flow"] if new is not None else self.get_flow(flow_id)
        return diff_flows(before, after)

    def restore_version(self, flow_id, version, user=None, note=""):
        """Save an old version as a new version (history is never rewritten)."""
        entry = self.get_version(flow_id, version)
        snapshot = copy.deepcopy(entry["flow"])
        with self.lock:
            exists = flow_id in self.flows
        note = note or f"Restored version {version}"
        if exists:
            return self.save_flow(snapshot, flow_id, user, note, force=True, restored_from=int(version))
        return self._recreate(flow_id, snapshot, user, note, int(version))

    def _recreate(self, flow_id, snapshot, user, note, restored_from):
        latest = self.versions.latest(flow_id) or {}
        with self.lock:
            # A placeholder for save_flow to replace; the version number carries on from the history
            self.flows[flow_id] = {
                "id": flow_id,
                "name": snapshot.get("name"),
                "armed": False,
                "version": int(latest.get("version") or 0),
                "created_at": time.time(),
            }
        return self.save_flow(snapshot, flow_id, user, note, force=True, restored_from=restored_from)

    def delete_flow(self, flow_id, user=None):
        with self.lock:
            flow = self.flows.get(flow_id)
            if flow is None:
                raise NotFoundError(f"Flow '{flow_id}' not found")
            # Valkey first: if it refuses, the flow stays (in memory and after a restart) and its runs go on
            self.store.delete_flow(flow_id)
            self.flows.pop(flow_id, None)
            self.queues.pop(flow_id, None)
            runs = [run for run in self.runs.values() if run.flow.get("id") == flow_id]
            self.store.save_record(
                "deleted_flows",
                flow_id,
                {
                    "id": flow_id,
                    "name": flow.get("name"),
                    "version": flow.get("version"),
                    "deleted_at": time.time(),
                    "deleted_by": user_name(user),
                    "template": bool(flow.get("template")),
                },
            )
        for run in runs:
            run.stop()
        self.log("info", f"Flow '{flow.get('name')}' deleted")
        return flow

    def list_deleted(self):
        deleted = list(self.store.records("deleted_flows").values())
        return sorted(deleted, key=lambda d: -(d.get("deleted_at") or 0))

    def restore_deleted(self, flow_id, user=None):
        record = self.store.records("deleted_flows").get(flow_id)
        latest = self.versions.latest(flow_id)
        if record is None or latest is None:
            raise NotFoundError(f"No deleted flow '{flow_id}' to restore")
        return self._recreate(flow_id, copy.deepcopy(latest["flow"]), user, "Restored after delete", latest["version"])

    def set_armed(self, flow_id, armed):
        with self.lock:
            flow = self.flows.get(flow_id)
            if flow is None:
                raise NotFoundError(f"Flow '{flow_id}' not found")
            if armed:
                problems = validate_flow(flow)
                if problems:
                    raise EngineError("Fix the flow's problems before arming it", problems)
                # Arming is saved first: if Valkey refuses, the flow stays disarmed and the error shows
                updated = {**flow, "armed": True}
                updated.pop("disarmed_reason", None)
                self.store.save_flow(updated)
                flow["armed"] = True
                flow.pop("disarmed_reason", None)
            else:
                # Disarming takes effect at once and is never refused (D28): saved when Valkey can be reached
                flow["armed"] = False
                flow.pop("disarmed_reason", None)
                self.store.save_flow(flow, defer=True)
        self.log("info", f"Flow '{flow['name']}' {'armed' if armed else 'disarmed'}")
        return copy.deepcopy(flow)

    def armed_flows(self):
        with self.lock:
            return [flow for flow in self.flows.values() if flow.get("armed")]

    # ---- runs ------------------------------------------------------------

    def trigger(
        self,
        flow_id,
        node_id=None,
        reason="",
        variables=None,
        manual=False,
        mode=None,
        trigger_info=None,
        dry_run=False,
        skip_waits=False,
    ):
        """Start a run of a flow from a trigger node, applying the flow's re-trigger policy.

        Returns {"result": "started" | "queued" | "skipped", "run": run json or None}.
        Automatic triggers only start armed flows; manual runs work while disarmed (for testing).
        ``trigger_info`` says what started it (kept with the run); ``dry_run`` sends nothing.
        """
        with self.lock:
            flow = self.flows.get(flow_id)
            if flow is None:
                raise NotFoundError(f"Flow '{flow_id}' not found")
            if self.halted:
                return {"result": "skipped", "run": None, "message": "The flow engine is shutting down"}
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
            flow_mode = flow.get("mode") if flow.get("mode") in MODES else "run"
            if mode is not None and not dry_run and MODES.index(mode) > MODES.index(flow_mode):
                # The flow's mode is a floor: a real run can be more cautious, never less (a dry run sends
                # nothing, so it may skip approvals). Change the flow's mode
                # (a saved, versioned, audited change) to run it with fewer approvals.
                raise EngineError(
                    f"This flow's mode is {flow_mode}: a run can use {flow_mode} or a more cautious mode, "
                    f"not {mode}. Change the flow's mode to run it with fewer approvals."
                )
            variables, param_problems = fill_parameters(flow.get("parameters"), variables)
            if param_problems:
                raise EngineError("Parameters: " + "; ".join(param_problems))
            if flow.get("template"):
                for name in CONTACT_VARIABLES:
                    variables.setdefault(name, None)
            trigger_info = dict(trigger_info or {})
            trigger_info.setdefault("kind", "manual" if manual else nodes[node_id]["type"])
            trigger_info.setdefault("node", node_id)
            trigger_info.setdefault(
                "node_label", (nodes[node_id].get("data") or {}).get("label") or nodes[node_id]["type"]
            )
            trigger_info.setdefault("detail", reason)
            options = {"mode": mode, "trigger_info": trigger_info, "dry_run": dry_run, "skip_waits": skip_waits}
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
                    options["trigger_info"] = {**trigger_info, "queued_at": time.time()}
                    queue.append((node_id, reason, variables, options))
                    return {"result": "queued", "run": None, "message": f"Queued ({len(queue)} waiting)"}
            return self._start_run(flow, node_id, reason, variables, options)

    def _start_run(self, flow, node_id, reason, variables, options=None):
        if self.halted:
            return {"result": "skipped", "run": None, "message": "The flow engine is shutting down"}
        if len(self.runs) >= MAX_ACTIVE_RUNS:
            message = f"Too many active runs ({MAX_ACTIVE_RUNS}), run not started"
            self.log("error", f"Flow '{flow['name']}': {message}")
            return {"result": "skipped", "run": None, "message": message}
        run = FlowRun(self, flow, node_id, reason, variables, **(options or {}))
        self.runs[run.id] = run
        self._remember_run(run)
        self.log("info", f"Flow '{flow['name']}' run {run.id} started{' (dry run)' if run.dry_run else ''}: {reason}")
        run.start_thread()
        return {"result": "started", "run": run.as_json(include_steps=False), "message": ""}

    def start_scheduled_run(
        self,
        flow,
        reason,
        variables,
        mode,
        approval_deadline=None,
        activity_id=None,
        trigger_info=None,
        dry_run=False,
        skip_waits=False,
    ):
        """Start a run of a flow snapshot for a scheduled activity (or a rehearsal). Policies don't apply.

        ``flow`` is the activity's snapshot; its ``subflow_snapshots`` (added by snapshot_flow) pin the
        sub-flows it calls.
        """
        flow = dict(flow)
        pinned_subflows = flow.pop("subflow_snapshots", None)
        problems = validate_flow(flow)
        if problems:
            raise EngineError(f"The flow has problems: {problems[0]['message']}")
        entry = next((n for n in flow.get("nodes") or [] if n["type"] == "trigger_manual"), None)
        if entry is None:
            raise EngineError("The flow needs a Manual trigger to be scheduled")
        info = {
            "kind": "activity",
            "node": entry["id"],
            "node_label": "Manual",
            "detail": reason,
            **(trigger_info or {}),
        }
        if self.planner.settings.get("paused") and not dry_run:
            # STOP ALL or an operator paused the scheduler (maybe while it was busy launching this)
            raise EngineError("The contact scheduler is paused")
        with self.lock:
            if self.halted:
                raise EngineError("The flow engine is shutting down")
            if len(self.runs) >= MAX_ACTIVE_RUNS:
                raise EngineError(f"Too many active runs ({MAX_ACTIVE_RUNS})")
            run = FlowRun(
                self,
                flow,
                entry["id"],
                reason,
                variables,
                mode=mode,
                trigger_info=info,
                dry_run=dry_run,
                skip_waits=skip_waits,
                # The activity's snapshot of its sub-flows, taken by the planner (never from the flow itself)
                pinned_subflows=pinned_subflows,
            )
            run.approval_deadline = approval_deadline
            run.activity_id = activity_id
            self.runs[run.id] = run
            self._remember_run(run)
        self.log(
            "info", f"Flow '{flow.get('name')}' run {run.id} started{' (dry run)' if run.dry_run else ''}: {reason}"
        )
        run.start_thread()
        return run

    def run_subflow(self, parent, flow_id, variables):
        if parent.depth + 1 > MAX_SUBFLOW_DEPTH:
            raise RuntimeError(f"Sub-flows nested more than {MAX_SUBFLOW_DEPTH} deep")
        with self.lock:
            if len(self.runs) >= MAX_ACTIVE_RUNS:
                raise RuntimeError(f"Too many active runs ({MAX_ACTIVE_RUNS}), sub-flow not started")
            if parent.pinned_subflows is not None:
                # A scheduled activity's run: the sub-flow as it was when the activity was scheduled
                flow = parent.pinned_subflows.get(flow_id)
                if flow is None:
                    raise RuntimeError(
                        f"Sub-flow '{flow_id}' isn't in this activity's snapshot: refresh the activity's template"
                    )
            else:
                flow = self.flows.get(flow_id)
            if flow is None:
                raise RuntimeError(f"Sub-flow '{flow_id}' not found")
            entry = next((n for n in flow.get("nodes") or [] if n["type"] == "trigger_manual"), None)
            if entry is None:
                raise RuntimeError(f"Sub-flow '{flow.get('name')}' needs a Manual trigger to be called")
            problems = validate_flow(flow)
            if problems:
                raise RuntimeError(f"Sub-flow '{flow.get('name')}' has problems: {problems[0]['message']}")
            info = {
                "kind": "subflow",
                "node": entry["id"],
                "node_label": "Manual",
                "parent_run_id": parent.id,
                "parent_flow_id": parent.flow.get("id"),
                "parent_flow": parent.flow.get("name"),
                "detail": f"Called from '{parent.flow.get('name')}'",
            }
            child = FlowRun(
                self,
                flow,
                entry["id"],
                f"Called from '{parent.flow.get('name')}'",
                variables,
                parent,
                trigger_info=info,
            )
            self.runs[child.id] = child
            parent.children.append(child.id)
            self._remember_run(child)
        child.execute()  # synchronous: the parent's step waits for it
        return child

    def on_run_finished(self, run):
        # History first, then clear the active record (queued in that order), so a crash in between never
        # loses the run; the finished run stays findable in memory until its history is surely written
        record = run.as_json()
        try:
            self.store.add_run(run.flow.get("id"), record)
        except Exception as error:  # noqa: BLE001 - history is best effort
            self.log("error", f"Could not save run {run.id} history: {error}")
        try:
            self.store.delete_record("active_runs", run.id, defer=True)
        except Exception as error:  # noqa: BLE001
            self.log("warn", f"Could not clear active run {run.id}: {error}")
        with self.lock:
            self.finished[run.id] = record
            while len(self.finished) > RECENTLY_FINISHED:
                self.finished.popitem(last=False)
            self.runs.pop(run.id, None)
            if run.parent is None and not self.halted:
                # Start the next queued run while still holding the lock, so a trigger arriving now sees it
                # running instead of starting a second run of a "queue" flow
                self._start_next_queued(run.flow.get("id"))
        level = {"success": "info", "stopped": "warn"}.get(run.status, "error")
        detail = f": {run.message}" if run.message else ""
        self.log(level, f"Flow '{run.flow.get('name')}' run {run.id} {run.status}{detail}")
        if run.parent is None and run.status in ("failed", "stopped"):
            event = "run_failed" if run.status == "failed" else "run_stopped"
            # Someone who pressed Run is watching it; unattended runs (triggers, activities) need an alert
            attended = (run.trigger_info or {}).get("kind") in ("manual", "rehearsal")
            self.alerts.emit(
                event,
                f"'{run.flow.get('name')}' {run.status}",
                run.message,
                path=run_path(run.flow.get("id"), run.id),
                key=f"{event}:{run.id}",
                level="notification" if attended or event == "run_stopped" else "alert",
                data={"run_id": run.id, "flow_id": run.flow.get("id"), "trigger": run.trigger_info},
                dry_run=run.dry_run,
            )
        if getattr(run, "activity_id", None):
            try:
                self.planner.on_run_finished(run)
            except Exception as error:  # noqa: BLE001
                self.log("error", f"Could not record activity result for run {run.id}: {error}")

    def _start_next_queued(self, flow_id):
        """Start the oldest queued trigger of a flow, skipping ones that no longer apply. Caller holds the lock."""
        queue = self.queues.get(flow_id)
        flow = self.flows.get(flow_id)
        while queue and flow:
            node_id, reason, variables, options = queue.popleft()
            manual = (options.get("trigger_info") or {}).get("kind") == "manual"
            if not manual and not flow.get("armed"):
                self.log("info", f"Flow '{flow['name']}': dropped a queued trigger because the flow was disarmed")
                continue
            result = self._start_run(flow, node_id, reason, variables, options)
            if result["result"] != "started":
                self.alerts.emit(
                    "run_failed",
                    f"'{flow['name']}' queued run not started",
                    result["message"],
                    path=run_path(flow_id),
                    key=f"queue_drop:{flow_id}:{time.time()}",
                )
            return

    def on_approval_requested(self, run, approval):
        """A run is waiting for an operator: if it's still waiting after a while, tell them wherever they are.

        Someone watching the tool answers in seconds, and a COSMOS alert stays until acknowledged even once
        the approval is answered, so it only goes out if nobody has answered after approval_escalate_after.
        """
        delay = self.alerts.approval_delay
        if delay <= 0:
            return self._alert_approval(run, approval)

        def escalate():
            if any(a["id"] == approval["id"] for a in run.pending_approvals()):
                self._alert_approval(run, approval, waited=delay)

        timer = threading.Timer(delay, escalate)
        timer.daemon = True
        timer.start()

    def _alert_approval(self, run, approval, waited=0):
        root = run
        while root.parent is not None:
            root = root.parent
        self.alerts.emit(
            "approval_waiting",
            f"'{run.flow.get('name')}' is waiting for approval" + (f" ({waited:g}s)" if waited else ""),
            f"{approval['label']}: {approval['preview'][:200]} ({approval['reason']})",
            path=run_path(root.flow.get("id"), root.id),
            key=f"approval:{approval['id']}",
            data={"run_id": run.id, "approval_id": approval["id"], "deadline": approval.get("deadline")},
            dry_run=run.dry_run,
        )

    def approve(self, run_id, approval_id, decision, user=None, value=None):
        """An operator answers a run's pending approval (crawl/walk modes)."""
        with self.lock:
            run = self.runs.get(run_id)
        if run is None:
            raise NotFoundError(f"Run '{run_id}' is not active")
        try:
            answered = run.approve(approval_id, decision, user_name(user), value)
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

    def stop_all(self, user=None):
        """Safety stop: disarm every flow, drop queued triggers, pause scheduled activities, stop every run.

        Everything takes effect in memory at once and is saved when Valkey can be reached (D28): STOP ALL
        never fails or waits because Valkey is slow or away.
        """
        # Pause the scheduler first (planner lock before engine lock): an activity it is launching right now
        # finishes starting before this returns, so the loop below stops it, and nothing launches after
        try:
            self.planner.set_paused(True, "STOP ALL")
        except Exception as error:  # noqa: BLE001 - never let pausing stop the rest
            self.log("error", f"STOP ALL could not pause the contact scheduler: {error}")
        with self.lock:
            to_save = []
            for flow in self.flows.values():
                if flow.get("armed"):
                    flow["armed"] = False
                    to_save.append(flow)
            disarmed = len(to_save)
            self.queues.clear()
            runs = list(self.runs.values())
            scripts = list(self.detached_scripts)
            self.detached_scripts.clear()
        for run in runs:
            run.stop()
        for flow in to_save:
            try:
                self.store.save_flow(flow, defer=True)
            except Exception as error:  # noqa: BLE001
                self.log("error", f"STOP ALL could not queue saving flow '{flow.get('name')}': {error}")
        # Scripts started without waiting belong to no run any more; STOP ALL stops them too (at once, not
        # one slow Script Runner call after another)
        stopped_scripts = self._stop_scripts(scripts)
        summary = f"Disarmed {disarmed} flows, stopped {len(runs)} runs"
        if scripts:
            summary += f" and {stopped_scripts} scripts started without waiting"
        self.log("warn", f"STOP ALL: {summary}, paused the contact scheduler")
        self.alerts.emit(
            "stop_all",
            f"STOP ALL by {user_name(user)}",
            f"{summary}, paused the contact scheduler",
            path="/tools/flowautomator",
        )
        return {
            "disarmed": disarmed,
            "stopped": len(runs),
            "scripts_stopped": stopped_scripts,
            "scheduler_paused": True,
            # False while Valkey is away: it all took effect, and is saved once Valkey is back
            "persisted": self.store.health()["pending_writes"] == 0,
        }

    def _stop_scripts(self, script_ids, collect=False):
        """Ask Script Runner to stop scripts, all at once. Returns how many it confirmed in time (or which)."""
        results = []

        def stop(script_id):
            try:
                self.backend.script_stop(script_id)
                results.append(script_id)
            except Exception:  # noqa: BLE001,S110 - it may have finished already
                pass

        threads = [threading.Thread(target=stop, args=(sid,), daemon=True) for sid in script_ids]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + SCRIPT_STOP_WAIT_SECONDS
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return list(results) if collect else len(results)

    def reserve_branch_threads(self, count):
        """Take threads for a Parallel's branches from the engine-wide budget. False if there aren't enough."""
        with self.lock:
            if self.branch_threads + count > MAX_BRANCH_THREADS:
                return False
            self.branch_threads += count
            return True

    def release_branch_threads(self, count):
        with self.lock:
            self.branch_threads = max(0, self.branch_threads - count)

    def track_detached_script(self, script_id):
        """Remember a script a run started without waiting for it, so STOP ALL can stop it."""
        with self.lock:
            self.detached_scripts.append(script_id)
            if len(self.detached_scripts) > MAX_DETACHED_SCRIPTS:
                dropped = self.detached_scripts.pop(0)
                self.log("warn", f"Tracking too many scripts started without waiting; STOP ALL forgets {dropped}")

    def prune_detached_scripts(self):
        """Forget fire-and-forget scripts that have finished (Script Runner is asked outside the lock)."""
        with self.lock:
            scripts = list(self.detached_scripts)
        done = set()
        for script_id in scripts:
            try:
                if self.backend.script_state(script_id) in SCRIPT_DONE_STATES:
                    done.add(script_id)
            except Exception:  # noqa: BLE001,S112 - keep it; STOP ALL tries anyway
                continue
        if done:
            with self.lock:
                self.detached_scripts = [s for s in self.detached_scripts if s not in done]

    def _history_flow_ids(self):
        with self.lock:
            ids = list(self.flows.keys())
        return ids + [d["id"] for d in self.list_deleted() if d["id"] not in ids]

    def get_run(self, run_id):
        with self.lock:
            run = self.runs.get(run_id)
            finished = self.finished.get(run_id)
        if run is not None:
            return run.as_json()
        if finished is not None:
            return finished
        for flow_id in self._history_flow_ids():
            for record in self.store.runs(flow_id):
                if record.get("id") == run_id:
                    return record
        raise NotFoundError(f"Run '{run_id}' not found")

    def list_runs(self, flow_id=None, limit=50, **filters):
        """Runs, newest first, across flows (including deleted ones).

        Filters: status, trigger (kind), user (who started it), activity_id, dry_run (bool),
        since / until (Unix seconds), search (text anywhere in the summary).
        """
        with self.lock:
            active = [r.as_json(include_steps=False) for r in self.runs.values()]
        flow_ids = [flow_id] if flow_id else self._history_flow_ids()
        if flow_id:
            active = [r for r in active if r["flow_id"] == flow_id]
        history = []
        for fid in flow_ids:
            for record in self.store.runs(fid):
                record.pop("steps", None)
                history.append(record)
        active_ids = {r["id"] for r in active}
        runs = [r for r in active + [r for r in history if r["id"] not in active_ids] if _run_matches(r, filters)]
        runs.sort(key=lambda r: r.get("started_at") or 0, reverse=True)
        return runs[:limit]

    def status(self):
        contacts = self.planner.status()  # outside the engine lock: the planner calls into the engine
        with self.lock:
            return {
                "flows": len(self.flows),
                "armed": sum(1 for flow in self.flows.values() if flow.get("armed")),
                "armed_ids": sorted(flow_id for flow_id, flow in self.flows.items() if flow.get("armed")),
                "active_runs": [run.as_json(include_steps=False) for run in self.runs.values()],
                "approvals": [a for run in self.runs.values() for a in run.pending_approvals()],
                "queued": {flow_id: len(queue) for flow_id, queue in self.queues.items() if queue},
                "triggers": self.triggers.status(),
                "contacts": contacts,
                "store": self.store.health(),
                "time": time.time(),
            }


def subflow_ids(flow):
    """Ids of the flows a flow's Sub-flow nodes call."""
    return [
        (node.get("data") or {}).get("flow_id")
        for node in flow.get("nodes") or []
        if node.get("type") == "subflow" and (node.get("data") or {}).get("flow_id")
    ]


def run_path(flow_id, run_id=None):
    """The tool URL that opens a flow (and a run of it)."""
    return f"/tools/flowautomator?flow={flow_id}" + (f"&run={run_id}" if run_id else "")


def _run_matches(run, filters):
    info = run.get("trigger") or {}
    if filters.get("status") and run.get("status") != filters["status"]:
        return False
    if filters.get("trigger") and info.get("kind") != filters["trigger"]:
        return False
    if filters.get("user") and info.get("user") != filters["user"]:
        return False
    if filters.get("activity_id") and info.get("activity_id") != filters["activity_id"]:
        return False
    if filters.get("dry_run") is not None and bool(run.get("dry_run")) != filters["dry_run"]:
        return False
    started = run.get("started_at") or 0
    if filters.get("since") and started < filters["since"]:
        return False
    if filters.get("until") and started > filters["until"]:
        return False
    if filters.get("search"):
        text = " ".join(str(run.get(k) or "") for k in ("flow_name", "reason", "message", "status", "id"))
        text += " " + " ".join(str(v) for v in info.values())
        if filters["search"].lower() not in text.lower():
            return False
    return True
