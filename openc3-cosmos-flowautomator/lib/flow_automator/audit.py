"""Who did what: an append-only record of changes and operator decisions.

Every write through the HTTP API is recorded with the user from their COSMOS
token (Enterprise has real usernames; Core has one user, reported by COSMOS as
"anonymous"), and the contact scheduler records what it does on its own
(starting activities, missing them, creating them from rules) as "scheduler".

Entries go to a capped Redis list and to the COSMOS log, which COSMOS keeps
in its log bucket, so the history survives the list being trimmed.
"""

import json
import time


DEFAULT_LIMIT = 10_000


def user_name(user):
    if isinstance(user, dict):
        return user.get("username") or user.get("name") or "anonymous"
    return str(user or "anonymous")


class AuditLog:
    def __init__(self, store, log=None, clock=time.time, limit=DEFAULT_LIMIT):
        self.store = store
        self.log = log
        self.clock = clock
        self.limit = limit

    def record(self, user, action, kind, target_id=None, target_name=None, detail=""):
        entry = {
            "time": self.clock(),
            "user": user_name(user),
            "action": action,
            "kind": kind,
            "target_id": target_id,
            "target_name": target_name,
            "detail": detail or "",
        }
        try:
            self.store.add_audit(entry, self.limit)
        except Exception as error:  # noqa: BLE001 - the audit must never break the action it records
            if self.log:
                self.log("error", f"Could not write audit entry: {error}")
        if self.log:
            target = f" {kind} '{target_name}'" if target_name else f" {kind}"
            self.log("info", f"[audit] {entry['user']} {action}{target}{': ' + detail if detail else ''}")
        return entry

    def entries(self, limit=200, kind=None, user=None, target_id=None, search=None):
        limit = max(1, min(int(limit), self.limit))
        result = []
        needle = (search or "").lower()
        for entry in self.store.audit_entries():
            if kind and entry.get("kind") != kind:
                continue
            if user and entry.get("user") != user:
                continue
            if target_id and entry.get("target_id") != target_id:
                continue
            if needle and needle not in json.dumps(entry).lower():
                continue
            result.append(entry)
            if len(result) >= limit:
                break
        return result
