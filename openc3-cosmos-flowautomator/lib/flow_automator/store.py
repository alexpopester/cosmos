"""Persists flows and run history in Redis (Valkey).

Keys (per scope):
    flow_automator__<scope>__flows            hash: flow id -> flow JSON
    flow_automator__<scope>__runs__<flow id>  list: newest first, run summaries (no steps)
    flow_automator__<scope>__run_steps__<flow id>  hash: run id -> its steps (JSON list)
    flow_automator__<scope>__run_index        hash: run id -> flow id
    flow_automator__<scope>__versions__<flow id>  list: newest first, saved versions
    flow_automator__<scope>__audit            list: newest first, audit entries
    flow_automator__<scope>__meta             hash: "format" -> the newest format written here
    flow_automator__<scope>__<kind>           hash: id -> record JSON, for the contact
                                              planner's satellites, stations,
                                              activities and settings

Every record is written with a ``format`` stamp and upgraded when it's read
(migrations.py). A record in a newer format than this engine reads is skipped
and listed in ``unreadable``, never rewritten.

Valkey outages: appends (run history, the audit log, saved versions) and
``save_record(..., defer=True)`` writes don't fail when Valkey can't be
reached. They're kept in memory, in order, and written by ``flush()`` (the
engine calls it every few seconds) once Valkey is back. ``health()`` reports
how many are waiting. Everything else raises, so an operator's save fails
visibly instead of being silently kept only in memory. Writes that only
reduce activity (disarm, STOP ALL, pause) use ``defer`` too: they must never
fail (D28).

A delete drops any buffered write to the same record first, so a retried
write can't bring a deleted record back. In the running engine buffered
writes are made by its store thread (``inline_flush`` off), never by the
caller: the engine queues writes while holding its lock, and a slow or hung
Valkey must not hold that lock.
"""

import collections
import contextlib
import json
import threading
import time

from flow_automator.migrations import FORMAT_VERSION, NewerFormatError, stamp, upgrade


MAX_PENDING_WRITES = 5000  # beyond this the oldest buffered appends are dropped (and counted)


def _text(value):
    return value.decode() if isinstance(value, bytes) else value


class FlowStore:
    def __init__(self, redis, scope, history_limit=50):
        self.redis = redis
        self.scope = scope
        self.history_limit = history_limit
        self.flows_key = f"flow_automator__{scope}__flows"
        self.meta_key = f"flow_automator__{scope}__meta"
        self.unreadable = {}  # (kind, id) -> the format it was saved in
        self._pending = collections.deque()  # (sequence, ("list", key, value, limit) | ("hash", key, field, value))
        self._summaries = {}  # flow id -> its runs' summaries, newest first (loaded on first use)
        self._pending_lock = threading.Lock()
        self._flush_lock = threading.Lock()  # one flush at a time, so writes land in order
        self._sequence = 0
        self._wake = threading.Event()  # set when a write is queued, for the engine's store thread
        self.inline_flush = True  # write queued writes at once in the caller's thread (tests, no store thread)
        self.dropped_writes = 0
        self.last_error = None
        self.failing_since = None

    # ---- reading ---------------------------------------------------------------

    def _load(self, kind, record_id, text):
        try:
            return upgrade(kind, json.loads(_text(text)))
        except NewerFormatError as error:
            self.unreadable[(kind, record_id)] = error.found
            return None

    def _load_hash(self, kind, key):
        raw = self.redis.hgetall(key) or {}
        result = {}
        for field, value in raw.items():
            record = self._load(kind, _text(field), value)
            if record is not None:
                result[_text(field)] = record
        return result

    def _load_list(self, kind, key, start, end):
        items = (
            self._load(kind, f"{key}[{n}]", item) for n, item in enumerate(self.redis.lrange(key, start, end) or [])
        )
        return [item for item in items if item is not None]

    # ---- format ------------------------------------------------------------------

    def stored_format(self):
        """The newest format anything has written to this scope (0 before formats existed)."""
        meta = {_text(k): v for k, v in (self.redis.hgetall(self.meta_key) or {}).items()}
        raw = meta.get("format")
        try:
            return int(_text(raw) or 0)
        except (TypeError, ValueError):
            return 0

    def note_format(self):
        """Record that this engine writes FORMAT_VERSION here, unless something newer already did."""
        if self.stored_format() < FORMAT_VERSION:
            self.redis.hset(self.meta_key, "format", str(FORMAT_VERSION))

    # ---- flows -------------------------------------------------------------------

    def all_flows(self):
        return self._load_hash("flow", self.flows_key)

    def save_flow(self, flow, defer=False):
        """Write a flow. With ``defer``, a write Valkey refuses is kept and retried by flush()."""
        value = json.dumps(stamp(flow))
        if defer:
            self._queue(("hash", self.flows_key, flow["id"], value))
            return
        self.redis.hset(self.flows_key, flow["id"], value)

    def delete_flow(self, flow_id):
        # Run history and versions stay, so a deleted flow can be restored and its runs looked up
        self._drop_pending(self.flows_key, flow_id)
        self.redis.hdel(self.flows_key, flow_id)

    def _versions_key(self, flow_id):
        return f"flow_automator__{self.scope}__versions__{flow_id}"

    # A version's metadata (who, when, note, summary) goes in the flow's list and its flow body in a hash beside
    # it, so listing versions doesn't read every body: a 2,000-node flow with 200 versions was a 92 MB list and
    # listing it took 2.2 s (EVALUATION-2026-11-05 B46).

    def _version_bodies_key(self, flow_id):
        return f"flow_automator__{self.scope}__version_bodies__{flow_id}"

    def add_version(self, flow_id, entry, limit):
        meta = {k: v for k, v in entry.items() if k != "flow"}
        self._queue(
            (
                "split",
                self._versions_key(flow_id),
                json.dumps(stamp(meta)),
                limit,
                self._version_bodies_key(flow_id),
                str(entry["version"]),
                json.dumps(stamp(entry.get("flow") or {})),
                None,
                None,
                "version",
            )
        )

    def version_list(self, flow_id, limit=None):
        """A flow's saved versions, newest first, without their flow bodies."""
        end = (limit - 1) if limit else -1
        return self._load_list("version", self._versions_key(flow_id), 0, end)

    def version_body(self, flow_id, version):
        raw = self.redis.hget(self._version_bodies_key(flow_id), str(version))
        return self._load("flow", f"{flow_id}@{version}", raw) if raw else None

    def versions(self, flow_id, limit=None):
        """A flow's saved versions with their flow bodies (backups, the latest version)."""
        return [
            {**meta, "flow": self.version_body(flow_id, meta["version"])} for meta in self.version_list(flow_id, limit)
        ]

    def migrate_version_history(self, flow_ids):
        """Once: move saved versions from the old layout (whole flows in one list) to metadata + bodies."""
        meta = {_text(k): _text(v) for k, v in (self.redis.hgetall(self.meta_key) or {}).items()}
        if meta.get("versions_layout") == "2":
            return 0
        moved = 0
        for flow_id in flow_ids:
            entries = self._load_list("version", self._versions_key(flow_id), 0, -1)
            if not any("flow" in e for e in entries):
                continue
            self.redis.delete(self._versions_key(flow_id))
            for entry in reversed(entries):  # oldest first, so the list ends newest first
                body = entry.pop("flow", None) or {}
                self.redis.hset(self._version_bodies_key(flow_id), str(entry["version"]), json.dumps(stamp(body)))
                self.redis.lpush(self._versions_key(flow_id), json.dumps(stamp(entry)))
                moved += 1
        self.redis.hset(self.meta_key, "versions_layout", "2")
        return moved

    def purge_flow_history(self, flow_id):
        """Delete everything kept for a deleted flow: its versions, runs (with steps and index entries)."""
        for summary in self.run_summaries(flow_id):
            self.redis.hdel(self._run_index_key, summary.get("id"))
        for key in (
            self._versions_key(flow_id),
            self._version_bodies_key(flow_id),
            self._runs_key(flow_id),
            self._steps_key(flow_id),
        ):
            self.redis.delete(key)
        with self._pending_lock:
            self._summaries.pop(flow_id, None)

    # ---- run history ---------------------------------------------------------------

    # A run's summary (no steps) goes in the flow's list, its steps in a hash beside it, and run id -> flow id in
    # an index, so listing runs reads small summaries and finding one run is a lookup, not a scan of every flow
    # (EVALUATION-2026-11-05 B44). Summaries are also cached in memory per flow (one engine per scope).

    def _runs_key(self, flow_id):
        return f"flow_automator__{self.scope}__runs__{flow_id}"

    def _steps_key(self, flow_id):
        return f"flow_automator__{self.scope}__run_steps__{flow_id}"

    @property
    def _run_index_key(self):
        return f"flow_automator__{self.scope}__run_index"

    def add_run(self, flow_id, run_json):
        summary = {k: v for k, v in run_json.items() if k != "steps"}
        steps = run_json.get("steps") or []
        with self._pending_lock:
            cached = self._summaries.get(flow_id)
            if cached is not None:
                cached.insert(0, stamp(dict(summary)))
                del cached[self.history_limit :]
        self._queue(
            (
                "split",
                self._runs_key(flow_id),
                json.dumps(stamp(summary)),
                self.history_limit,
                self._steps_key(flow_id),
                summary.get("id"),
                json.dumps(steps),
                self._run_index_key,
                flow_id,
                "id",
            )
        )

    def run_summaries(self, flow_id, limit=None):
        """A flow's runs, newest first, without their steps."""
        with self._pending_lock:
            cached = self._summaries.get(flow_id)
        if cached is None:
            cached = self._load_list("run", self._runs_key(flow_id), 0, self.history_limit - 1)
            with self._pending_lock:
                cached = self._summaries.setdefault(flow_id, cached)
        return [dict(r) for r in cached[: (limit or self.history_limit)]]

    def run_steps(self, flow_id, run_id):
        raw = self.redis.hget(self._steps_key(flow_id), run_id)
        return json.loads(_text(raw)) if raw else []

    def find_run(self, run_id):
        """A finished run with its steps, by id (None if it isn't in history)."""
        flow_id = self.redis.hget(self._run_index_key, run_id)
        if not flow_id:
            return None
        flow_id = _text(flow_id)
        summary = next((r for r in self.run_summaries(flow_id) if r.get("id") == run_id), None)
        if summary is None:
            return None
        return {**summary, "steps": self.run_steps(flow_id, run_id)}

    def runs(self, flow_id, limit=None):
        """A flow's runs with their steps (backups)."""
        return [{**r, "steps": self.run_steps(flow_id, r.get("id"))} for r in self.run_summaries(flow_id, limit)]

    def migrate_run_history(self, flow_ids):
        """Once: move run history from the old layout (whole runs in one list) to summaries + steps + index.

        Returns how many runs it moved. Marked done in the meta hash, so it runs once per scope.
        """
        meta = {_text(k): _text(v) for k, v in (self.redis.hgetall(self.meta_key) or {}).items()}
        if meta.get("runs_layout") == "2":
            return 0
        moved = 0
        for flow_id in flow_ids:
            records = self._load_list("run", self._runs_key(flow_id), 0, -1)
            if not any("steps" in r for r in records):
                continue
            self.redis.delete(self._runs_key(flow_id))
            for record in reversed(records):  # oldest first, so the list ends newest first
                steps = record.pop("steps", None) or []
                self.redis.hset(self._steps_key(flow_id), record.get("id"), json.dumps(steps))
                self.redis.hset(self._run_index_key, record.get("id"), flow_id)
                self.redis.lpush(self._runs_key(flow_id), json.dumps(stamp(record)))
                moved += 1
            self._summaries.pop(flow_id, None)
        self.redis.hset(self.meta_key, "runs_layout", "2")
        return moved

    # ---- audit log -----------------------------------------------------------

    def add_audit(self, entry, limit):
        self._append(f"flow_automator__{self.scope}__audit", entry, limit)

    def audit_entries(self):
        return self._load_list("audit", f"flow_automator__{self.scope}__audit", 0, -1)

    # ---- contact planner records ---------------------------------------------

    def _records_key(self, kind):
        return f"flow_automator__{self.scope}__{kind}"

    def records(self, kind):
        return self._load_hash(kind, self._records_key(kind))

    def save_record(self, kind, record_id, record, defer=False):
        """Write a record. With ``defer``, a write Valkey refuses is kept and retried by flush()."""
        value = json.dumps(stamp(record))
        if defer:
            self._queue(("hash", self._records_key(kind), record_id, value))
            return
        self.redis.hset(self._records_key(kind), record_id, value)

    def delete_record(self, kind, record_id, defer=False):
        """Delete a record. With ``defer`` the delete is queued behind (and in order with) buffered writes."""
        key = self._records_key(kind)
        if defer:
            self._queue(("hdel", key, record_id))
            return
        self._drop_pending(key, record_id)
        self.redis.hdel(key, record_id)

    def _drop_pending(self, key, field):
        """Forget buffered writes to one hash field: retrying them later would undo a delete."""
        with self._pending_lock:
            kept = [item for item in self._pending if not (item[1][0] == "hash" and item[1][1:3] == (key, field))]
            if len(kept) != len(self._pending):
                self._pending = collections.deque(kept)

    # ---- writes that survive an outage ------------------------------------------------

    def _append(self, key, entry, limit):
        self._queue(("list", key, json.dumps(stamp(entry)), limit))

    def _queue(self, write):
        with self._pending_lock:
            self._sequence += 1
            self._pending.append((self._sequence, write))
            while len(self._pending) > MAX_PENDING_WRITES:
                self._pending.popleft()
                self.dropped_writes += 1
            failing = self.failing_since is not None
        if not self.inline_flush:
            self._wake.set()
        elif not failing:
            # While Valkey is away the engine's periodic flush retries: a run mustn't wait on a timeout per write
            self.flush(wait=False)

    def wait_for_writes(self, timeout):
        """Block until a write is queued or the timeout passes (the engine's store thread)."""
        self._wake.wait(timeout)
        self._wake.clear()

    def flush(self, wait=True):
        """Write what's waiting, oldest first; stop at the first failure. Returns how many are left.

        Valkey is called outside the queue's lock, so a write arriving during a slow retry isn't held up.
        """
        while True:
            if not self._flush_lock.acquire(blocking=wait):
                return None  # another flush is writing them
            try:
                left = self._flush()
            finally:
                self._flush_lock.release()
            with self._pending_lock:
                # A write queued just as that flush finished found it busy and left it to us
                if left or not self._pending:
                    return left

    def _flush(self):
        while True:
            with self._pending_lock:
                if not self._pending:
                    self.last_error = None
                    self.failing_since = None
                    return 0
                sequence, write = self._pending[0]
            try:
                if write[0] == "split":
                    # A summary in a list, its body in a hash (and maybe an index entry): runs and versions.
                    # Body and index first: writing them twice is harmless; the summary makes it visible
                    _kind, list_key, summary, _limit, body_key, field, body, index_key, index_value, _id_field = write
                    self.redis.hset(body_key, field, body)
                    if index_key:
                        self.redis.hset(index_key, field, index_value)
                    self.redis.lpush(list_key, summary)
                elif write[0] == "list":
                    self.redis.lpush(write[1], write[2])
                elif write[0] == "hdel":
                    self.redis.hdel(write[1], write[2])
                else:
                    self.redis.hset(write[1], write[2], write[3])
            except Exception as error:  # noqa: BLE001 - Valkey is away; keep it for the next flush
                with self._pending_lock:
                    self.last_error = str(error) or type(error).__name__
                    if self.failing_since is None:
                        self.failing_since = time.time()
                    return len(self._pending)
            if write[0] == "split":
                with contextlib.suppress(Exception):  # can wait for the next write, like a list trim
                    self._trim_split(write[1], write[3], write[4], write[7], write[9])
            elif write[0] == "list":
                # Trimming can wait for the next append: retrying the push would add the entry twice
                with contextlib.suppress(Exception):
                    self.redis.ltrim(write[1], 0, write[3] - 1)
            with self._pending_lock:
                if self._pending and self._pending[0][0] == sequence:
                    self._pending.popleft()

    def _trim_split(self, list_key, limit, body_key, index_key, id_field):
        """Drop the entries beyond the limit, with their bodies and index entries."""
        for raw in self.redis.lrange(list_key, limit, -1) or []:
            with contextlib.suppress(ValueError, TypeError):
                field = str(json.loads(_text(raw)).get(id_field))
                self.redis.hdel(body_key, field)
                if index_key:
                    self.redis.hdel(index_key, field)
        self.redis.ltrim(list_key, 0, limit - 1)

    def health(self):
        with self._pending_lock:
            pending = len(self._pending)
        return {
            "ok": pending == 0 and self.last_error is None,
            "pending_writes": pending,
            "dropped_writes": self.dropped_writes,
            "error": self.last_error,
            "failing_since": self.failing_since,
            "format": FORMAT_VERSION,
            "unreadable": [
                {"kind": kind, "id": record_id, "format": found} for (kind, record_id), found in self.unreadable.items()
            ],
        }


class MemoryRedis:
    """The handful of Redis commands FlowStore uses, in memory. For tests and local runs."""

    def __init__(self):
        self.hashes = {}
        self.lists = {}

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

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
        items = self.lists.get(key, [])
        return items[start:] if end == -1 else items[start : end + 1]
