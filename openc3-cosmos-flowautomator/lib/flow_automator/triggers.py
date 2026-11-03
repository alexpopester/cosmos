"""Watches armed flows' trigger nodes and starts runs when they fire.

One background thread ticks every ``period`` seconds (default 0.25). Each tick
reads all telemetry the armed triggers need in one batched request, then:

* ``trigger_tlm``      fires when its condition goes from false to true (edge),
                       never just because it is already true when armed
* ``trigger_schedule`` fires every N seconds, or on a cron schedule (UTC)
* ``trigger_packet``   fires when a packet's received count goes up
* ``trigger_limits``   fires when an item's limits state changes into one of
                       the chosen states (default the RED states)

Editing a flow resets the state of its triggers.
"""

import datetime
import json
import threading
import time

from .cron import Cron
from .expressions import Evaluator, ExpressionError, normalize_item, telemetry_refs


DEFAULT_LIMITS_STATES = ["RED", "RED_LOW", "RED_HIGH"]


class TriggerManager:
    def __init__(self, engine, period=0.25, clock=time.time):
        self.engine = engine
        self.period = period
        self.clock = clock
        self.states = {}
        self._stop = threading.Event()
        self._thread = None
        self.last_tick = None
        self.last_error = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="flow-triggers", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self):
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.tick()
                self.last_error = None
            except Exception as error:  # noqa: BLE001 - keep watching even if one tick fails
                if str(error) != self.last_error:
                    self.engine.log("error", f"Flow trigger poll failed: {error}")
                self.last_error = str(error)
            self._stop.wait(max(0.0, self.period - (time.monotonic() - started)))

    def status(self):
        return {
            "watching": len(self.states),
            "last_tick": self.last_tick,
            "error": self.last_error,
            "errors": {
                f"{flow_id}/{node_id}": state["error"]
                for (flow_id, node_id), state in self.states.items()
                if state.get("error")
            },
        }

    # ---- one poll ----------------------------------------------------------

    def tick(self):
        now = self.clock()
        self.last_tick = now
        watched = []
        for flow in self.engine.armed_flows():
            for node in flow.get("nodes") or []:
                if node.get("type", "").startswith("trigger_") and node["type"] != "trigger_manual":
                    watched.append((flow, node))

        # Forget triggers that are gone, disarmed or edited
        current = {}
        for flow, node in watched:
            key = (flow["id"], node["id"])
            signature = json.dumps(node.get("data") or {}, sort_keys=True) + str(flow.get("updated_at"))
            state = self.states.get(key)
            if state is None or state["signature"] != signature:
                state = {"signature": signature, "initialized": False, "error": None}
            current[key] = state
        self.states = current
        if not watched:
            return

        values = self._read_telemetry(watched)
        counts = self._read_counts(watched)
        for flow, node in watched:
            state = self.states[(flow["id"], node["id"])]
            try:
                fired, reason, variables = self._check(flow, node, state, now, values, counts)
                state["error"] = None
            except Exception as error:  # noqa: BLE001 - a bad trigger mustn't stop the others
                if state.get("error") != str(error):
                    self.engine.log("warn", f"Flow '{flow.get('name')}' trigger '{node['id']}': {error}")
                state["error"] = str(error)
                continue
            if fired:
                try:
                    self.engine.trigger(flow["id"], node["id"], reason, variables)
                except Exception as error:  # noqa: BLE001
                    self.engine.log("error", f"Flow '{flow.get('name')}' could not start: {error}")

    def _read_telemetry(self, watched):
        """Batch read every (item, type) the tlm and limits triggers need. Returns {(item, type): (value, limits)}."""
        refs = set()
        for _flow, node in watched:
            data = node.get("data") or {}
            try:
                if node["type"] == "trigger_tlm":
                    refs |= telemetry_refs(data.get("condition"))
                elif node["type"] == "trigger_limits":
                    refs.add((normalize_item(data.get("item")), "CONVERTED"))
            except ExpressionError:
                pass  # reported per trigger in _check
        refs = sorted(refs)
        if not refs:
            return {}
        backend = self.engine.backend
        try:
            return dict(zip(refs, backend.tlm_values(refs), strict=True))
        except Exception:  # noqa: BLE001 - one bad item fails the batch; read one by one to isolate it
            values = {}
            for ref in refs:
                try:
                    values[ref] = backend.tlm_values([ref])[0]
                except Exception as error:  # noqa: BLE001
                    values[ref] = error
            return values

    def _read_counts(self, watched):
        packets = sorted(
            {
                ((n.get("data") or {}).get("target", "").upper(), (n.get("data") or {}).get("packet", "").upper())
                for _f, n in watched
                if n["type"] == "trigger_packet"
            }
        )
        if not packets:
            return {}
        try:
            return dict(zip(packets, self.engine.backend.tlm_counts(packets), strict=True))
        except Exception as error:  # noqa: BLE001
            return dict.fromkeys(packets, error)

    def _check(self, flow, node, state, now, values, counts):
        """Returns (fired, reason, variables)."""
        data = node.get("data") or {}
        kind = node["type"]

        if kind == "trigger_tlm":

            def lookup(item, value_type):
                value = values.get((item, value_type))
                if value is None:
                    # Not batched (computed item name): read it directly
                    return self.engine.backend.tlm(item, value_type)
                if isinstance(value, Exception):
                    raise value
                return value[0]

            evaluator = Evaluator(dict(flow.get("variables") or {}), lookup)
            result = evaluator.truthy(data.get("condition"))
            previous = state.get("value")
            state["value"] = result
            if not state["initialized"]:
                state["initialized"] = True
                return False, "", None
            cooldown = float(data.get("cooldown") or 0)
            if result and not previous and now - state.get("last_fired", 0) >= cooldown:
                state["last_fired"] = now
                return True, f"Telemetry: {data.get('condition')}", {}
            return False, "", None

        if kind == "trigger_schedule":
            if data.get("mode", "interval") == "cron":
                minute = int(now // 60)
                if not state["initialized"]:
                    state["initialized"] = True
                    state["minute"] = minute
                    state["cron"] = Cron(data.get("cron"))
                    return False, "", None
                if minute == state["minute"]:
                    return False, "", None
                state["minute"] = minute
                moment = datetime.datetime.fromtimestamp(minute * 60, tz=datetime.timezone.utc)
                if state["cron"].matches(moment):
                    return True, f"Schedule: cron '{data.get('cron')}'", {}
                return False, "", None
            interval = float(data.get("interval"))
            if not state["initialized"]:
                state["initialized"] = True
                state["next"] = now + interval
                return False, "", None
            if now >= state["next"]:
                state["next"] += interval
                if state["next"] <= now:  # fell behind (engine paused); don't burst
                    state["next"] = now + interval
                return True, f"Schedule: every {interval:g}s", {}
            return False, "", None

        if kind == "trigger_packet":
            packet = (data.get("target", "").upper(), data.get("packet", "").upper())
            count = counts.get(packet)
            if isinstance(count, Exception):
                raise count
            if count is None:
                raise RuntimeError(f"No count for {packet[0]} {packet[1]}")
            previous = state.get("count")
            state["count"] = count
            if not state["initialized"]:
                state["initialized"] = True
                return False, "", None
            if previous is not None and count > previous:
                return True, f"Packet {packet[0]} {packet[1]} received", {"packet_count": count}
            return False, "", None

        if kind == "trigger_limits":
            item = normalize_item(data.get("item"))
            value = values.get((item, "CONVERTED"))
            if isinstance(value, Exception):
                raise value
            if value is None:
                raise RuntimeError(f"No value for {item}")
            limits_state = value[1] if len(value) > 1 else None
            previous = state.get("limits")
            state["limits"] = limits_state
            if not state["initialized"]:
                state["initialized"] = True
                return False, "", None
            wanted = data.get("states") or DEFAULT_LIMITS_STATES
            if limits_state != previous and limits_state in wanted:
                return (
                    True,
                    f"Limits: {item} {previous} -> {limits_state}",
                    {"limits_state": limits_state, "trigger_value": value[0]},
                )
            return False, "", None

        return False, "", None
