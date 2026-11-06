# Flow Automator architecture

How the plugin is put together, for anyone changing it. What it does for
operators is in [../README.md](../README.md); why it is built this way is in
[DECISIONS.md](DECISIONS.md); how to build, test and install it is in
[DEVELOPMENT.md](DEVELOPMENT.md).

## The big picture

```
 Browser (any COSMOS tool page)
 ┌──────────────────────────────────────────────┐
 │ Flow Automator tool (Vue 3 + Vuetify + Vue    │
 │ Flow, single-spa app at /tools/flowautomator) │
 │  flowautomator.vue  ── Flows editor           │
 │  ContactsView.vue   ── Contacts / scheduling  │
 └──────────────┬───────────────────────────────┘
                │ HTTP + COSMOS token (flowApi.js), polled every 0.5–1 s
                ▼
 Traefik  /flow-automator/*  (plugin ROUTE_PREFIX, prefix NOT stripped)
                │
                ▼
 openc3-operator container
 ┌──────────────────────────────────────────────────────────────────────┐
 │ FLOW_ENGINE microservice (microservices/FLOW_ENGINE/flow_engine.py)   │
 │                                                                      │
 │  FlowHttpServer ─► FlowApi (http_api.py) ─► TokenAuthorizer           │
 │                        │                     (calls COSMOS JSON-RPC)  │
 │                        ▼                                              │
 │  FlowEngine (engine.py) ── owns flows, runs, queues, Stop all         │
 │   ├─ TriggerManager (triggers.py)   thread "flow-triggers"            │
 │   ├─ FlowRun × N (runner.py)        thread per run (+ per branch)     │
 │   ├─ ContactPlanner (contacts.py)   threads "flow-contacts",          │
 │   │    ├─ orbit.py (SGP4)                   "flow-predict"            │
 │   │    ├─ templates.py, tle_inputs.py, tle_sources.py                 │
 │   ├─ Alerter (alerts.py)            webhook delivery thread           │
 │   ├─ AuditLog (audit.py), FlowVersions (versions.py)                  │
 │   └─ FlowStore (store.py) ─────────────────────► Valkey (Redis)       │
 │                                                                      │
 │  CosmosBackend (cosmos_backend.py) ─► openc3.api (tlm, cmd, stash,    │
 │                                       target files) and Script Runner │
 └──────────────────────────────────────────────────────────────────────┘
```

Everything that matters happens in the engine. The tool is a view and an
editor: closing it changes nothing, and two people can have it open at once.

## Source map

| Path | Role |
| --- | --- |
| `plugin.txt` | Declares the `FLOW_ENGINE` microservice (port, `ROUTE_PREFIX`, options) and the `TOOL`. Plugin variables are documented at the top |
| `microservices/FLOW_ENGINE/flow_engine.py` | COSMOS `Microservice` subclass: reads options, wires store + backend + engine + HTTP server, reports state every 5 s |
| `lib/flow_automator/engine.py` | `FlowEngine`: flow CRUD + versions, arm/disarm, `trigger()` with re-trigger policy, scheduled and sub-flow runs, approvals, Stop all, run lists |
| `lib/flow_automator/runner.py` | `FlowRun`: executes one run. Graph walk, parallel/join, approval gate, step log, and one `exec_<type>` function per node type (`EXECUTORS`) |
| `lib/flow_automator/graph.py` | `NODE_TYPES` registry (outputs, error port, which fields are expressions/templates, required fields), `validate_flow`, `Graph` index |
| `lib/flow_automator/expressions.py` | Safe AST evaluator for conditions and `{{ }}` templates; COSMOS `TGT PKT ITEM` triplet rewriting; `telemetry_refs` for batching |
| `lib/flow_automator/triggers.py` | `TriggerManager`: one polling thread for every armed trigger node, batched telemetry reads, edge detection |
| `lib/flow_automator/cron.py` | 5-field cron matcher (UTC) |
| `lib/flow_automator/store.py` | `FlowStore` (all Redis access) and `MemoryRedis` (the in-memory stand-in tests use) |
| `lib/flow_automator/versions.py` | Version snapshots and field-by-field diffs |
| `lib/flow_automator/audit.py` | Append-only audit log (Redis list + `[audit]` COSMOS log lines) |
| `lib/flow_automator/alerts.py` | Events → COSMOS bell (notification/alert) and webhooks (Slack, Teams, generic), dedup and retries |
| `lib/flow_automator/contacts.py` | `ContactPlanner`: satellites, stations, predicted contacts, activities, rules, scheduler tick, re-timing, capacity |
| `lib/flow_automator/orbit.py` | TLE parsing/checksums, SGP4 propagation, TEME → ECEF → topocentric, pass finding |
| `lib/flow_automator/templates.py` | Template parameter types, validation, `fill_parameters`, `CONTACT_VARIABLES` |
| `lib/flow_automator/tle_inputs.py` | `TleIntake`: TLEs from the HTTP bulk endpoint, a stash key, bucket files and telemetry, with the safeguards |
| `lib/flow_automator/tle_sources.py` | Per-satellite TLE sources: `manual`, `celestrak`, `spacetrack`. `TleFetcher` runs on the `flow-tle-inputs` thread and sends what it fetches through `TleIntake.ingest` |
| `lib/flow_automator/demo.py` | `load_demo` / `clear_demo` (File → Load Demo) |
| `lib/flow_automator/cosmos_backend.py` | The only module that imports `openc3`. `CosmosBackend` and `TokenAuthorizer` |
| `lib/flow_automator/http_api.py` | Route table (`_routes()`), handlers, the full endpoint list in the module docstring, the stdlib HTTP server |
| `src/tools/flowautomator/` | The Vue tool (see [Frontend](#frontend)) |
| `test/` | pytest suite against `FakeBackend` + `MemoryRedis` (`conftest.py`) |
| `test/ui/` | Playwright scripts against a live COSMOS (`run.sh`) |
| `demo/setup_demo.sh` | One-shot local demo setup |

## Processes and threads

The engine is one Python process (one microservice per scope the plugin is
installed in). Inside it:

| Thread | Owner | Does |
| --- | --- | --- |
| main | `FlowEngineMicroservice.run` | Starts everything, updates the microservice state line every 5 s |
| `flow-http` + one per request | `FlowHttpServer` (`ThreadingHTTPServer`) | Serves the API |
| `flow-triggers` | `TriggerManager._loop` | Every `trigger_period` (0.25 s) evaluates all armed triggers |
| `flow-run-<id>` | `FlowRun.start_thread` | One per top-level run |
| `flow-run-<id>-b<n>` | `FlowRun._run_parallel` | One per Parallel branch, joined before continuing |
| `flow-contacts` | `ContactPlanner._loop` | Scheduler tick every 1 s: start/miss/stop activities, rules every 60 s, prune and stale-TLE check every 300 s |
| `flow-tle-inputs` | `ContactPlanner._poll_loop` | TLE inputs and sources every 1 s (stash, bucket files, packets): kept off the scheduler tick so slow reads can't delay activity starts |
| `flow-predict` | `ContactPlanner._predict_loop` | Predicts queued satellites one at a time |
| alert worker | `Alerter` | Delivers webhooks with retries |
| `flow-store` | `FlowEngine._store_loop` | Writes queued writes as soon as they're queued, and retries refused ones every 2 s (`FlowStore.flush`); alerts when storage fails and recovers; forgets finished fire-and-forget scripts every 60 s |
| `threading.Timer`s | `FlowEngine.on_approval_requested` | Escalate an unanswered approval to an alert after the delay |

Sub-flows do **not** get their own thread: `run_subflow` calls
`child.execute()` synchronously from the parent's step.

### Locks

- `FlowEngine.lock` (RLock) guards `flows`, `runs`, `queues`.
- `FlowRun._lock` guards a run's `steps`, `active`, `approvals`.
- `ContactPlanner.lock` guards satellites, stations, activities, rules, contacts.
- **Lock order is planner → engine.** The planner calls the engine while
  holding its lock (e.g. `_launch` → `start_scheduled_run`); the engine must
  never call into the planner while holding `engine.lock`. That is why
  `FlowEngine.status()` reads `planner.status()` before taking its own lock,
  and `on_run_finished` releases `engine.lock` before calling
  `planner.on_run_finished`. Keep it that way.

## Data model

All persistent state is JSON in Valkey under `flow_automator__<scope>__*`
(see `store.py`). Runs in progress, contacts and trigger state live only in
memory and are rebuilt (or lost) on restart.

| Key | Type | Contents |
| --- | --- | --- |
| `…__flows` | hash id → flow | Current version of every flow |
| `…__versions__<flow id>` | list, newest first | Version metadata (limit 200); flow bodies in `…__version_bodies__<flow id>` (hash: version -> flow) (D32) |
| `…__runs__<flow id>` | list, newest first | Finished runs' summaries (limit `run_history`); steps in `…__run_steps__<flow id>` (hash: run id -> steps), and `…__run_index` (hash: run id -> flow id) (D32) |
| `…__flow_memory` | hash | What each flow remembers between runs (D31) |
| `…__audit` | list, newest first | Audit entries (limit 10,000) |
| `…__satellites`, `…__stations`, `…__activities`, `…__rules` | hash id → record | Contact planner records |
| `…__deleted_flows` | hash id → summary | Deleted flows that can be restored |
| `…__active_runs` | hash run id → small record | Runs in progress (flow, trigger, scripts started). Deleted when a run ends; leftovers at startup are runs a crash interrupted: recorded as failed, their scripts stopped |
| `…__planner_settings` | hash | `settings` (paused, reason), `tle_inputs` |
| `…__alert_settings` | hash | Alert destinations and event choices |
| `…__meta` | hash | `format`: the newest storage format written here |

**Formats** (`migrations.py`, DECISIONS D26):
- Every record is written with `"format": N`. `FlowStore` upgrades it to the
  current format on read and removes the stamp, so nothing else sees it.
- A record in a newer format is skipped, listed in `store.unreadable`, and
  never rewritten.
- Backups (`backup.py`) carry the same stamp.

**Outages** (D28):
- These writes go through `FlowStore`'s pending queue, so a refused write is
  retried instead of lost:
  - appends: run history, audit, versions;
  - `save_record(..., defer=True)`: everything `ContactPlanner._save` writes.
- Flow saves and other API writes raise.
- `status()["store"]` and `/health` report the queue.

### Flow

The same JSON is stored, sent to the tool, exported to a file, and
snapshotted into activities. Vue Flow's node/edge shape is used directly.

```jsonc
{
  "id": "a1b2c3d4e5f6", "name": "Health check", "description": "",
  "armed": false,                       // not content: doesn't make a version
  "mode": "crawl" | "walk" | "run",
  "policy": "skip" | "queue" | "parallel",
  "approval_timeout": 0,                // seconds, 0 = wait forever
  "dry_run": false,                     // every run of it is a dry run
  "max_steps": 100000,
  "variables": {"count": 0},            // starting variables
  "template": false, "parameters": [ {"name", "type", "default", ...} ],
  "nodes": [{"id": "n1", "type": "if", "position": {"x": 0, "y": 0},
             "data": {"label": "...", "condition": "...", "require_approval": false}}],
  "edges": [{"id": "e1", "source": "n1", "sourceHandle": "true", "target": "n2"}],
  "version": 7, "saved_by": "anonymous", "saved_note": "", "created_at": 0, "updated_at": 0
}
```

Edges are control flow ("then do this"), not data flow. Data moves through
run variables. `graph.py`'s docstring is the authoritative description.

### Run

`FlowRun.as_json()` is what the API returns and what is stored in history:
status (`pending/running/success/failed/stopped`), `trigger` (what started it:
kind, user, node, activity/rule/contact, parent run), `flow_version`, `mode`,
`dry_run`, `start_variables`, `variables`, pending `approvals`, `children`
(sub-flow run ids), and `steps` (last 1,000: node, status, output handle,
message, timings, who decided an approval).

### Contact, activity, rule

- **Contact**: computed, never stored. `id = <sat id>-<station id>-<round(tca)>`,
  plus aos/tca/los (Unix s), duration, max elevation, satellite/station names.
- **Activity**: a *snapshot* of the template flow (`flow`), `params`, a copy
  of its contact (`contact`), `anchor` + `offset`, `mode`,
  `late_tolerance`, `approval_margin`, `stop_at_los`, `duration`, `status`
  (`scheduled`, `needs_review`, `running`, `done`, `failed`, `stopped`,
  `missed`, `cancelled`), `run_id`, `rule_id`.
- **Rule**: template id, satellite/station filters, min elevation/duration,
  priority, `skip_full_stations`, and the same timing/mode/params fields as
  an activity.

## How a run works

1. **Start.** Something calls `FlowEngine.trigger()` (Run now, a trigger
   firing), `start_scheduled_run()` (an activity or a rehearsal) or
   `run_subflow()` (a Sub-flow node). `trigger()` applies the arm check,
   validation (manual only), template parameters (`fill_parameters`), and the
   re-trigger policy: skip, queue (max 10 per flow), or parallel. At most 50
   runs are active.
2. **Snapshot.** `FlowRun` deep-copies the flow, so editing a flow never
   affects runs in progress.
3. **Walk.** `_walk` starts at the trigger node and loops: `_execute_node`
   returns an output handle name, `_next` follows the one wire on it. No
   wire means that path ends. Step count is capped (`max_steps`) so a loop
   wired forever fails rather than spins.
4. **Execute a node.** `_execute_node` records a step, calls `_gate`
   (approval), then `EXECUTORS[type](run, node, data, step)`. Exceptions:
   - `RunStoppedError` (Stop, Stop all, abort, shutdown) and `RunEndedError`
     (End node) unwind the whole run.
   - `RunFailedError` fails the run.
   - Any other exception is a node error: if the node type has an error
     port (`"errors": True` in `NODE_TYPES`) and it is wired, `error` is set
     and the run follows it; otherwise the run fails.
5. **Parallel.** `_run_parallel` starts a thread per outgoing wire. Each
   branch walks until it reaches a Join (returns it) or ends. All branches
   must reach the same Join. The first real failure cancels the others. With
   *isolate*, each branch gets a shallow copy of the variables (via a
   thread-local) and changes are merged back in wire order into
   `branch_results`.
6. **Finish.** `on_run_finished` removes the run, saves it to history,
   emits `run_failed`/`run_stopped` (alert only if unattended), tells the
   planner if it belongs to an activity, and starts the next queued trigger.

Waiting is always done with `run.sleep()` or `Event.wait(0.1)` loops that
check `run.cancelled()`, so Stop is honoured within ~100 ms. A cancelled
parent cancels its children (`cancelled()` checks the parent chain).

### Approvals (crawl / walk)

`_gate` decides if a node needs an operator: crawl → every node except
triggers and Ask; walk → nodes with `require_approval`; any mode but run →
hazardous Send command (checked by `CosmosBackend.cmd_hazardous` on the
rendered command). Then `_wait_for_approval` registers a pending approval
on the run, notifies the engine (which arms an escalation timer), and waits.
Decisions: `go`, `run` (go and auto-approve routine steps for the rest of
this run; hazardous still ask), `abort`. An approval of a hazardous command
is the hazardous acknowledgement (`approved_hazardous` → sent without the
COSMOS hazardous check). Deadlines: the flow's `approval_timeout`, and for
activities the run's `approval_deadline` (LOS − margin), inherited by
sub-flows. Ask operator nodes reuse the same mechanism with an `ask` spec and
a value.

A sub-flow runs at the stricter of its own mode and its caller's
(`strictest_mode`). Dry-run-ness is inherited the same way.

### Commands, scripts and telemetry

- **Send command** renders the template with `render_command`, which refuses
  `{{ }}` values that would change the command's structure (commas, quotes,
  `with` outside quotes; the open quote inside quotes) unless *Allow raw
  values*. Then `backend.send_cmd` calls `openc3.api.cmd`, so COSMOS checks,
  logs and attributes it to the microservice like any other command.
- **Run script** calls Script Runner (`script_run`) with environment
  `FLOW_RESULT_KEY`, `FLOW_RUN_ID`, `FLOW_NAME`, polls `script_get` every
  second, and on finish takes the stash value at `FLOW_RESULT_KEY` into
  `script_result`. Stopping the run stops the script. The microservice sets
  `OPENC3_API_PASSWORD` from the service password before importing openc3 so
  this authenticates.
- **Dry runs** (`run.dry_run`) replace sends with `backend.check_cmd` (unknown
  command/parameter, required, states, ranges) and skip scripts, telemetry
  writes and notifications. `skip_waits` makes waits succeed immediately.

## Triggers

`TriggerManager.tick()` every `trigger_period`:

1. Collect every non-manual trigger node of every armed flow. State per
   `(flow id, node id)` is keyed by a signature of the node's data + the
   flow's `updated_at`, so editing a flow resets its triggers.
2. Batch-read every telemetry item the conditions mention
   (`expressions.telemetry_refs`) with one `get_tlm_values`, and packet
   counts with one `get_tlm_cnts`. If the batch fails, items are read one by
   one so one bad item only breaks its own trigger.
3. `_check` each trigger. The first evaluation after arming only
   initialises state: a telemetry trigger fires on a false → true edge, never
   because the condition was already true when armed. Schedules don't burst
   after a pause. Cron is evaluated once per UTC minute.
4. Fire through `engine.trigger()`, so policy and limits apply.

Per-trigger errors are kept and shown in `/status` → `triggers.errors`.

## Expressions

`expressions.py` parses with `ast`, rejects any node type not in
`_ALLOWED_NODES` (no attribute access, calls only to names in
`_SAFE_FUNCTIONS`, no comprehensions/lambdas/imports), and evaluates the tree
itself. Limits: string results 100,000 chars, `**` exponent ≤ 1000,
`range` ≤ 10,000. Before parsing, three upper-case words in a row outside
string literals are rewritten to `tlm('A B C')`. Parsed trees are cached.
Templates are text with `{{ expr }}`; `render_value` keeps the type when a
field is a single `{{ }}`.

To add a function: add it to `_SAFE_FUNCTIONS`, document it in the module
docstring, the README table and the editor's hints, and add a test in
`test/test_expressions.py`.

## Contact planner

- **Prediction.** `invalidate(sats, stations)` queues work in `pending`; the
  `flow-predict` thread pops one satellite at a time, propagates once per
  time step with SGP4 and computes every station's look angles
  (`find_passes_for_stations`: 20 s coarse sampling, then refinement to
  0.1 s), then merges that satellite's contacts. The window is
  `now − 6 h … now + horizon`, refreshed every 6 h. Without the thread (tests)
  prediction runs inline.
- **Re-timing.** After a merge, `_retime` matches each waiting activity to
  the nearest predicted contact of the same satellite and station (by TCA).
  Within `retime_tolerance` it moves; otherwise it becomes `needs_review`
  with a suggestion (a rule's activity is cancelled and the rule re-plans).
- **Scheduling.** `tick()` starts activities whose start time
  (`contact[anchor] + offset`) has come, as long as they're within
  `late_tolerance` and before LOS; otherwise marks them missed. Paused
  scheduler → no starts. `stop_at_los` stops runs at LOS.
- **Run variables.** `run_variables` gives the run the template parameters
  and the flat `contact_*` variables (flat because expressions have no
  attribute access). `time_to_aos()` etc. read those.
- **Rules.** `_apply_rules` (every 60 s and on save) walks rules by priority
  and creates activities for matching future contacts not already used,
  honouring station capacity when `skip_full_stations`. Cancelled activities
  are kept as tombstones so a rule won't recreate them.
- **Capacity and the change version.** Antenna use is a `StationUse`: each
  station's windows sorted by AOS, so "what overlaps this pass" is a binary
  search. `_usage()` caches it with the station conflicts, keyed by
  `_version`, which `_save`, `_delete`, `_merge` and `load` bump; anything
  that changes an activity, rule, station or the contacts must go through
  one of them (or call `_changed()`), or `/status` and `/schedule` show stale
  conflicts. Planning copies it before adding to it.
- **`/schedule`** lists only activities whose pass overlaps the requested
  window, plus running and needs-review ones, without template parameters
  (`GET /activities/<id>` has them), and says how many are outside. Large
  responses are gzipped (D30).
- **TLEs.** Every change goes through `set_tle` / `_apply_tle` (keeps the last
  5, records the source, invalidates that satellite). `TleIntake` adds the
  automated inputs and the safeguards (catalog number, newer epoch, preferred
  source, jump limit → held for review).
- **Restart.** `load()` marks activities that were `running` as failed (their
  run died with the process) and re-predicts everything.

## Alerts

Engine and planner code call `alerts.emit(event, title, message, path, key,
level, data, dry_run)`. `Alerter` decides per destination (COSMOS bell,
each webhook) whether that event is enabled, de-duplicates by `key` for 60 s,
never rings the bell for dry runs, and queues webhooks for the worker
thread. COSMOS notifications go through `Logger` with type `NOTIFICATION`
(bell) or `ALERT` (bell + toast that reappears until acknowledged), via the
`notify` callback the microservice passes in. The event list with default
levels is `EVENTS` in `alerts.py`.

## Auth and audit

`FlowApi.handle` matches the route, then for any route with a permission flag
calls `TokenAuthorizer(token, write)`: it makes a cheap COSMOS JSON-RPC call
with the user's token (`get_target_names` for reads, `stash_delete` of an
unused key for writes, which needs `script_run`) and caches the result for
30 s. The username comes from the JWT claims (Enterprise) or is `anonymous`
(Core). Every write handler records an audit entry; the scheduler records its
own actions as user `scheduler`.

## Frontend

A single-spa Vue 3 app (`src/main.js`) built by Vite into
`tools/flowautomator/main.js`, which COSMOS loads at `/tools/flowautomator`.
It uses COSMOS's shared `@openc3/vue-common` (TopBar, dialogs, notify,
TargetPacketItemChooser, CommandEditor) and `@openc3/js-common` (`Api`, which
adds the token and scope).

| File | Role |
| --- | --- |
| `flowautomator.vue` | The shell: TopBar menus, flow list, Vue Flow canvas, palette, save/run/arm, approvals, polling (`/status` every 1 s, the open run every 0.5 s), switching to Contacts. URL `?flow=&run=&view=` deep-links |
| `nodeTypes.js` | Editor-side node registry: category, icon, outputs, inspector fields (with types like `condition`, `template`, `packet`, `item`, `command`, `select`), defaults, summary line. **Mirrors `graph.py` `NODE_TYPES`** |
| `FlowNode.vue` | Custom Vue Flow node: handles per output, run state colouring, problem badges |
| `NodeInspector.vue` | Renders a node's `fields` from `nodeTypes.js` |
| `TlmPicker.vue`, `CommandBuilder.vue` | Telemetry picker and Build command (wrap COSMOS components) |
| `ParamForm.vue`, `ParameterEditor.vue`, `KeyValueEditor.vue` | Template parameter values / declarations, key-value fields |
| `RunPanel.vue`, `RunHistory.vue`, `ApprovalPanel.vue` | Run log and variables, cross-flow run search, approval cards |
| `HistoryDialog.vue`, `AuditLog.vue`, `AlertsDialog.vue` | Versions/diff/restore, audit log, alert settings |
| `ContactsView.vue` | The whole Contacts tab: timeline, satellites, stations, activities, rules, schedule dialog; polls `/schedule` |
| `TleUploadDialog.vue`, `TleInputsDialog.vue` | TLE file upload, TLE input settings |
| `flowApi.js` | Every API call. Background reads send `Ignore-Errors` so COSMOS doesn't toast on every failed poll |

The tool keeps no state the engine doesn't also have: it validates through
`/validate` and the engine is authoritative for problems.

## Limits worth knowing before changing things

- Runs in progress don't survive an engine restart (they fail; activities
  are marked failed).
- One engine per scope, and the tool hardcodes `/flow-automator`, so in
  practice the plugin is installed in one scope.
- Prediction of 50 satellites × 20 stations × 7 days takes about a minute.
- At the real-pass soak's scale (16 satellites × 13 stations, 4,600
  activities over 7 days; `realdata/bench_soak.py`): `/schedule` for a day
  34 ms and 123 KB gzipped, `/status` 3 ms, a scheduler tick ~25 ms with 5
  clients polling.
- A 2,000-node flow works but a save takes a few seconds in the browser.
