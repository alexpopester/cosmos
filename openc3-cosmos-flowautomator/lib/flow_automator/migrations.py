"""The format of what the engine stores, and how older formats are upgraded.

Everything the engine writes to Valkey (flows, their versions, run history,
the audit log and the contact planner's records) is stamped with
``FORMAT_VERSION`` as a ``"format"`` key. The stamp lives only in Valkey and
in backups: ``upgrade()`` removes it when a record is read, so the rest of the
engine never sees it.

When a change renames or reshapes a stored field:

1. bump ``FORMAT_VERSION``;
2. add a function to ``STEPS[kind]`` that takes a record in the previous
   format and returns it in the new one (``STEPS[kind][n]`` upgrades format n
   to n + 1). Kinds without a step for n are copied unchanged;
3. add a test that loads a record in the old format.

A saved version holds a whole flow, and a scheduled activity holds the flow
snapshot it runs plus snapshots of its sub-flows (``NESTED``). They're
upgraded with the flow steps, so a flow change needs no separate version or
activity step, and the snapshots that actually run are never left behind.

Steps must be pure and must accept anything an older engine could have
written, including fields that were missing then. Records saved before
formats existed have no stamp and count as format 0.

A record in a *newer* format than this engine knows (someone downgraded the
plugin) raises ``NewerFormatError``: the engine leaves it alone in Valkey
rather than guessing, and says so, so reinstalling the newer plugin gets it
back.
"""

FORMAT_VERSION = 1


def _flow_0(flow):
    # Format 1 only introduced the stamp: flows from before it load as they are
    return flow


STEPS = {
    "flow": {0: _flow_0},
}

# Records that carry another kind inside them, saved in the same format: upgraded along with them.
# ("map", kind): a dict of id -> record of that kind.
NESTED = {
    "version": {"flow": "flow"},
    "activities": {"flow": "flow"},
    "flow": {"subflow_snapshots": ("map", "flow")},
}


class NewerFormatError(Exception):
    def __init__(self, kind, found):
        super().__init__(
            f"This {kind} was saved by a newer Flow Automator (format {found}; this one reads up to "
            f"{FORMAT_VERSION}). Install that version again to use it."
        )
        self.kind = kind
        self.found = found


def record_format(record):
    try:
        return int(record.get("format") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def upgrade(kind, record, found=None):
    """Return ``record`` in the current format, without its ``format`` stamp."""
    if not isinstance(record, dict):
        return record
    found = record_format(record) if found is None else found
    if found > FORMAT_VERSION:
        raise NewerFormatError(kind, found)
    record = dict(record)
    record.pop("format", None)
    for version in range(found, FORMAT_VERSION):
        step = STEPS.get(kind, {}).get(version)
        if step is not None:
            record = step(record)
    for key, nested_kind in NESTED.get(kind, {}).items():
        value = record.get(key)
        if not isinstance(value, dict):
            continue
        if isinstance(nested_kind, tuple):
            record[key] = {k: upgrade(nested_kind[1], v, found) for k, v in value.items()}
        else:
            record[key] = upgrade(nested_kind, value, found)
    return record


def stamp(record):
    """The record as it's written: with the current format."""
    return {**record, "format": FORMAT_VERSION} if isinstance(record, dict) else record
