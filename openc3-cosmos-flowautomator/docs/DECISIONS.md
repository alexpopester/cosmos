# Design decisions

The choices that shaped the Flow Automator, why they were made, and what was
turned down. Read this before proposing to change one of them: most were
explicit choices by the project owner, not accidents. New decisions go at
the end with the next number.

Format: **Decision**, then *why*, then *rejected / consequences*.

## D1. A standalone plugin, no COSMOS core changes

The whole feature is one gem loaded through Admin → Plugins. Nothing in
`openc3/`, `openc3-cosmos-init/` or the other core services was changed.

*Why:* custom work stays decoupled from upstream, survives COSMOS upgrades and
rebases, and can be distributed on its own.
*Consequences:* where core has a bug we work around it in the plugin (see D9)
instead of fixing core. If something really needs core, ask the owner first.

## D2. The engine is a Python plugin microservice with its own HTTP API

Flows run server side in a `MICROSERVICE` declared in `plugin.txt`, reached
through `ROUTE_PREFIX /flow-automator` (Traefik routes it; the prefix is not
stripped). Same pattern as the CFDP plugin.

*Why:* triggers must stay armed 24/7 and runs must not depend on a browser.
Python because COSMOS ships a Python API with the same functions the API
server uses, and sgp4/numpy are available.
*Rejected:* running flows in the browser (dies with the tab); generating
Script Runner scripts (no live graph view, no server-side triggers); COSMOS
Autonomic (Enterprise only; target was Core 7.4.1).

## D3. Flows are stored as JSON in Valkey, not in the bucket

Flows, versions, runs, audit and planner records are JSON under
`flow_automator__<scope>__*`.

*Why:* simple, transactional enough, no new storage. Export/Import gives
files when people want them.
*Consequences:* history lists are capped (runs per flow, versions, audit);
the audit log is also written to the COSMOS log so it outlives the cap.

## D4. Wires are control flow, variables carry data

A wire means "then run this". Each output takes one wire, except Parallel.
Nodes that can fail have an `error` output. A run has one shared variable
context.

*Why:* operators read it like a flowchart or procedure; it maps onto how
COSMOS scripts are written. Dataflow graphs make sequencing and waits hard to
read.
*Consequences:* loops are drawn by wiring the body's end back into the Loop
node; branching needs Parallel/Join; isolated Parallel exists for branches
that must not share variables.

## D5. Conditions are a safe subset of Python, plus COSMOS check syntax

`expressions.py` evaluates an AST whitelist. `INST HEALTH_STATUS TEMP1 > 50`
works as-is.

*Why:* operators already know `wait_check` syntax and Python; arbitrary code
in a server-side, always-on engine that sends commands is unacceptable.
*Consequences:* no attribute access, so functions replace methods
(`upper(x)`, `get(d, k)`), and contact data is flat variables
(`contact_aos`), not `contact.aos`.

## D6. Safety model: disarmed by default, Stop all, modes, hazardous gating

- New and imported flows start disarmed; saving keeps the armed state but a
  save that doesn't validate disarms. Only armed flows fire triggers; Run now
  works disarmed.
- Stop all disarms everything, drops queues, stops runs and pauses the
  contact scheduler.
- Execution modes crawl/walk/run per flow, overridable per run; sub-flows
  run at the stricter of theirs and the caller's.
- Hazardous commands: in run mode they fail unless the node has *Allow
  hazardous*; in crawl/walk the operator's approval is the acknowledgement.
- Re-trigger policy defaults to *skip*.

*Why:* agreed with the owner up front as v1 scope.
*Deliberate choice:* "Go & run rest" still asks for hazardous commands. The
owner was told; making it skip everything is a one-line change in
`runner._gate` / `FlowRun.approve` if they ever want it.

## D7. Values substituted into commands can't change the command

`render_command` rejects `{{ }}` values that contain commas, quotes or
`with` outside quotes (or the enclosing quote inside quotes), unless the
node's *Allow raw values* is on.

*Why:* template parameters and telemetry flow into commands; a value like
`NORMAL, DURATION 99` must fail the step, not add a parameter.

## D8. Commands go through `openc3.api` directly; scripts through Script Runner

*Why:* `cmd()` from inside the operator is the same code path the API server
runs, so COSMOS checks, logs and attributes commands normally. Script Runner
is the only supported way to run a procedure with its UI and logs.
Script results come back through the COSMOS stash (`FLOW_RESULT_KEY`)
because Script Runner has no return value channel.

## D9. The plugin does its own hazardous check

`CosmosBackend.cmd_hazardous` re-implements `get_cmd_hazardous`.

*Why:* in COSMOS 7.4.1 the Python `get_cmd_hazardous` raises `KeyError` when
a command omits a parameter that has states (an `and` that should be `or`).
Fixing core is off limits (D1).

## D10. Contact planning lives in the same engine

Pass prediction (SGP4 via the `sgp4` package, own TEME → topocentric math),
satellites, stations, activities and rules are part of FLOW_ENGINE, shown in
a Contacts tab of the same tool.

*Why:* scheduled activities are runs; one process means one Stop all, one
audit log, one set of alerts, and no cross-service protocol.
*Own frame math instead of skyfield:* no ephemeris/data files, works air
gapped; agrees with skyfield to < 0.2 s on pass times.

## D11. An activity is a snapshot of a template + parameters + contact + anchor

Templates are ordinary flows with `template: true` and declared parameters.
Scheduling copies the flow; the activity says *template changed* and offers
**Update** if the template is edited later.

*Why:* what was reviewed and approved is what runs; edits to a template
can't silently change tonight's passes.
*Timing:* start = `contact[aos|tca|los] + offset`, so a flow can start before
telemetry flows. Approvals close at LOS minus a margin.

## D12. TLE changes re-time activities, within a tolerance

An activity follows the nearest predicted pass of the same satellite and
station if it moved less than `retime_tolerance` (120 s); otherwise it needs
review and won't run. Rule activities are cancelled and re-planned.

*Why:* routine TLE updates shouldn't need operator work; a big move
(maneuver, wrong TLE) must.

## D13. Automated TLE inputs are guarded; pasting is trusted

TLEs from HTTP, stash, bucket files or telemetry must match the catalog
number, be newer, respect a preferred source, and not jump more than 100 km
(else held for Accept/Reject). Pasting in the satellite dialog applies as is.

*Why:* an automated feed can be stale, wrong or a different object; an
operator pasting is making a decision.

## D14. Station capacity conflicts warn, they don't block

*Why:* operators know things the planner doesn't (antennas down, priorities).
Rules can opt to skip full stations.

## D15. Alerts only for things someone must act on

COSMOS *alerts* re-toast on every page load until acknowledged, so only
unattended failures, missed/stuck activities, failed prediction, Notify nodes
set to alert, and approvals unanswered for 30 s (configurable) are alerts.
Everything else is a bell notification. Dry runs never ring the bell.
Background polling sends `Ignore-Errors` so an engine restart shows "Engine
offline" instead of a toast per poll.

## D16. Writes need `script_run`; identity comes from the token

*Why:* flows send commands and run scripts, so changing them is equivalent
to running scripts. The engine checks the token by making a harmless COSMOS
call with it rather than validating tokens itself.
*Consequences:* Core has one user, so the audit log shows `anonymous` there.

## D17. Every save is a version; restore makes a new version

Saving unchanged content doesn't create a version. Saving over a version
someone else saved after you opened it is a 409 conflict unless forced.
Deleted flows keep history and runs and can be restored.

## D18. Build works without Node in the COSMOS container

`openc3.sh cli rake build` runs in the cmd-tlm-api container, which has no
pnpm. The Rakefile packages the already-built tool if its source hash
matches (`tools/flowautomator/.source-hash`); `build_tool.sh` builds the tool
locally or in the `openc3-node` container.

## D19. d3 packages pinned in `pnpm-workspace.yaml`

New releases of the d3 packages Vue Flow depends on appeared on 2026-11-03,
and pnpm 11's `minimumReleaseAge` refuses packages that new, which broke
lockfile installs. The pins can be dropped once those releases are old
enough (or kept; nothing needs the new versions).
