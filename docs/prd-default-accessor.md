# PRD: Target-Level Default Accessor

## Problem Statement

When a target communicates entirely over a non-binary format (e.g., JSON), every `COMMAND` and `TELEMETRY` packet definition in its cmd_tlm files must include an `ACCESSOR` line. This is repetitive, easy to forget, and forces accessor concerns into packet definitions that would otherwise be purely semantic. There is no way to declare "this entire target speaks JSON" in one place.

## Solution

Add a `DEFAULT_ACCESSOR` keyword to `target.txt` that sets the accessor for every packet in the target. Individual packet definitions can still override it with their own `ACCESSOR` keyword. Targets that don't specify `DEFAULT_ACCESSOR` continue to use `BinaryAccessor` as today.

**Example `target.txt`:**
```
LANGUAGE python
DEFAULT_ACCESSOR JsonAccessor
```

With this in place, no `ACCESSOR` line is needed in any cmd_tlm file — every packet automatically uses `JsonAccessor`.

## User Stories

1. As a target author, I want to declare a default accessor in `target.txt`, so that I don't have to repeat `ACCESSOR JsonAccessor` on every packet definition.
2. As a target author, I want the default accessor to apply to both command and telemetry packets, so that the entire target's protocol is described in one place.
3. As a target author, I want to pass constructor arguments to the default accessor (e.g., `DEFAULT_ACCESSOR TemplateAccessor some_param`), so that parameterised accessors work the same way they do per-packet.
4. As a target author, I want a per-packet `ACCESSOR` keyword to override the target default, so that I can have one-off exceptions without removing the default.
5. As a Ruby target author, I want `DEFAULT_ACCESSOR` to load the accessor class using the same `require_class` path as the per-packet `ACCESSOR` keyword, so that custom accessor classes work.
6. As a Python target author, I want `DEFAULT_ACCESSOR` to work for Python-language targets using the same module-loading path as the per-packet `ACCESSOR` keyword.
7. As a target author, I want the default accessor to be visible in `target.as_json()`, so that the configuration is fully inspectable via the API.
8. As an operator, I want existing targets without `DEFAULT_ACCESSOR` to behave identically to today, so that this is a non-breaking addition.
9. As a target author, I want a clear error if the class name given to `DEFAULT_ACCESSOR` cannot be loaded, so that misconfiguration is caught at startup.
10. As a target author using `SELECT_COMMAND` / `SELECT_TELEMETRY` to patch existing packets, I want the default accessor to not retroactively re-apply to already-selected packets, so that edit operations are unambiguous.

## Implementation Decisions

- **New keyword**: `DEFAULT_ACCESSOR <ClassName> [arg...]` in `target.txt`. Follows the same variadic-arg convention as the per-packet `ACCESSOR` keyword.

- **Storage on Target**: `Target` stores two new fields — `default_accessor` (String class name or nil) and `default_accessor_args` (Array, default `[]`). The class is not loaded at `target.txt` parse time; only the name and args are stored. This keeps `Target` free of accessor-loading concerns.

- **Threading into PacketConfig**: `PacketConfig#process_file` gains two optional trailing parameters — `default_accessor_class` and `default_accessor_args` — mirroring how `language` is already threaded through. The call site in `System` passes `target.default_accessor` and `target.default_accessor_args` alongside `target.language`.

- **Application point**: Immediately after a `COMMAND` or `TELEMETRY` packet is constructed (before any subsequent per-packet keywords are processed), `PacketConfig` applies the default accessor if one is set. The per-packet `ACCESSOR` keyword — processed later in the same block — overwrites it naturally, requiring no special override logic.

- **Language handling**: The same Ruby/Python branching used by the per-packet `ACCESSOR` applies — `require_class` for Ruby targets, `get_class_from_module` for Python targets.

- **`as_json` / `as_json()`**: Both Ruby `Target#as_json` and Python `Target.as_json()` include `default_accessor` and `default_accessor_args` in their output so the configuration round-trips correctly.

- **`PacketConfig.from_config`**: The existing `from_config` class method calls `process_file` without a `Target` object; it passes `nil`/`None` for both new params (the defaults), which is correct — accessors are already embedded per-packet in serialised config.

- **No new seams**: `SELECT_COMMAND` / `SELECT_TELEMETRY` select an already-created packet; the default is only applied at creation time, so selected packets are unaffected.

## Testing Decisions

**What makes a good test here**: test observable packet state (`packet.accessor.class`, `packet.accessor.args`) after parsing a complete target config string. Do not test internal instance variables like `@default_accessor_class`. Use temp-file config strings (the pattern already used throughout `packet_config_spec.rb` and `target_spec.rb`).

**Seams to test:**

- **`Target#process_file` (Ruby) / `Target.process_file` (Python)**: verify that `DEFAULT_ACCESSOR ClassName` stores the class name and args, and that an unknown keyword still raises. Prior art: `spec/system/target_spec.rb` — `describe "process_file"`.

- **`PacketConfig#process_file` (Ruby) / `PacketConfig.process_file` (Python)**: verify that packets created after calling `process_file` with a non-nil `default_accessor_class` have the expected accessor set; verify that a per-packet `ACCESSOR` overrides it; verify that `SELECT_COMMAND`/`SELECT_TELEMETRY` packets are unaffected. Prior art: `spec/packets/packet_config_spec.rb` — `context "with ACCESSOR"` at line 612.

- **`Target#as_json`**: verify `default_accessor` and `default_accessor_args` keys appear in the returned hash.

Both Ruby (`rspec`) and Python (`pytest`) test suites should have parallel coverage for their respective implementations.

## Out of Scope

- Per-direction defaults (`DEFAULT_CMD_ACCESSOR` / `DEFAULT_TLM_ACCESSOR`) — a single `DEFAULT_ACCESSOR` covers both directions; overrides handle exceptions.
- Validating or loading the accessor class at `target.txt` parse time — errors surface when packets are first constructed, which is early enough.
- Modifying XTCE parsing to honour `DEFAULT_ACCESSOR` — XTCE files have their own schema and are out of scope.
- UI exposure of `DEFAULT_ACCESSOR` in the plugin editor or target configuration screens.

## Further Notes

- The motivation is the HTTP/JSON demo target (`openc3-cosmos-httpjsondemo`) and similar targets that speak a single non-binary protocol uniformly across all packets.
- The feature is purely additive — no existing keyword or behaviour changes. Targets without `DEFAULT_ACCESSOR` in `target.txt` are unaffected.
- The `from_config` path (used for serialisation round-trips) correctly ignores the new params because the accessor is already recorded per-packet in the serialised form.
