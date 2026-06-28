# Issue 02: Python: `DEFAULT_ACCESSOR` end-to-end

**Type**: AFK
**Blocked by**: None — can start immediately (parallel with Issue 01)

## What to build

Mirror the Ruby `DEFAULT_ACCESSOR` feature in the Python implementation end-to-end. A Python-language target author adds `DEFAULT_ACCESSOR <ClassName> [arg...]` to `target.txt` and every packet in that target automatically uses the named accessor. Per-packet `ACCESSOR` keywords still override the default. Targets without `DEFAULT_ACCESSOR` are unaffected.

This slice touches three layers:

- **`Target`**: parse the new keyword, store `default_accessor` (str or None) and `default_accessor_args` (list), expose them in `as_json`
- **`PacketConfig`**: extend `process_file` with two optional keyword params (`default_accessor_class=None`, `default_accessor_args=[]`); immediately after each `COMMAND`/`TELEMETRY` packet is created, apply the default accessor using the same `get_class_from_module` path as the per-packet `ACCESSOR` keyword; the subsequent per-packet `ACCESSOR` keyword overwrites it naturally
- **`System`**: pass `target.default_accessor` and `target.default_accessor_args` in the existing `process_file` call

## Acceptance criteria

- [ ] `target.txt` accepts `DEFAULT_ACCESSOR ClassName` with zero or more trailing args and stores them on `Target`
- [ ] An unknown keyword in `target.txt` still raises an exception
- [ ] `Target.as_json()` includes `default_accessor` (str or None) and `default_accessor_args` (list)
- [ ] Every `COMMAND` and `TELEMETRY` packet parsed from a target with `DEFAULT_ACCESSOR` has the expected accessor class set
- [ ] Constructor args passed to `DEFAULT_ACCESSOR` are forwarded to the accessor's initializer
- [ ] A per-packet `ACCESSOR` keyword overrides the target default for that packet only
- [ ] `SELECT_COMMAND` / `SELECT_TELEMETRY` patches do not re-apply the default to already-created packets
- [ ] A bad class name in `DEFAULT_ACCESSOR` raises a meaningful error at packet-construction time
- [ ] Targets without `DEFAULT_ACCESSOR` continue to use `BinaryAccessor` as before (no regression)
- [ ] pytest tests added to `test/system/test_target.py` (parsing) and `test/packets/test_packet_config.py` (application and override), following the existing temp-file config pattern

## Blocked by

None — can start immediately
