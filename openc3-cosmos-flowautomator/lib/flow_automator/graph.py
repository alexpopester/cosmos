"""Node type registry and flow graph validation.

A flow is JSON::

    {
      "id": "...", "name": "...", "description": "...",
      "armed": false,
      "policy": "skip" | "queue" | "parallel",   # what to do if triggered while running
      "variables": {"name": value, ...},          # initial run variables
      "nodes": [{"id": "n1", "type": "if", "position": {...}, "data": {...}}],
      "edges": [{"id": "e1", "source": "n1", "sourceHandle": "true", "target": "n2"}]
    }

Wires are control flow: following an edge means "then do this". Every node
except a trigger has one input. Outputs are named handles; each handle may
have one edge, except a ``parallel`` node's ``out`` which starts one branch
per edge.
"""

from .cron import check_cron
from .expressions import check_syntax, check_template


POLICIES = ("skip", "queue", "parallel")
# Execution modes, most cautious first: crawl asks the operator before every
# step, walk only before steps marked require_approval and hazardous commands,
# run never asks
MODES = ("crawl", "walk", "run")
ERROR_HANDLE = "error"


def _switch_outputs(data):
    cases = data.get("cases") or []
    return [f"case_{index}" for index in range(len(cases))] + ["default"]


# For each type: outputs (list or function of data), whether it can fail with
# an "error" handle, and which data fields hold expressions or templates.
NODE_TYPES = {
    # Triggers start a run. They have no input.
    "trigger_manual": {"trigger": True, "outputs": ["out"]},
    "trigger_tlm": {"trigger": True, "outputs": ["out"], "expressions": ["condition"]},
    "trigger_schedule": {"trigger": True, "outputs": ["out"]},
    "trigger_packet": {"trigger": True, "outputs": ["out"], "required": ["target", "packet"]},
    "trigger_limits": {"trigger": True, "outputs": ["out"], "required": ["item"]},
    # Logic
    "if": {"outputs": ["true", "false"], "expressions": ["condition"], "errors": True},
    "switch": {"outputs": _switch_outputs, "expressions": ["expression"], "errors": True},
    "loop": {"outputs": ["body", "done"], "errors": True},
    "parallel": {"outputs": ["out"]},
    "join": {"outputs": ["out"]},
    "set_var": {"outputs": ["out"], "expressions": ["expression"], "required": ["name"], "errors": True},
    # Actions
    "send_cmd": {"outputs": ["out"], "templates": ["command"], "required": ["command"], "errors": True},
    "run_script": {"outputs": ["out"], "templates": ["filename"], "required": ["filename"], "errors": True},
    "set_tlm": {"outputs": ["out"], "templates": ["value"], "required": ["item"], "errors": True},
    "log": {"outputs": ["out"], "templates": ["message"]},
    # Waits
    "wait_time": {"outputs": ["out"]},
    "wait_tlm": {"outputs": ["ok", "timeout"], "expressions": ["condition"], "errors": True},
    "wait_check": {"outputs": ["out"], "expressions": ["condition"], "errors": True},
    # Flow control
    "subflow": {"outputs": ["out"], "required": ["flow_id"], "errors": True},
    "end": {"outputs": []},
}


def outputs_for(node):
    spec = NODE_TYPES[node["type"]]
    outputs = spec["outputs"]
    outputs = list(outputs(node.get("data") or {})) if callable(outputs) else list(outputs)
    if spec.get("errors"):
        outputs.append(ERROR_HANDLE)
    return outputs


def is_trigger(node):
    return NODE_TYPES.get(node.get("type"), {}).get("trigger", False)


def validate_flow(flow):
    """Return a list of problems: [{"node": id or None, "message": str}]. Empty means the flow can be armed."""
    problems = []

    def problem(message, node=None):
        problems.append({"node": node, "message": message})

    if not str(flow.get("name") or "").strip():
        problem("Flow needs a name")
    if flow.get("policy", "skip") not in POLICIES:
        problem(f"Policy must be one of {', '.join(POLICIES)}")
    if flow.get("mode", "run") not in MODES:
        problem(f"Mode must be one of {', '.join(MODES)}")
    try:
        if float(flow.get("approval_timeout") or 0) < 0:
            raise ValueError
    except (TypeError, ValueError):
        problem("Approval timeout must be zero (wait forever) or a positive number of seconds")
    variables = flow.get("variables") or {}
    if not isinstance(variables, dict):
        problem("Variables must be an object")
    for name in variables if isinstance(variables, dict) else []:
        if not str(name).isidentifier():
            problem(f"Variable name '{name}' is not a valid identifier")

    nodes = flow.get("nodes") or []
    edges = flow.get("edges") or []
    by_id = {}
    for node in nodes:
        node_id = node.get("id")
        if not node_id:
            problem("A node has no id")
            continue
        if node_id in by_id:
            problem(f"Duplicate node id '{node_id}'", node_id)
        by_id[node_id] = node
        if node.get("type") not in NODE_TYPES:
            problem(f"Unknown node type '{node.get('type')}'", node_id)
            continue
        problems.extend({"node": node_id, "message": msg} for msg in _validate_node_data(node))

    if not any(is_trigger(node) for node in nodes):
        problem("Flow needs at least one trigger node")

    used_handles = {}
    for edge in edges:
        source = by_id.get(edge.get("source"))
        target = by_id.get(edge.get("target"))
        if source is None or target is None:
            problem(f"Edge '{edge.get('id')}' connects to a missing node")
            continue
        if source.get("type") not in NODE_TYPES or target.get("type") not in NODE_TYPES:
            continue
        handle = edge.get("sourceHandle") or "out"
        if handle not in outputs_for(source):
            problem(f"Node has no output '{handle}'", source["id"])
            continue
        if is_trigger(target):
            problem("Triggers can't have inputs", target["id"])
        key = (source["id"], handle)
        used_handles[key] = used_handles.get(key, 0) + 1
        if used_handles[key] > 1 and source["type"] != "parallel":
            problem(f"Output '{handle}' has more than one wire (use a Parallel node to branch)", source["id"])

    for node in nodes:
        if node.get("type") == "parallel" and used_handles.get((node["id"], "out"), 0) < 2:
            problem("Parallel needs at least two outgoing wires", node["id"])
    return problems


def _validate_node_data(node):
    spec = NODE_TYPES[node["type"]]
    data = node.get("data") or {}
    messages = []
    for field in spec.get("required", []):
        if data.get(field) in (None, ""):
            messages.append(f"'{field}' is required")
    for field in spec.get("expressions", []):
        error = check_syntax(data.get(field))
        if error:
            messages.append(f"{field}: {error}")
    for field in spec.get("templates", []):
        error = check_template(data.get(field))
        if error:
            messages.append(f"{field}: {error}")

    node_type = node["type"]
    if node_type == "trigger_schedule":
        if data.get("mode", "interval") == "cron":
            error = check_cron(data.get("cron", ""))
            if error:
                messages.append(error)
        elif not _positive(data.get("interval")):
            messages.append("Interval must be a positive number of seconds")
    elif node_type == "loop":
        mode = data.get("mode", "count")
        if mode == "count" and not _positive(data.get("count")):
            messages.append("Count must be a positive number")
        if mode == "while":
            error = check_syntax(data.get("condition"))
            if error:
                messages.append(f"condition: {error}")
        if not str(data.get("variable") or "i").isidentifier():
            messages.append("Loop variable must be a valid identifier")
    elif node_type == "set_var":
        if data.get("name") and not str(data["name"]).isidentifier():
            messages.append("Variable name must be a valid identifier")
    elif node_type == "switch":
        for case in data.get("cases") or []:
            error = check_syntax(str(case))
            if error:
                messages.append(f"case {case!r}: {error}")
    elif node_type in ("wait_time",):
        if not _non_negative(data.get("seconds")):
            messages.append("Seconds must be zero or more")
    elif node_type in ("wait_tlm", "wait_check"):
        if not _positive(data.get("timeout")):
            messages.append("Timeout must be a positive number of seconds")
    elif node_type == "end" and data.get("status", "success") not in ("success", "failed"):
        messages.append("End status must be success or failed")
    return messages


def _positive(value):
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _non_negative(value):
    try:
        return float(value) >= 0
    except (TypeError, ValueError):
        return False


class Graph:
    """Read-only index over a flow's nodes and edges."""

    def __init__(self, flow):
        self.flow = flow
        self.nodes = {node["id"]: node for node in flow.get("nodes") or []}
        self._next = {}
        for edge in flow.get("edges") or []:
            key = (edge["source"], edge.get("sourceHandle") or "out")
            self._next.setdefault(key, []).append(edge["target"])

    def next_nodes(self, node_id, handle):
        return [self.nodes[target] for target in self._next.get((node_id, handle), []) if target in self.nodes]

    def has_output(self, node_id, handle):
        return bool(self._next.get((node_id, handle)))

    def triggers(self):
        return [node for node in self.nodes.values() if is_trigger(node)]
