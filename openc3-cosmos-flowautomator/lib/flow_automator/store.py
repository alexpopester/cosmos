"""Persists flows and run history in Redis (Valkey).

Keys (per scope):
    flow_automator__<scope>__flows            hash: flow id -> flow JSON
    flow_automator__<scope>__runs__<flow id>  list: newest first, run JSON
    flow_automator__<scope>__<kind>           hash: id -> record JSON, for the contact
                                              planner's satellites, stations,
                                              activities and settings
"""

import json


def _text(value):
    return value.decode() if isinstance(value, bytes) else value


class FlowStore:
    def __init__(self, redis, scope, history_limit=50):
        self.redis = redis
        self.scope = scope
        self.history_limit = history_limit
        self.flows_key = f"flow_automator__{scope}__flows"

    def _runs_key(self, flow_id):
        return f"flow_automator__{self.scope}__runs__{flow_id}"

    def all_flows(self):
        raw = self.redis.hgetall(self.flows_key) or {}
        return {_text(k): json.loads(_text(v)) for k, v in raw.items()}

    def save_flow(self, flow):
        self.redis.hset(self.flows_key, flow["id"], json.dumps(flow))

    def delete_flow(self, flow_id):
        self.redis.hdel(self.flows_key, flow_id)
        self.redis.delete(self._runs_key(flow_id))

    def add_run(self, flow_id, run_json):
        key = self._runs_key(flow_id)
        self.redis.lpush(key, json.dumps(run_json))
        self.redis.ltrim(key, 0, self.history_limit - 1)

    def runs(self, flow_id, limit=None):
        end = (limit or self.history_limit) - 1
        return [json.loads(_text(item)) for item in self.redis.lrange(self._runs_key(flow_id), 0, end) or []]

    # ---- contact planner records ---------------------------------------------

    def _records_key(self, kind):
        return f"flow_automator__{self.scope}__{kind}"

    def records(self, kind):
        raw = self.redis.hgetall(self._records_key(kind)) or {}
        return {_text(k): json.loads(_text(v)) for k, v in raw.items()}

    def save_record(self, kind, record_id, record):
        self.redis.hset(self._records_key(kind), record_id, json.dumps(record))

    def delete_record(self, kind, record_id):
        self.redis.hdel(self._records_key(kind), record_id)


class MemoryRedis:
    """The handful of Redis commands FlowStore uses, in memory. For tests and local runs."""

    def __init__(self):
        self.hashes = {}
        self.lists = {}

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    def hdel(self, key, field):
        self.hashes.get(key, {}).pop(field, None)

    def delete(self, key):
        self.hashes.pop(key, None)
        self.lists.pop(key, None)

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    def ltrim(self, key, start, end):
        self.lists[key] = self.lists.get(key, [])[start : end + 1]

    def lrange(self, key, start, end):
        return self.lists.get(key, [])[start : end + 1]
