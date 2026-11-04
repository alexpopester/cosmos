# Flow Automator: full evaluation (2026-11-04)

Six independent reviews of plugin 1.7.8 (branch `automation_flowgraph`, commit
`c2a0120fb`):

- the execution engine
- UI/UX
- contact scheduling
- the API and security
- tooling, tests and docs
- feature gaps against comparable tools

All six were read-only. Where a finding says **repro**, a reviewer reproduced
it with a small script, in Docker or against the code. The scripts are listed
in the [appendix](#appendix-repro-scripts). Finding IDs (ENG-, UX-, CON-,
SEC-, TOOL-, GAP-) refer to the area sections below. Where two reviewers found
the same issue, it is merged and both IDs are given.

---

## 1. Summary

| Area | Grade | One line |
|---|---|---|
| Engine | B | Well-structured locking, cancellation and approvals. Real concurrency and safety bugs (approval TOCTOU, nested Parallel runs the tail twice, STOP ALL and queue races). |
| UI/UX | B− | Feature-rich and carefully thought out. Danger colours never render, hazardous Go is one click, no undo, and Run/Arm save edits without asking. |
| Contacts / scheduling | B | Orbit math matches skyfield (≤0.28 s on AOS/LOS). Scheduler logic bugs: a cancelled pass comes back, demo un-pauses STOP ALL, no GEO. |
| API / security | C+ | Auth on every route, scopes isolated, sandbox has no code escape. Read-only users can OOM the operator container; no per-user RBAC; expression and fan-out DoS. |
| Tooling / tests / docs | B+ | 167 pytest pass (86% coverage), lint clean, docs excellent. No CI, no frontend unit tests, brittle browser tests, two 2,000-line components. |
| Feature completeness | B (B+ for a v1) | The core loop is complete and safety-minded, and contact planning is deep. Missing: pause/step, retries, a safing path, sub-flow reuse, as-run reports, a review workflow. |

**Overall: B−, held back by safety.** The architecture is sound and the
codebase is unusually well documented and tested for a plugin. Before anyone
relies on it for real commanding, it needs one short hardening pass. That pass
is section 2.1 below: about ten small fixes, mostly in `runner.py`,
`engine.py`, `expressions.py` and `ApprovalPanel.vue`. The ten are the
approval/command integrity bugs, the STOP ALL gaps, the expression limits and
the button colours. After that, the biggest returns are:

- **Editor basics:** undo, copy/paste.
- **Execution control:** pause/step, retries, an `on_failure` safing path.
- **Engineering:** CI and frontend tests.

---

## 2. Fix first (prioritised)

### 2.1 Safety and integrity: before relying on it for real commanding

| # | Issue | IDs | Where | Effort |
|---|---|---|---|---|
| 1 | **The approved hazardous command is re-rendered before it is sent.** `_gate` renders a preview, the operator approves, and `approved_hazardous=True` is set. `exec_send_cmd` then renders again and sends whatever comes out with the hazard check bypassed. Repro: approved `INST FIRE`, sent `INST SELFDESTRUCT` after telemetry changed. Verified by reading `runner.py:300-326` and `:781-795`. | ENG-1, SEC-2 | `runner.py` | S. Store the rendered string in `step` at gate time, send exactly that, and refuse if a re-render differs. |
| 2 | **Nested Parallel without an inner Join: the nodes after the outer Join run twice, concurrently.** This can send commands twice. | ENG-2 | `runner.py:188-257`, `graph.py:156` | S. Return the Join to the caller when in a parallel branch, and validate Parallel/Join nesting. |
| 3 | **Templated values can still inject command parameters (D7 bypass).** A `\` inside quotes escapes the closing quote; `[ … ]` outside quotes merges tokens. | ENG-6 | `runner.py:735-778` | S. Reject `\ [ ]`, or parse the rendered command (`extract_fields_from_cmd_text`) and require the same parameter names. |
| 4 | **STOP ALL is not airtight.** It stops runs *before* pausing the planner, and `_launch` never re-checks paused. It needs a live cmd-tlm-api permission check, so it returns 503 if the API is down. Fire-and-forget scripts are never stopped. The UI has no error handling, so a failure is silent. **Load Demo un-pauses** a scheduler that STOP ALL paused. | ENG-4, SEC-6, ENG-12, UX-5, CON-2 | `engine.py:571-592`, `contacts.py:1073-1100`, `demo.py:419`, `flowautomator.vue:1734` | S. Add one engine-wide `halted` flag checked by every run-start path; pause first; allow stop_all when auth can't be reached; track all scripts; persistent UI failure alert; don't touch pause in `load_demo`. |
| 5 | **Expressions can freeze or OOM the engine.** One request of a 2 MB literal to `/api/validate`, which needs only *read*, used 16 s CPU with the GIL held and kept 400 MB in the parse cache. Other examples: `sum([[0]*3000]*3000,[])` took 73 s; `((9**1000)**1000)**1000`, `1<<10**8` and `format(1,'>999999999')`. Trigger conditions share one thread. The engine lives in the operator container, so an OOM takes down every interface with it. | SEC-1, SEC-4, ENG-5 | `expressions.py:175-193, 312-401`, `http_api.py:112` | S. Caps on source length and AST nodes; byte-bounded cache; size and bit-length checks *before* computing; catch `RecursionError`/`MemoryError`; `/validate` needs write. |
| 6 | **No limit on fan-out.** Sub-flow runs skip `MAX_ACTIVE_RUNS`, Parallel starts a thread per branch with no cap, `max_steps` is not clamped, and sub-flow cycles aren't detected. A self-calling flow inside a 10-way Parallel gives 10^10. | SEC-5, ENG-16 | `engine.py:442-476`, `runner.py:100, 216-233` | S. One global budget for runs and threads; cap branches; clamp `max_steps`; detect cycles at validation. |
| 7 | **The run-time mode override weakens safety.** Any write user can run a crawl flow in `run` mode. Saving an armed flow keeps it armed, so `allow_hazardous`/`allow_raw_values` changes take effect immediately. | SEC-8, ENG-18 (challenges D6) | `http_api.py:306-327`, `engine.py:161` | S. Overrides may only make the mode stricter; disarm, or audit and re-arm, when safety fields change. |
| 8 | **Danger is invisible in the UI.** COSMOS tool-base CSS (`_overrides.scss:36`, `!important`) paints every flat button primary blue. So hazardous **Go**, **Abort** and **Stop all** look identical. | UX-1 | `ApprovalPanel.vue:149-180`, `flowautomator.vue:245,348,673,840` | S. Higher-specificity danger class, or outlined + `text-red`. |
| 9 | **Hazardous approval takes one click**, and the next card appears under the cursor about 0.5–1 s later, so a double-click can approve the next step. | UX-2 | `ApprovalPanel.vue:148-158` | S. Confirm (or type-to-confirm) for hazardous; ignore input on new cards for about 750 ms. |
| 10 | **A cancelled rule pass comes back** after a routine TLE update. The tombstone is keyed by a contact id containing `round(tca)`, and a 0.03° change brings it back. The id also jitters with no TLE change. | CON-1, CON-13 | `contacts.py:345, 826, 979` | S–M. Match tombstones by satellite+station+TCA window; use stable contact ids. |
| 11 | **Sub-flows aren't snapshotted or pinned.** A scheduled activity runs the *current* sub-flow, not the reviewed one, which breaks D11. | CON-4, GAP-14 | `engine.py:442-475` | M. Snapshot the sub-flow closure into the activity, or pin a version on the node. |
| 12 | **Webhooks allow SSRF.** Any host is accepted, redirects are followed, and 200 characters of the response are echoed back to the user. | SEC-7, CON-16 | `alerts.py:146-155, 322-328` | S. Block private/link-local after DNS resolution, `allow_redirects=False`, don't echo the body. |

### 2.2 Correctness bugs (next)

| Issue | IDs | Where |
|---|---|---|
| Queue-policy race: two runs of a "queue" flow can run at once (the next queued run starts after the lock is released). | ENG-3 | `engine.py:478-515` |
| A loop counter isn't reset when the loop body is exited early. Re-entry resumes the old iteration (repro). | ENG-7 | `runner.py:553-571` |
| After a crash or OOM, in-flight runs vanish with no history or alert. Queued triggers and approvals are lost. Script Runner scripts are orphaned. On shutdown, alerts stop before runs, so `run_stopped` webhooks are lost. | ENG-8 | `engine.py:78-88`, `flow_engine.py:94` |
| `/status` returns 500 intermittently: `as_json` iterates the variables while threads write to them (repro). | ENG-10 | `runner.py:499-512` |
| One bad `trigger_packet` breaks every packet trigger on every tick. One bad telemetry item causes N+1 API calls at 4 Hz forever. | ENG-11 | `triggers.py:142-155` |
| Ask ignores the flow's `approval_timeout` and can wait forever. | ENG-13 | `runner.py:369` |
| Race between approve and timeout: the audit says approved while the run failed. | ENG-14 | `runner.py:364-416` |
| A sub-flow abort shows the parent step as `done` and loses the message. A stopped branch doesn't cancel its siblings. | ENG-15 | `runner.py:268, 957` |
| `render_value` turns "nan", "inf" and "007" into numbers. | ENG-17 | `runner.py:464` |
| Scheduler network I/O (stash, S3, tlm) runs on the 1 s tick thread. A slow bucket means missed passes. | CON-3 | `contacts.py:1058` |
| GEO or always-visible satellites can't be scheduled: they produce one 54 h partial contact. | CON-5 | `orbit.py:251`, `contacts.py:531, 959` |
| Rule priority doesn't pre-empt: a higher-priority rule added later schedules nothing (repro: `skipped_full=13`). | CON-6 | `contacts.py:933-1012` |
| A flood of missed alerts after downtime (one per activity, no summary). One dead webhook stalls all the others (single thread, about 21 s per failure, unbounded queue). | CON-7 | `alerts.py:236-297` |
| The audit log is flooded by repeated "unchanged" TLE ingests (about 2,880 a day pushes real entries out of the 10k cap). | CON-8 | `tle_inputs.py:225, 348` |
| `clear_demo` deletes *any* station named Wallops/Svalbard, though the confirm says your own stations are left alone. | CON-9 | `demo.py:20, 399` |
| `approval_margin` isn't checked against the start time, so the first approval fails at once. | CON-10 | `contacts.py:711, 765` |
| A decayed or invalid TLE gives zero contacts silently, with no problem reported. | CON-11 | `orbit.py:139` |
| Re-timing can run against an empty pair list during the first prediction after a restart. | CON-12 (suspected) | `contacts.py:541` |
| `stash_take` get+delete is not atomic. | CON-15 | `cosmos_backend.py:105` |
| Saving a rule, even a rename, drops operator edits on its activities. | CON-17 | `contacts.py:933-956` |
| The TLE stash key can be pointed at any stash key, which the poller then deletes. | SEC-12 | `tle_inputs.py:115, 307` |
| Malformed payloads return 500 with type-name leaks. A negative `Content-Length` blocks a thread. No socket timeout. | SEC-9, SEC-11 | `http_api.py:652-697` |
| Restoring a version discards unsaved edits, though the confirm says nothing is lost. | UX-7 | `HistoryDialog.vue:306`, `flowautomator.vue:1520` |
| Run now, Arm and Run… save unsaved edits as a new version without asking. The toolbar "Mode" changes the saved flow, not just this run. | UX-4 | `flowautomator.vue:279-299, 1591` |
| Save As Copy detaches the editor if the save fails. | UXBUG-1 | `flowautomator.vue:1529` |
| Unhandled async errors: stopAll, clearDemo, deleteFlow, reloadTheirs, scheduler pause/resume, exportVersion. | UXBUG-2 | various |
| Opening an approval from Contacts doesn't switch to Flows. The telemetry picker appends rather than inserting at the cursor. Importing non-flow JSON shows a TypeError. | UXBUG-3, -4, -6 | `flowautomator.vue`, `NodeInspector.vue` |

---

## 3. Engine (grade B)

**Strengths:**

- The lock order (planner → engine) is documented, and no deadlock path was found.
- Waits can be cancelled (100 ms loops), and stopping a run also stops its scripts.
- Each run gets a deep-copied flow, and validation runs on every start path.
- A save that fails validation disarms the flow.
- Approval deadlines are inherited by sub-flows.
- Triggers use edge semantics with batched reads and no burst after a stall.
- Every Redis list is capped.
- Hazardous commands fail safe through COSMOS's own check.

**Bugs:** ENG-1…17 are in section 2.

**Performance:**

- **ENG-9:** `list_runs` parses up to 200 full runs (≤1,000 steps each) and then throws the steps away. The UI calls it every second while anything is running. `get_run` for a finished run scans the history of every flow.
- **ENG-19:** listing versions parses all 200 full snapshots, about 200 MB for a 2,000-node flow. History of deleted flows is never purged.

**Smaller items:**

- Edge triggers sample at 4 Hz, so pulses shorter than 250 ms are missed.
- Interval schedules use wall-clock time, not monotonic.
- Cooldown exists only for tlm triggers.
- `save_flow` writes the flow and the version outside one MULTI transaction.
- There is no leader lock, so two engine instances during an upgrade would both fire.

**Recommendations:**

| Recommendation | Effort |
|---|---|
| Persist the run lifecycle (an active-runs hash plus script ids) to recover from crashes, clean up orphans and keep history honest | M |
| `halted` gate | S |
| Approve what you send, and put the approver in the COSMOS command log | S |
| Structural validation: nesting, unreachable nodes, sub-flow existence and cycles, warnings for unknown packets or items | S/M |
| Expression budget | S |
| Run storage redesign: summaries separate from full runs, get-by-id, truncated large variables | M |
| Observability: tick duration and lag, active/queued counts, payload sizes in `/status` | S/M |
| A regression test for each repro | S |

## 4. UI/UX (grade B−)

**Strengths:**

- Clear two-row toolbar, with Stop all always pinned.
- View/Edit lock, foldable rails, and a layout remembered per browser.
- Good empty states and field hints.
- Validation shows up as badges, an inspector alert and a Problems tab that jumps to the node.
- Live node lighting; clicking a log row centres the node.
- An All running tab that rolls sub-flows up under their caller.
- Approval cards with the exact rendered command, and cross-flow toasts and a chip.
- Save-conflict dialog, unsaved-changes guards, and dry runs everywhere.
- A contacts timeline with a now-line, conflicts and Rehearse.

**Findings beyond section 2:**

- **UX-3, High:** no undo/redo, copy/paste/duplicate or Ctrl+S. `Delete` *and* `Backspace` delete immediately (`flowautomator.vue:494`). The inspector trash button has no confirm.
- **UX-6:** the canvas shows no hazardous / allow-hazardous badge, so reviewers can't see where the dangerous steps are.
- **UX-8:** the editor opens in Edit, including from alert and run deep links. View mode has no read-only inspector.
- **UX-9:** the run log doesn't auto-scroll and has no filter, durations or export. Variables are shown as raw JSON in a 220 px column. `nodeLabel` is O(steps × nodes) on every 0.5 s poll.
- **UX-10:** fixed-rate polling with no pause when the tab is hidden: `/status` 1 s, the full run 0.5 s, `listRuns(50)` 1 s, `/schedule` 2 s. Every edit POSTs the whole flow to `/validate`.
- **UX-11:** native `window.confirm` is used in about 8 places.
- **UX-12:** keyboard access and aria: the palette is draggable divs, the timeline items are click-only divs, there are no aria-labels, and tooltips work on hover only.
- **UX-13:** layout glitches. The condition hint overlaps the Timeout label (`04_problem`). The Parameters caption in the schedule dialog collides with the fields (`contacts_02`).
- **UX-14:** an approval card has no "Show step" and covers the top-centre of the canvas.
- **UX-15:** contacts density. Actions are icon-only, pass labels are truncated, there is no pan/zoom on the timeline, and the schedule dialog defaults to walk rather than the template's mode.
- **UX-16:** contrast (white on green chips is about 2.8:1). Hard-coded hex colours ignore the COSMOS themes.
- **UX-17:** the minimap is a grey smear on large flows, and there is no auto-layout or find-node.
- **UX-18:** the empty state still shows Run/Save/settings as enabled.
- **UX-19:** imports without a mode default to `run`.
- **UX-20:** screenshots were captured mid-transition, and some are stale.
- **UXBUG-5:** child components change their props in place, which is also what blocks undo.
- **UXBUG-7:** `revokeObjectURL` runs immediately after `click()`, `seenApprovals` is unbounded, and a burst of toasts appears on first load.

**Maintainability:**

- Split `flowautomator.vue` (2,421 lines, 78 methods) and `ContactsView.vue` (2,006 lines) into dialog components, plus `useFlowEditor` and `useEnginePoll` composables.
- `statusColor`, `fmtTime` and `duration` are duplicated in 6 files; move them to one module.

**Top UX improvements, by impact ÷ effort:**

1. Danger colours (S).
2. Hazardous confirm plus an input delay on new cards (S).
3. Undo/redo, with Backspace removed from the delete keys meanwhile (M).
4. Ask before saving on Run/Arm, and make toolbar mode a per-run override (S).
5. Harden Stop all (S).
6. View mode by default for deep links, plus a read-only inspector (S–M).
7. HAZ badges (S–M).
8. Run log: follow, filter, JSON tree, "Show step" on cards (M).
9. Copy/paste, auto-arrange, find node (M).
10. Split the monoliths and smarter polling (L).

## 5. Contacts and scheduling (grade B)

**Orbit accuracy, checked against skyfield:** 7 cases (ISS at a mid-latitude
station at 0° and 10° masks, grazing passes at 60°, polar orbits at Svalbard
and McMurdo, Molniya). Pass counts were identical. Worst errors: AOS/LOS
≤0.28 s, TCA ≤0.12 s, max elevation ≤0.005°. The frames are correct (TEME via
GMST, WGS-84 geodetic ENU), and edge finding bisects to 0.1 s with grazing
passes recovered.

**Strengths:**

- Unix UTC throughout, so there are no DST issues.
- A clear anchor/offset/late-tolerance model.
- D11 snapshots with "template changed".
- TLE intake checks the catalog number, epoch, jump distance and preferred source.
- Template parameters are strictly typed.
- Rehearsal uses the real contact.

**Bugs:** CON-1…17 are in section 2.

**Performance (CON-14):** `/schedule` returns every activity whatever the
window: 0.40 s under the planner lock and 2.4 MB for a realistic fleet. It is
polled every 2 s per client, and each activity stores a full flow copy.

**Accuracy and model limits:**

- **CON-18:** stale TLEs only produce a single 3-day notification and never block an unattended run. TLEs with a future epoch are accepted. Contacts don't carry the TLE age.
- **CON-19:** no refraction, which puts AOS 20–40 s late at a 0° mask. There is no azimuth/terrain mask, and capacity ignores setup offsets and slew time.

**Scheduling gaps, ranked by value:**

1. Real conflict resolution: priority that pre-empts, occupancy including setup/teardown and slew, and an automatic alternate station (M).
2. Station constraints: az/el mask, keyhole, slew rate, blackout windows (M).
3. GEO / continuous visibility, plus time-based (cron) activities alongside pass-based ones (M).
4. Export: iCal, CSV/JSON pass lists, CCSDS OEM, STK `.int`, station-request formats (S–M).
5. Doppler and range-rate curves and az/el track tables; the velocity is already computed and thrown away (S).
6. Sun and eclipse constraints (S).
7. TLE-quality gating (S).
8. Link-budget, range and time-above-X° filters (M).
9. Batch editing of activities (S).
10. Multi-scope fleet view (L).

## 6. API and security (grade C+)

**Strengths:**

- Auth is centralised in the route table. Live probes confirmed 401 without a token or with a bad one.
- COSMOS verifies the tokens, and JWT claims are trusted only after that.
- Scope isolation holds: the scope is fixed per engine and IDs are regex-limited.
- No cookie auth or CORS, so CSRF doesn't apply.
- The sandbox's AST allow-list held: no attribute or dunder access, comprehensions, lambdas or imports.
- Dry runs skip every side effect.
- Caps: 5 MB body, 50 active runs, sub-flow depth 10.
- Every write is audited and mirrored to the COSMOS log.

**Beyond section 2:**

- **SEC-3, High on Enterprise:** every action runs as the microservice, and Python `authorize()` is a no-op there. Anyone with `script_run` can command *any* target, use `set_tlm` and run any script with service rights, and COSMOS logs credit the service. Fixes: check per-user target permissions at save, arm, run and approve, or send through cmd-tlm-api with the user's token. Require `admin` for `allow_hazardous`, `set_tlm` and `run_script` nodes. (L)
- **SEC-10:** audit gaps. Denied attempts, trigger starts and `allow_hazardous` sends are not audited. Failed audit writes are swallowed. The 10k Redis list can be edited.
- **SEC-13:** readers can see script env values in step messages, run variables and webhook `last_error`.
- **SEC-14:** `sgp4>=2.23` has no upper bound. Five npm dependencies are unused (astro-web-components, axios, lodash, date-fns, sprintf-js). The docs name the `tlm` permission for the read probe, but it actually needs `system`.

## 7. Tooling, tests and docs (grade B+)

**Measured results:**

- pytest: **167 passed** in about 34 s, **86% line coverage**, hermetic (FakeBackend/MemoryRedis).
- `pnpm lint --max-warnings 0` on `src`: **0 problems**.
- `ruff check` and `ruff format --check`: clean.
- shellcheck: clean.
- `graph.NODE_TYPES`, `runner.EXECUTORS` and `nodeTypes.js` agree (26 each). The `CosmosBackend` and `FakeBackend` method sets are identical.
- The build is reproducible: the source hash matches.
- Heavy artefacts are git-ignored.

**Findings:**

- **TOOL-1, High:** no CI covers the plugin. No workflow path matches `openc3-cosmos-flowautomator/`.
- **TOOL-2, High:** no frontend unit tests for about 7,000 lines of Vue/JS.
- **TOOL-3:** the browser tests are plain Node scripts rather than `@playwright/test`:
  - about 50 fixed sleeps;
  - `DEFAULT` scope hard-coded;
  - they need a live COSMOS, a password, `docker exec` and a real pass, and take about 10 minutes;
  - helpers are copied into 9 files;
  - console errors don't fail a test.
- **TOOL-4:** linting `scripts/`, `demo/` and `test/ui/` gives 575 problems (40 `no-console`, 535 prettier).
- **TOOL-5:** god files: `flowautomator.vue` 2,421 lines, `ContactsView.vue` 2,006, `contacts.py` 1,362, `runner.py` 1,026. Move the `exec_*` functions into a `nodes/` package.
- **TOOL-6:** gemspec placeholders (`Anonymous`, `name@domain.com`, `your-github`). The store image is missing. `minimum_version` is 6.0.0 but the plugin needs 7.4.x. `package.json` is still at version 1.0.0. There is no `packageManager` field (the image has pnpm 10, the repo uses 11).
- **TOOL-7:** every install needs a manual version bump (40 versions in two days). `setup_demo.sh` picks the gem with `ls -t` and then `sleep 20`. `run.sh` sorts versions lexically. There is no CHANGELOG.
- **TOOL-8:** coverage gaps:
  - `flow_engine.py` 0%;
  - `cosmos_backend.py` 40%;
  - `http_api.py` 72% (update/conflict, satellite/station/TLE, rehearse, alerts, rules, pause handlers);
  - `graph._validate_node_data` largely untested;
  - `subflow`, `loop`, `trigger_schedule` and `set_tlm` are not in the demos.

  `orbit.find_passes` is used only by tests. Production uses `find_passes_for_stations`, which has duplicated sampling code and no direct test.
- **TOOL-9:** doc drift:
  - the `rehearse` route is missing from the `http_api` docstring, and `GET /api/runs` is listed twice;
  - the "root-owned /gems" venv explanation looks stale;
  - the README install line has no scope;
  - commands are duplicated across 3 docs;
  - `plugin.txt` exposes `flow_route_prefix`, but `flowApi.js` hard-codes the prefix.
- **TOOL-10:** typing: 6 of 423 Python functions have hints; no JSDoc or TypeScript.
- **TOOL-11:** bundles of 1.26 MB and 502 KB; all of vue-common's widgets are bundled.
- **TOOL-12:** `sys.path` includes every gem's `lib`. `test/ui` has no lockfile. `setup_demo.sh` splices the password into JSON. There are 37 `except Exception` blocks.

**Recommendations, in order:**

1. A path-filtered GitHub Actions workflow: pytest with coverage ≥85%, ruff, eslint, build, gem (S).
2. A cross-language registry contract test, plus a backend parity test (S).
3. Widen lint to the scripts, then run prettier once (S).
4. Fix the metadata, drop unused dependencies, fix the doc drift (S).
5. Auto-versioned dev builds plus `rake dev`/install with a health poll, and a CHANGELOG (S–M).
6. vitest for the pure modules; table-driven `FlowApi` and `validate_flow` tests (M).
7. Move to `@playwright/test`: shared fixtures, API polling instead of sleeps, configurable scope, slow tests tagged (M).
8. Refactor the god files, after 6 is in place (L).
9. Gradual typing (M).

## 8. Feature gaps (grade B / B+ for a v1)

**Where it stands:**

- **Stronger than Script Runner** for event-driven, unattended, pass-relative work.
- **Roughly at par with Enterprise Autonomic** on triggers, and ahead of it on sequencing.
- **Weaker** than Script Runner, ITOS/STOL and SCOS/MOIS on interactive control and as-run records.
- **Weaker** than Node-RED and Temporal on reuse, error handling and durability.

**Differentiators:**

- The safety model: disarmed by default, strictest mode inherited by sub-flows, approval cut-off at LOS.
- Real dry runs against COSMOS command definitions.
- Contact planning in the same engine.
- Versions with diff and restore.
- Clear recorded design decisions.

| ID | Gap | Value | Effort |
|---|---|---|---|
| GAP-1 | Pause, resume, single-step and breakpoints on a live run | H | M |
| GAP-2 | Durable runs: resume after an engine restart, with confirmation | H | L |
| GAP-3 | Per-node retries and backoff | H | S |
| GAP-4 | `on_failure`/`on_stop` safing sub-flow, which runs even on Stop | H | M |
| GAP-5 | Run-level timeout and step-overrun alarm | M | S |
| GAP-6 | Named resource locks / critical sections across flows | H | M |
| GAP-7 | Priorities, start or re-run from a node, node bypass | M | M |
| GAP-8 | Allow-listed COSMOS API node (interfaces, limit sets, logging) | H | S/M |
| GAP-9 | More triggers: flow completed, interface state, stash/event, inbound webhook payload | M/H | M |
| GAP-10 | Limits read/wait nodes (`get_out_of_limits`) | M | S |
| GAP-11 | Telemetry staleness (`tlm_age`) and windowed statistics (mean, rate) | H | S/M |
| GAP-12 | File and data nodes (target files, CSV/JSON, tables) | M | M |
| GAP-13 | Verified command (an optional verify expression and timeout on Send command) | H | S |
| GAP-14 | Sub-flows with parameters and outputs, pinned to a version | H | M |
| GAP-15 | Draft → reviewed → approved release state; only approved versions can be armed or scheduled | H | M |
| GAP-16 | Editor basics: undo, copy/paste, comments/groups, auto-layout, search | H | M |
| GAP-17 | Tags and folders for flows | M | S |
| GAP-18 | Text/YAML round-trip; export to a Script Runner script; import from procedures | M | L |
| GAP-19 | Starter template gallery | L/M | S |
| GAP-20 | Two-person rule; approver ≠ starter (`runner.py:389` doesn't check) | H (Enterprise) | M |
| GAP-21 | As-run report export (PDF/HTML/CSV) for runs, audit and schedule | H | S/M |
| GAP-22 | Shift handover, presence, notes on runs and activities | M | M |
| GAP-23 | Email/PagerDuty destinations, ack and escalation | M | S/M |
| GAP-24 | Prometheus metrics | M | S |
| GAP-25 | Calendar/Timeline and Autonomic interop (Enterprise) | M | M |
| GAP-26 | Scheduling inputs/outputs (see section 5) | M/H | M |
| GAP-27 | Multi-scope (the prefix is hard-coded) and per-flow ACLs / RBAC (see SEC-3) | H (Enterprise) | M/L |

---

## 9. Roadmap

### Phase 0: hardening

Section 2.1 items 1–10 and 12:

- Approval integrity.
- Nested Parallel.
- D7 bypass.
- `halted` gate and STOP ALL fixes.
- Expression limits.
- Fan-out budget.
- Mode as a minimum.
- Danger colours and hazardous confirm.
- Tombstone matching.
- Webhook SSRF.

Add CI (TOOL-1) in the same pass, with a regression test for every repro.

### Phase 1: Now

- The rest of section 2.2.
- Undo/redo and copy/paste (UX-3, GAP-16).
- Prompt before saving on Run/Arm (UX-4).
- Per-node retries (GAP-3).
- Verify on send (GAP-13).
- Staleness functions (GAP-11).
- Run timeout (GAP-5).
- `on_failure` safing path (GAP-4).
- Pinned sub-flows (CON-4, GAP-14).
- As-run export (GAP-21).
- COSMOS API and limits nodes (GAP-8, GAP-10).
- Run storage and polling redesign (ENG-9, UX-10, CON-14).
- Frontend unit tests (TOOL-2).

### Phase 2: Next

- Pause, step and breakpoints (GAP-1, GAP-7).
- Parameterised sub-flows and a library with tags (GAP-14, GAP-17, GAP-19).
- Resource locks (GAP-6).
- New triggers (GAP-9).
- Release workflow (GAP-15).
- Metrics (GAP-24).
- Email/PagerDuty (GAP-23).
- Pre-empting priority and station blackout windows, plus GEO support (CON-5, CON-6).
- Refactor the god components (TOOL-5).
- Move the browser tests to `@playwright/test` (TOOL-3).

### Phase 3: Later

- Durable runs (GAP-2, ENG-8).
- Enterprise RBAC: two-person rule, per-user command identity, multi-scope (SEC-3, GAP-20, GAP-27).
- Calendar and Autonomic interop (GAP-25).
- DSL round-trip (GAP-18).
- Antenna pointing, Doppler, OEM, az/el masks, eclipse, iCal export (GAP-26).
- Collaboration (GAP-22).

---

## Appendix: repro scripts

The reviewers' scratch scripts are in the session scratchpad. They are
temporary, so copy any you want to turn into regression tests:

- **Engine:** `test_eval_repro.py` (approval TOCTOU, nested Parallel, loop counter, variables race, expression limits).
- **Contacts:** `sky_cmp.py` (skyfield comparison), `test_eval.py` and `test_eval2.py` (tombstone resurrection, GEO, priority, approval margin).
- **Security:** `parsebomb.py`, `val.py`, `one.py` and `run.sh` (validate DoS, expression bombs, malformed payloads).
- **Tooling:** `pytest.txt` and `lint.txt` (full test, coverage and lint output).

Base path:
`/tmp/claude-1000/-home-alex-Projects-cosmos/fa116981-c13f-460f-89bf-23004132028f/scratchpad/eval_{engine,contacts,security,tooling}/`
