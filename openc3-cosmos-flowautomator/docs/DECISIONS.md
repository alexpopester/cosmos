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
- Execution modes crawl/walk/run per flow. A real run can override the
  mode only towards *more* caution; a dry run can use any mode. Sub-flows run
  at the stricter of theirs and the caller's. (Amended 2026-11-04: it used to
  be overridable either way, which let any `script_run` user run a crawl
  flow with no approvals; see the evaluation, SEC-8.)
- An armed flow whose save relaxes its safety settings is disarmed: a less
  cautious mode, or *Allow hazardous* / *Allow raw values* turned on (added
  2026-11-04).
- Approval covers the exact command shown: the executor refuses to send a
  re-rendered command that differs (added 2026-11-04, ENG-1).
- Hazardous commands: in run mode they fail unless the node has *Allow
  hazardous*; in crawl/walk the operator's approval is the acknowledgement.
- Re-trigger policy defaults to *skip*.

*Amended 2026-11-05 (1.12.0, owner's answers Q1–Q4 in EVALUATION-2026-11-05):*

- **The mode floor applies to every real run.** That includes scheduled
  activities, rules and sub-flows, not only Run now and triggers. It is
  enforced in `FlowRun` itself, and the planner refuses an activity or rule
  in a less cautious mode than its flow.
- **The approved command is sent verbatim.** It is never rendered again, so
  a command using `now()` or changing telemetry works. It used to be
  re-rendered and refused if it differed. If the command can't be rendered
  to show, the step fails rather than ask the operator to approve something
  they can't see.
- **In crawl and walk, only an operator's approval acknowledges a hazardous
  command.** The node's *Allow hazardous* flag counts in run mode only. If
  the hazard lookup fails, the command is treated as hazardous.
- **COSMOS has the last word on hazards.** If `cmd()` refuses a command
  that the plugin's check passed (a hazardous default, a state name in
  another case), crawl and walk ask then, showing the exact command. "Go &
  run rest" doesn't cover it.
- **More saves disarm an armed flow:** turning dry run off; adding a node that
  acts (Send command, Run script, Set telemetry, Sub-flow); changing the
  command, script, item or sub-flow of a node that has *Allow hazardous* or
  *Allow raw values*; changing which flow a Sub-flow node calls. Relaxing a
  sub-flow disarms every armed flow that calls it, at any depth.
- **`subflow_snapshots` comes only from the planner.** It is stripped from
  saved, imported and restored flows. A run ignores one inside a flow's
  JSON, so a flow can't carry hidden sub-flow content.

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
Scheduling copies the flow *and every sub-flow it calls* (directly or not,
`subflow_snapshots`, added 2026-11-04); runs of the activity, rehearsals
included, use those copies. The activity says *template changed* (or *sub-flow
changed*) and offers **Update** if any of them is edited later. Manual and
triggered runs still call the current sub-flows.

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

## D20. Every version has a CHANGELOG.md entry, enforced by the build

`CHANGELOG.md` (Keep a Changelog format) records what changed in each gem
version. Changes collect under `## [Unreleased]`, and that heading is renamed
to the version when it is built. `rake build VERSION=X.Y.Z` stops if
`CHANGELOG.md` has no `## [X.Y.Z]` heading.

*Why:* the owner asked for a changelog updated with every version increment.
Forty versions went out in two days with no record of what changed between
them.
*Consequences:* quick development builds need a one-line entry too; several
of them can be folded into one entry before release. Versions 1.0.0–1.7.8
are summarised by commit, because they predate the changelog.

## D21. Webhooks may reach private networks, but not link-local addresses

Webhook URLs may point at private-network and localhost receivers. Link-local
addresses (169.254.0.0/16, fe80::/10, where cloud metadata services live),
multicast and unspecified addresses are refused, both when saving and at
send time (after DNS resolution). Redirects aren't followed, and a failed
delivery reports only the HTTP status, never the response body.

*Why:* on-premises and air-gapped ops networks run their chat and paging
services on private addresses, so blocking private ranges would break the
main use. What made webhooks a server-side request forgery risk (reading a
response back, metadata services, redirects) is closed. Writes already need
`script_run`, which can run any script anyway.
*Consequences:* an admin-set allow-list of domains would be the next step if
a deployment needs it.

## D22. Expressions have resource limits

`expressions.py` caps:

- source length (10,000 characters) and syntax tree size (2,000 nodes);
- result size: 100,000 characters or elements, counting nested ones;
- integer size: 64,000 bits;
- `format` widths (200) and `round` digits (100).

Operations that could build a huge value check sizes *before* computing.
`sum` takes numbers only, and `%` on text is refused. Only short
expressions are kept in the parse cache. `/api/validate` needs write
permission.

*Why:* evaluation runs on engine threads, and one big expression held the
GIL for tens of seconds and could run the operator container out of memory.
That froze every trigger and STOP ALL with it, and took down every
interface (SEC-1, SEC-4, ENG-5).
*Consequences:* new safe functions must be bounded in the same way. Check
the size before building the result, not after.

## D23. Rule priority pre-empts; hand-edited rule activities are detached

Saving a rule that changes which passes it takes re-plans that rule and every
lower-priority rule, highest priority first. Renaming a rule keeps its
activities. An activity a rule made, then edited by hand, is marked
`detached`: a re-plan leaves it alone, and deleting the rule keeps it as a
manual activity. Cancelled rule activities (tombstones) are matched to passes
by satellite, station and TCA within `retime_tolerance`, not by contact id.

*Why:* priority that didn't pre-empt meant a higher-priority rule added later
scheduled nothing. Re-planning on every save threw away operators' edits. A
routine TLE update changed contact ids and brought cancelled passes back
(CON-1, CON-6, CON-17).

## D24. A satellite that never sets gets one contact per UTC day

If a pass covers the whole prediction window, it's split into UTC days
(`continuous: true`, AOS 00:00, LOS 24:00). Days cut by the window's edges
stay partial. Partial contacts can't be scheduled.

*Why:* a single window-long partial contact could never be scheduled, so GEO
satellites couldn't be used at all (CON-5). Daily windows let activities
anchor to a time of day with the existing AOS + offset model.
*Rejected for now:* wall-clock or cron activities with no contact. That's a
bigger feature (evaluation, scheduling gap 3).

## D25. Interrupted runs are recorded, not resumed

Every active run has a small record in `active_runs`, which is deleted when
it ends. At startup, leftovers are recorded as failed runs (`interrupted:
true`), their Script Runner scripts are stopped, and unattended ones raise
an alert. Runs are not resumed.

*Why:* after a crash, runs used to vanish without history or alert, and
their scripts kept running unsupervised (ENG-8). Resuming safely needs
durable step state and an operator decision, which is a larger feature
(evaluation GAP-2).

## D26. Stored records carry a format version; newer ones are left alone

Everything written to Valkey is stamped `"format": N` (`migrations.py`).
Reading upgrades older records step by step. A record in a format newer
than the engine knows (after a downgrade) is skipped, never rewritten, and
the engine raises an alert.

*Why:* nothing recorded which version wrote a flow, so the first rename of a
stored field would have broken saved flows silently. Skipping, rather than
guessing, means a downgrade can't damage data that the newer version can
still read.

## D27. Restoring a backup never starts anything

Restoring leaves every flow it touches disarmed (with the reason shown) and
pauses the contact scheduler. Restoring merges rather than replaces: items
are matched by id, then by name, and nothing absent from the backup is
deleted. Everything restored goes through the API's own validation.

Webhook URLs and one-off activities aren't restored:
- **Webhook URLs:** backups can be made by read-only users, so they never
  contain the URLs.
- **One-off activities:** their passes are predicted again.

*Why:* a backup may come from another system, or from long ago. Its armed
triggers and rules must not command a spacecraft before someone has looked
at them (the same principle as D6).

## D28. A Valkey outage buffers history, but fails operator saves

Some writes are kept in memory, in order, and retried every 2 s: run
history, audit entries, versions, and the contact planner's own records. Up
to 5,000 can wait. Saving or arming a flow, and other API writes, fail with
an error instead.

*Why:* runs and the scheduler keep going during a blip, and their as-run
record matters more than anything else that was lost before. The planner
updates its memory before writing, so retrying keeps memory and Valkey in
step. An operator's save that only lived in memory would look stored, and
then vanish on restart.

*Amended 2026-11-05 (1.12.0):*

- **Writes that only reduce activity never fail.** STOP ALL, disarming and
  pausing the scheduler change memory at once and use the buffer. Arming and
  deleting save first and change memory only if that worked.
- **The engine never waits on Valkey while holding its lock.** Run
  bookkeeping (active-run records, history) is queued. The engine's store
  thread writes the queue, not the caller.
- **A delete drops buffered writes to the same record**, so a retry can't
  bring it back.
- **The engine has its own Valkey client**, with a 5 s socket timeout and one
  quick retry. COSMOS's shared client has no timeout and retries for about
  9 s, so a paused or partitioned Valkey used to hang the engine, STOP ALL
  included.

## D29. Internet TLEs pass the safeguards; credentials stay in COSMOS secrets

CelesTrak and Space-Track TLEs go through `TleIntake.ingest`, like any
automated TLE: the catalog must match, the epoch must be newer, a preferred
source wins, and a big jump is held for review.

- **Credentials:** the Space-Track login is read from COSMOS secrets each
  time it's needed. The plugin stores only the secret names.
- **Schedule:** each catalog number is fetched at most every `hours`, never
  below the service's minimum (CelesTrak 2 h, Space-Track 1 h), even on
  Fetch now.
- **Failures:** a failure of the whole source backs off from 10 minutes,
  doubling.
- **Requests:** no redirects are followed.

*Why:*
- **Safeguards:** a public catalog can carry a wrong or post-maneuver
  element set like any feed, and re-timing every activity on one deserves
  the same review.
- **Credentials:** keeping them out of Valkey keeps them out of backups and
  API responses (as with webhook URLs, D27). It also lets Enterprise
  installs use their own secret store.
- **Rate limits:** both services ban clients that poll too hard, and a
  ban would stop every satellite's updates.
