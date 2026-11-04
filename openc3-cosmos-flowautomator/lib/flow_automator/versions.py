"""Version history for flows (and so for templates, which are flows).

Every save stores a numbered snapshot of the whole flow with who saved it, an
optional note and a summary of what changed since the previous version.
Restoring an old version saves it again as a new version, so history is
never rewritten. Versions outlive the flow: a deleted flow can be restored
from its last version.
"""

import copy
import json


# Flow fields that are content (a change makes a new version). armed, ids and
# timestamps aren't.
SETTINGS = (
    "name",
    "description",
    "mode",
    "policy",
    "approval_timeout",
    "variables",
    "template",
    "parameters",
    "dry_run",
    "max_steps",
)
VOLATILE = ("armed", "updated_at", "created_at", "version", "saved_by", "saved_note", "disarmed_reason")


def content(flow):
    """The parts of a flow a version is about, for comparing."""
    return {k: v for k, v in (flow or {}).items() if k not in VOLATILE}


def node_label(node):
    data = node.get("data") or {}
    return data.get("label") or node.get("type") or node.get("id")


def _canonical(value):
    return json.dumps(value, sort_keys=True, default=str)


def diff_flows(old, new):
    """What changed from old to new.

    Returns {"settings": {field: [old, new]}, "nodes_added": [...], "nodes_removed": [...],
    "nodes_changed": [{"id", "label", "type", "fields": {field: [old, new]}}], "nodes_moved": n,
    "edges_added": n, "edges_removed": n, "summary": "..."}.
    """
    old = old or {}
    new = new or {}
    settings = {}
    for key in SETTINGS:
        if _canonical(old.get(key)) != _canonical(new.get(key)):
            settings[key] = [old.get(key), new.get(key)]
    old_nodes = {n["id"]: n for n in old.get("nodes") or [] if n.get("id")}
    new_nodes = {n["id"]: n for n in new.get("nodes") or [] if n.get("id")}
    added = [
        {"id": i, "label": node_label(n), "type": n.get("type")} for i, n in new_nodes.items() if i not in old_nodes
    ]
    removed = [
        {"id": i, "label": node_label(n), "type": n.get("type")} for i, n in old_nodes.items() if i not in new_nodes
    ]
    changed = []
    moved = 0
    for node_id in old_nodes.keys() & new_nodes.keys():
        a, b = old_nodes[node_id], new_nodes[node_id]
        fields = {}
        if a.get("type") != b.get("type"):
            fields["type"] = [a.get("type"), b.get("type")]
        a_data, b_data = a.get("data") or {}, b.get("data") or {}
        for key in sorted(a_data.keys() | b_data.keys()):
            if _canonical(a_data.get(key)) != _canonical(b_data.get(key)):
                fields[key] = [a_data.get(key), b_data.get(key)]
        if fields:
            changed.append({"id": node_id, "label": node_label(b), "type": b.get("type"), "fields": fields})
        elif _canonical(a.get("position")) != _canonical(b.get("position")):
            moved += 1

    def edge_key(edge):
        return (edge.get("source"), edge.get("sourceHandle") or "out", edge.get("target"))

    old_edges = {edge_key(e) for e in old.get("edges") or []}
    new_edges = {edge_key(e) for e in new.get("edges") or []}
    result = {
        "settings": settings,
        "nodes_added": added,
        "nodes_removed": removed,
        "nodes_changed": sorted(changed, key=lambda c: c["label"]),
        "nodes_moved": moved,
        "edges_added": len(new_edges - old_edges),
        "edges_removed": len(old_edges - new_edges),
    }
    result["summary"] = summarize(result)
    return result


def summarize(diff):
    parts = []
    for key, verb in (("nodes_added", "added"), ("nodes_removed", "removed"), ("nodes_changed", "changed")):
        items = diff[key]
        if items:
            names = ", ".join(f"'{i['label']}'" for i in items[:3]) + (f" +{len(items) - 3}" if len(items) > 3 else "")
            parts.append(f"{verb} {names}")
    wires = diff["edges_added"] + diff["edges_removed"]
    if wires:
        parts.append(f"rewired ({diff['edges_added']} added, {diff['edges_removed']} removed)")
    for key, (before, after) in diff["settings"].items():
        if key in ("mode", "policy", "name", "template", "dry_run"):
            parts.append(f"{key} {before!r} → {after!r}")
        else:
            parts.append(f"{key} changed")
    if diff["nodes_moved"] and not parts:
        parts.append(f"moved {diff['nodes_moved']} nodes")
    return "; ".join(parts) or "no changes"


class FlowVersions:
    """Snapshots of each flow, newest first, in the store."""

    def __init__(self, store, limit=200):
        self.store = store
        self.limit = limit

    def record(self, flow, user, note="", previous=None, restored_from=None):
        diff = diff_flows(previous, flow) if previous is not None else None
        entry = {
            "version": flow["version"],
            "saved_at": flow["updated_at"],
            "saved_by": user,
            "note": note or "",
            "summary": "created" if previous is None else diff["summary"],
            "restored_from": restored_from,
            "flow": copy.deepcopy(content(flow) | {"id": flow["id"], "version": flow["version"]}),
        }
        self.store.add_version(flow["id"], entry, self.limit)
        return entry

    def list(self, flow_id):
        return [{k: v for k, v in entry.items() if k != "flow"} for entry in self.store.versions(flow_id)]

    def get(self, flow_id, version):
        for entry in self.store.versions(flow_id):
            if entry["version"] == version:
                return entry
        return None

    def latest(self, flow_id):
        versions = self.store.versions(flow_id, 1)
        return versions[0] if versions else None
