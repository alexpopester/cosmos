# Issue 01: Ruby: `DEFAULT_ACCESSOR` end-to-end

**Type**: AFK
**Blocked by**: None — can start immediately

## What to build

Add `DEFAULT_ACCESSOR <ClassName> [arg...]` support to the Ruby implementation end-to-end. A target author adds one line to `target.txt` and every packet in that target automatically uses the named accessor — no `ACCESSOR` line needed in any cmd_tlm file. Per-packet `ACCESSOR` keywords still override the default. Targets without `DEFAULT_ACCESSOR` are unaffected.

This slice touches three layers:

- **`Target`**: parse the new keyword, store the class name and args, expose them in `as_json`
- **`PacketConfig`**: extend `process_file` with two optional trailing params (`default_accessor_class`, `default_accessor_args`); immediately after each `COMMAND`/`TELEMETRY` packet is created, apply the default accessor using the same `require_class` path as the per-packet `ACCESSOR` keyword; the subsequent per-packet `ACCESSOR` keyword overwrites it naturally
- **`System`**: pass `target.default_accessor` and `target.default_accessor_args` alongside `target.language` in the existing `process_file` call

## Acceptance criteria

- [ ] `target.txt` accepts `DEFAULT_ACCESSOR ClassName` with zero or more trailing args and stores the class name and args on `Target`
- [ ] An unknown keyword in `target.txt` still raises a `ConfigParser::Error`
- [ ] `Target#as_json` includes `default_accessor` (String or nil) and `default_accessor_args` (Array)
- [ ] Every `COMMAND` and `TELEMETRY` packet parsed from a target with `DEFAULT_ACCESSOR` has `packet.accessor.class` equal to the named class
- [ ] Constructor args passed to `DEFAULT_ACCESSOR` are forwarded to the accessor's initializer
- [ ] A per-packet `ACCESSOR` keyword overrides the target default for that packet only
- [ ] `SELECT_COMMAND` / `SELECT_TELEMETRY` patches do not re-apply the default to already-created packets
- [ ] A bad class name in `DEFAULT_ACCESSOR` raises a meaningful error at packet-construction time
- [ ] Targets without `DEFAULT_ACCESSOR` continue to use `BinaryAccessor` as before (no regression)
- [ ] rspec tests added to `spec/system/target_spec.rb` (parsing) and `spec/packets/packet_config_spec.rb` (application and override), following the existing temp-file config pattern

## Blocked by

None — can start immediately
