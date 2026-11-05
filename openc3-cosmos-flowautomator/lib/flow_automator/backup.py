"""Back up everything the engine keeps for a scope, and restore it.

A backup is one JSON document (``KIND``, stamped with the storage format):
flows with their saved versions, satellites with their TLEs, stations, rules,
TLE input settings and alert settings. ``include_history`` adds run history
and the audit log, for the record; restoring ignores them.

Webhook URLs are left out (they often carry secrets, and a read-only user can
take a backup). Scheduled activities are in the backup for reference but
aren't restored: rules recreate theirs, and one-off activities are listed so
they can be scheduled again.

Restoring merges: nothing that isn't in the backup is deleted. It is careful
not to start anything:

- every restored flow is left **disarmed**, with the reason shown;
- the contact scheduler is **paused** until someone reviews and resumes it;
- everything goes through the same checks as the API, so a hand-edited
  backup can't store what the tool would refuse.

Flows keep their ids (rules, activities and sub-flow nodes refer to them).
Satellites and stations are matched by id, then by name; rules are remapped
to whatever they matched.
"""

import copy
import re
import time

from .audit import user_name
from .migrations import FORMAT_VERSION, NewerFormatError, record_format, upgrade


KIND = "flow_automator_backup"
_ID = re.compile(r"[\w-]{1,64}")


class BackupError(Exception):
    status = 400


def make_backup(engine, include_history=False, now=None):
    planner = engine.planner
    store = engine.store
    with engine.lock:
        flows = copy.deepcopy(list(engine.flows.values()))
    for flow in flows:
        flow["armed"] = False  # a restore never arms; don't suggest it does
    versions = {flow["id"]: store.versions(flow["id"]) for flow in flows}
    with planner.lock:
        satellites = copy.deepcopy(list(planner.satellites.values()))
        stations = copy.deepcopy(list(planner.stations.values()))
        rules = copy.deepcopy(list(planner.rules.values()))
        activities = copy.deepcopy(list(planner.activities.values()))
    with engine.alerts.lock:
        alerts = copy.deepcopy(engine.alerts.settings)
    for hook in alerts.get("webhooks", []):
        hook["url_masked"] = _mask(hook.pop("url", ""))
    backup = {
        "kind": KIND,
        "format": FORMAT_VERSION,
        "scope": store.scope,
        "created_at": now if now is not None else time.time(),
        "flows": flows,
        "versions": versions,
        "satellites": satellites,
        "stations": stations,
        "rules": rules,
        "activities": activities,
        "tle_inputs": copy.deepcopy(planner.intake.settings),
        "tle_sources": copy.deepcopy(planner.fetcher.settings),  # secret names only, never the credentials
        "alerts": alerts,
    }
    if include_history:
        backup["runs"] = {flow["id"]: store.runs(flow["id"]) for flow in flows}
        backup["audit"] = store.audit_entries()
    return backup


def _mask(url):
    from .alerts import _mask as mask  # one definition of how URLs are shown

    return mask(url)


def _items(backup, key):
    value = backup.get(key) or []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise BackupError(f"'{key}' in the backup must be a list of objects")
    return value


def check_backup(backup):
    """Raise BackupError unless this looks like a backup this engine can read."""
    if not isinstance(backup, dict) or backup.get("kind") != KIND:
        raise BackupError('That isn\'t a Flow Automator backup (it has no "kind": "flow_automator_backup")')
    found = record_format(backup)
    if found > FORMAT_VERSION:
        raise BackupError(
            f"The backup was made by a newer Flow Automator (format {found}; this one reads up to {FORMAT_VERSION})"
        )
    for key in ("flows", "satellites", "stations", "rules"):
        _items(backup, key)
    for key in ("versions", "tle_inputs", "tle_sources", "alerts"):
        if backup.get(key) is not None and not isinstance(backup[key], dict):
            raise BackupError(f"'{key}' in the backup must be an object")
    return found


def restore_backup(engine, backup, user=None):
    """Merge a backup into this scope. Returns what happened, item by item."""
    found = check_backup(backup)
    planner = engine.planner
    result = {
        "flows": {"created": [], "updated": [], "unchanged": [], "failed": []},
        "satellites": {"created": [], "updated": [], "failed": []},
        "stations": {"created": [], "updated": [], "failed": []},
        "rules": {"created": [], "updated": [], "failed": []},
        "settings": [],
        "skipped": [],
    }
    reason = f"Restored from a backup by {user_name(user)}: review the flows and the schedule, then resume"
    # Pause first, so nothing restored can start before someone has looked at it
    planner.set_paused(True, reason)

    versions = backup.get("versions") or {}
    for flow in _items(backup, "flows"):
        _restore_flow(engine, flow, versions.get(flow.get("id")) or [], found, user, result["flows"])

    sat_ids = _restore_planner(
        backup, "satellites", planner.satellites, planner, _satellite_payload, planner.save_satellite, user, result
    )
    station_ids = _restore_planner(
        backup, "stations", planner.stations, planner, _station_payload, planner.save_station, user, result
    )
    for rule in _items(backup, "rules"):
        name = rule.get("name") or rule.get("id")
        try:
            payload = _strip(rule, ("id", "created_at", "created_by", "updated_at", "updated_by", "last_error"))
            payload["satellite_ids"] = [sat_ids.get(i, i) for i in rule.get("satellite_ids") or []]
            payload["station_ids"] = [station_ids.get(i, i) for i in rule.get("station_ids") or []]
            existing = rule.get("id") if rule.get("id") in planner.rules else None
            if existing is None:
                existing = next((r["id"] for r in planner.rules.values() if r.get("name") == rule.get("name")), None)
            saved = planner.save_rule(payload, existing, user=user)["rule"]
            result["rules"]["updated" if existing else "created"].append(saved["name"])
        except Exception as error:  # noqa: BLE001 - report it and carry on with the rest
            result["rules"]["failed"].append({"name": name, "error": str(error)})

    if isinstance(backup.get("tle_inputs"), dict):
        try:
            planner.intake.save_settings(
                {
                    k: v
                    for k, v in backup["tle_inputs"].items()
                    if k in ("stash_key", "files", "packets", "max_jump_km", "poll_seconds")
                }
            )
            result["settings"].append("TLE inputs")
        except Exception as error:  # noqa: BLE001
            result["skipped"].append(f"TLE input settings: {error}")
    if isinstance(backup.get("tle_sources"), dict):
        try:
            planner.fetcher.save_settings(backup["tle_sources"])
            result["settings"].append("TLE sources")
        except Exception as error:  # noqa: BLE001
            result["skipped"].append(f"TLE source settings: {error}")
    if isinstance(backup.get("alerts"), dict):
        _restore_alerts(engine, backup["alerts"], result)

    one_off = [
        a
        for a in _items(backup, "activities")
        if not a.get("rule_id") and a.get("status") in ("scheduled", "needs_review")
    ]
    if one_off:
        result["skipped"].append(
            f"{len(one_off)} one-off scheduled activities weren't restored (contacts are predicted again); "
            "schedule them again: "
            + ", ".join(sorted({a.get("label") or a.get("template_name") or a["id"] for a in one_off}))
        )
    if backup.get("runs") or backup.get("audit"):
        result["skipped"].append("Run history and the audit log in the backup are kept for the record, not restored")
    result["scheduler_paused"] = True
    return result


def _strip(record, keys):
    return {k: copy.deepcopy(v) for k, v in record.items() if k not in keys}


def _restore_flow(engine, flow, versions, found, user, result):
    name = flow.get("name") or flow.get("id")
    try:
        flow = upgrade("flow", flow, found if "format" not in flow else None)
        flow_id = flow.get("id")
        if not isinstance(flow_id, str) or not _ID.fullmatch(flow_id):
            raise BackupError("the flow has no usable id")
        body = _strip(flow, ("armed", "disarmed_reason"))
        with engine.lock:
            existing = engine.flows.get(flow_id)
            if existing is not None and existing.get("armed"):
                engine.set_armed(flow_id, False)
        note = "Restored from a backup"
        if existing is not None:
            before = existing.get("version")
            saved, _problems = engine.save_flow(body, flow_id, user, note, force=True)
            kind = "unchanged" if saved.get("version") == before else "updated"
        else:
            if not engine.store.versions(flow_id, 1):
                # Bring its history along (oldest first, so the newest ends up on top)
                for entry in reversed([v for v in versions if isinstance(v, dict)][: engine.versions.limit]):
                    try:
                        engine.store.add_version(
                            flow_id,
                            upgrade("version", entry, found if "format" not in entry else None),
                            engine.versions.limit,
                        )
                    except NewerFormatError:
                        continue
            saved, _problems = engine._recreate(flow_id, body, user, note, None)
            kind = "created"
        with engine.lock:
            current = engine.flows.get(flow_id)
            if current is not None:
                current["disarmed_reason"] = "Restored from a backup: review it, then arm it"
                engine.store.save_flow(current)
        result[kind].append(saved.get("name") or name)
    except Exception as error:  # noqa: BLE001 - report it and carry on with the rest
        result["failed"].append({"name": name, "error": str(error)})


def _restore_planner(backup, key, current, planner, to_payload, save, user, result):
    """Satellites or stations: returns {backup id: id here}."""
    ids = {}
    for record in _items(backup, key):
        name = record.get("name") or record.get("id")
        try:
            with planner.lock:
                existing = record.get("id") if record.get("id") in current else None
                if existing is None:
                    existing = next((r["id"] for r in current.values() if r.get("name") == record.get("name")), None)
            saved = save(to_payload(record), existing, user=user)
            ids[record.get("id")] = saved["id"]
            result[key]["updated" if existing else "created"].append(saved["name"])
        except Exception as error:  # noqa: BLE001
            result[key]["failed"].append({"name": name, "error": str(error)})
    return ids


def _satellite_payload(sat):
    payload = {
        k: copy.deepcopy(sat[k])
        for k in ("name", "target", "enabled", "catalog", "preferred_source", "tle_source")
        if k in sat
    }
    tle = sat.get("tle") or {}
    if tle.get("line1") and tle.get("line2"):
        payload["tle"] = "\n".join(line for line in (tle.get("name"), tle["line1"], tle["line2"]) if line)
        payload["source"] = tle.get("source") or "manual"
    return payload


def _station_payload(station):
    return _strip(station, ("id", "created_at", "created_by", "updated_at", "updated_by"))


def _restore_alerts(engine, saved, result):
    alerts = engine.alerts
    with alerts.lock:
        current = copy.deepcopy(alerts.settings)
    current_ids = {hook["id"] for hook in current.get("webhooks", [])}
    missing = [
        hook.get("name") or hook.get("id") for hook in saved.get("webhooks") or [] if hook.get("id") not in current_ids
    ]
    payload = {
        "cosmos": saved.get("cosmos", current.get("cosmos")),
        "base_url": saved.get("base_url", current.get("base_url")),
        "approval_escalate_after": saved.get("approval_escalate_after", current.get("approval_escalate_after")),
        "webhooks": current.get("webhooks", []),  # URLs aren't in backups: keep the ones set up here
    }
    try:
        alerts.save_settings(payload)
        result["settings"].append("alerts")
    except Exception as error:  # noqa: BLE001
        result["skipped"].append(f"Alert settings: {error}")
    if missing:
        result["skipped"].append(
            "Webhooks aren't restored (backups don't hold their URLs); add them again: " + ", ".join(missing)
        )
