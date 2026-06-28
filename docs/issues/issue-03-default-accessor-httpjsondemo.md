# Issue 03: Demo target: adopt `DEFAULT_ACCESSOR`

**Type**: AFK
**Blocked by**: Issue 01 or Issue 02 (whichever language the demo target uses)

## What to build

Update the `openc3-cosmos-httpjsondemo` target to use `DEFAULT_ACCESSOR` instead of repeating `ACCESSOR` on every packet definition. This is the motivating use case for the feature and serves as a real-world integration proof.

- Add `DEFAULT_ACCESSOR JsonAccessor` (or the appropriate accessor) to the demo target's `target.txt`
- Remove the now-redundant per-packet `ACCESSOR` lines from all cmd_tlm definition files in the target
- Verify the demo target continues to function correctly end-to-end

## Acceptance criteria

- [ ] `target.txt` for `openc3-cosmos-httpjsondemo` contains `DEFAULT_ACCESSOR <AccessorClassName>`
- [ ] No cmd_tlm definition file in the demo target contains a redundant `ACCESSOR` line that matches the default
- [ ] All command and telemetry packets in the demo target have the correct accessor applied at runtime
- [ ] The demo target's procedure (`procedure.py` or equivalent) runs without error against the updated definitions
- [ ] No other targets in the repo are modified

## Blocked by

- Issue 01 (Ruby) or Issue 02 (Python) — whichever language the httpjsondemo target is implemented in
