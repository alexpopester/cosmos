# Flow Automator: second evaluation (2026-11-05)

Nine independent reviews of plugin **1.11.0** (branch `automation_flowgraph`,
commit `13cb4e2ba`). The [first evaluation](EVALUATION-2026-11-04.md) looked
at 1.7.8. Its fixes shipped in 1.8.0–1.11.0.

| Reviewer | Prefix | Section |
|---|---|---|
| Execution engine | E- | [3](#3-engine-grade-c) |
| UI/UX | U- | [4](#4-uiux-grade-c) |
| Contacts and scheduling | C- | [5](#5-contacts-and-scheduling-grade-b) |
| Security | S- | [6](#6-security-grade-c) |
| Documentation | DOC- | [7](#7-documentation-grade-b) |
| Maintainability | M- | [8](#8-maintainability-grade-b) |
| Review of the 1.8–1.11 fixes | R- | [9](#9-review-of-the-18111-fixes-grade-c) |
| Performance and reliability (measured) | P- | [10](#10-performance-and-reliability-grade-c) |
| Operator workflows and feature gaps | G- | [11](#11-operator-workflows-and-feature-gaps-grade-b) |

All nine were read-only. Benchmarks ran in throwaway containers, not against
the local COSMOS.

- **Repro'd** means a reviewer wrote a test that shows the bug. Each repro
  test asserts the *buggy* behaviour, so it passes while the bug exists.
  Turn it around when you fix the bug and keep it as a regression test.
- **Read** means found by reading the code.
- **Suspected** means not confirmed.

Repro scripts are listed in the [appendix](#appendix-repro-scripts).

---

## How to use this document

1. **Work from section 2.** It is the whole backlog, de-duplicated and in
   priority order. Each item is one change, with every reviewer ID that found
   it, the files to touch, the fix and a "done when" test.
2. **Sections 3–11 are the evidence.** They hold each reviewer's grade,
   strengths and full findings. Items already in section 2 are marked with
   their backlog number (for example **→ B3**), so nothing is listed twice.
3. **Track progress in the Status column.** Change `Open` to
   `Fixed in 1.12.0` (or `Won't fix: <reason>`) in the same commit as the
   fix. Then `grep -c "| Open |"` counts what's left. The CHANGELOG entry
   should cite the backlog number and IDs.
4. **Decisions first.** Section 2.0 lists questions only the owner can
   answer. Several fixes depend on them, and each answer needs a new or
   amended D-entry.
5. **Turn every repro into a regression test** in the module's own test file
   (M-9), named after the "done when" column.

---

## 1. Summary

| Area | Grade | Was | One line |
|---|---|---|---|
| Engine | C+ | B | Locking and cancellation are sound. But nodes after a Join still run twice, STOP ALL does nothing during a Valkey outage, and walk mode can send a hazardous command nobody approved. |
| UI/UX | C+ | B− | The 1.8 safety UI holds. The editor can silently change what a flow does: deleting a Switch case re-routes another case, and the variables editor corrupts values as you type. |
| Contacts / scheduling | B− | B | Orbit maths matches skyfield to 0.28 s. A station edit leaves activities on the old AOS/LOS. Rule priority inverts on batch prediction. A GEO activity blocks a station all day. |
| Security | C+ | C+ | Auth, the sandbox and D7 hold. New in 1.10/1.11: any write user can send any COSMOS secret to any host. Readers can see webhook secrets. One dry run can OOM the container. |
| Documentation | B | (B+) | Plain, thorough and mostly current. A few statements are wrong on safety points (for example the mode floor). There is no expression, API or troubleshooting reference. |
| Maintainability | B− | (B+) | 266 hermetic tests, 87% coverage, ruff clean. Still no CI and no frontend tests, and the god files grew 10–15%. |
| 1.8–1.11 fixes | C+ | — | Most fixes are real and tested. The ENG-7 fix caused a P0 regression. Five "closed" safety items can still be bypassed. |
| Performance / reliability | C | — | Triggers and run soak are fine. The planner stops scaling at about 10 satellites × 5 stations. A Valkey that stops answering freezes the engine lock indefinitely. |
| Operator fitness | B− | B | Good for attended nominal procedures and simple pass automation. An operator is stuck once a step fails or needs a retry, skip, verify or report. |

**Overall: C+.** The 1.8–1.11 hardening fixed most of what the first
evaluation found, and the core design is still sound. But going deeper found
**18 safety and integrity items** (section 2.1). Two of them are regressions
introduced by the fixes, and five are bypasses of fixes recorded as closed.
Until 2.1 is done, don't use the plugin for unattended real commanding.

Common causes behind many findings, each fixed once in section 2:

- **Safety rules are checked per start path, not in one place.** The mode
  floor lives in `trigger()`, so activities skip it, and STOP ALL has no
  engine-wide halt. Put the checks in `FlowRun.__init__` and one `halted`
  flag (B5, B3).
- **"What you approve is what is sent" has three holes:** the re-render, a
  preview that failed to render, and the node's Allow-hazardous flag (B6).
- **Valkey is called while holding `engine.lock`, with no socket timeout.**
  This one cause explains E-1, E-11, R-18, P-6 and P-7 (B3).
- **User-supplied fields are trusted:** `subflow_snapshots`, `created_by`,
  the Space-Track URL and secret names, the catalog number, and
  `FLOW_RESULT_KEY` (B8, B10).

---

## 2. Backlog (prioritised)

Effort: **S** < half a day, **M** 1–3 days, **L** > 3 days.

### 2.0 Decisions needed from the owner first

| # | Question | Affects | Reviewer suggestion |
|---|---|---|---|
| Q1 | Should a scheduled activity or rule be allowed to run **less cautiously** than its template's mode? Today it can (crawl template, run-mode activity, no approvals). | B5, DOC-1, D6 | No. Enforce the floor in `FlowRun.__init__` for every real run. **Decided 2026-11-05: as recommended.** |
| Q2 | After approval, send the **exact approved string** (so `now()` and changing telemetry work), or keep re-render-and-refuse? | B6, R-7, D6 | Send the approved string verbatim. **Decided 2026-11-05: as recommended.** |
| Q3 | In crawl/walk, should a node's **Allow hazardous** flag ever stand in for an approval? | B6, E-3, D6 | No. Only an operator approval acknowledges a hazardous command in crawl/walk. **Decided 2026-11-05: as recommended.** |
| Q4 | Let COSMOS's own `cmd()` detect hazards (catch `HazardousError`, then ask), instead of the plugin's check, which ignores defaults and is case-sensitive? | B6, D9 | Yes, to remove the mismatch without a core change. **Decided 2026-11-05: as recommended.** |
| Q5 | Should STOP ALL, disarm and pause be **memory-first and never fail**, persisting later? | B3, D28 | Yes. Add to D28: "writes that reduce activity never fail". |
| Q6 | A pass the operator **cancelled** comes back as a new rule activity if a maneuver moves it more than 120 s. Keep that, notify, or keep it cancelled? | C-9, D12, D23 | At least notify. Keep hand-edited (detached) activities. |
| Q7 | Should STOP ALL run cleanup (`on_stop`) flows once those exist? | G-1, D6 | Owner's call. |
| Q8 | Where may the Space-Track host be changed: nowhere, or only in microservice config by an admin? | B10, D29 | Only in plugin variables or admin config, https only. |

### 2.1 Safety and integrity: before any unattended real commanding

| # | Issue | IDs | Where | Fix | Effort | Done when | Status |
|---|---|---|---|---|---|---|---|
| B1 | **Loops in parallel branches reset each other, so commands go out extra times.** This is a regression from the ENG-7 fix: a count-3 loop sent `INST B` 6 times (1.7.8 sent it 3 times). `_reset_loops_since` uses the run-wide `step_count`, and `step_count += 1` isn't atomic across threads. | R-1, E-4 | `runner.py:614-650` | Keep loop counters, entered/iterated markers and the step counter per branch (thread-local `self._local`, like `variables`). Reset only loops nested in the current loop. Also fix re-entry without an outer loop (R-27). | M | `test_loops_in_parallel_branches_count_independently`: 3 × `INST A` and 3 × `INST B` in each of 20 runs, and the nested-loop test still passes. | Fixed in 1.12.0 |
| B2 | **Nodes after a Join still run twice, concurrently (ENG-2 incomplete).** (a) With P1→{P2,P3}→J and no inner Joins, `join_owners(J)={p2,p3}`: `INST AFTER` ran twice in 5/5 runs. (b) The Join-owner cache is filled lazily with no lock, so a padded 1,500-node flow also sent twice in 5/5 runs. | E-2, E-15 | `graph.py:303-333`, `runner.py:206-217` | (1) In `validate_flow`, reject unstructured nesting with a depth-counting walk: every branch must reach its Parallel's own Join or end. (2) Pair Parallels with Joins once, eagerly, in `Graph.__init__`, using an adjacency map (no lazy cache, and it removes the O(N·E) scan). (3) Keep a runtime check. | M | `test_nested_parallels_without_inner_join_run_after_join_once` (refused or sends once over 20 runs), and `test_join_pairing_is_computed_at_graph_build`. | Fixed in 1.12.0 |
| B3 | **STOP ALL is still not airtight.** (a) **During a Valkey outage it stops nothing and returns 500.** `set_paused` writes first and raises, so no flow is disarmed and no run stopped; the run later sent `INST FIRE`. Measured: 6.9 s, then an error, with 1 run active and 2 flows armed. (b) **If Valkey stops answering** (a paused container or network partition, not a refused connection), `engine.lock` is held indefinitely. 0 of 20 `/status` calls were answered within 1 s, and the trigger never returned, because there's no socket timeout. (c) STOP ALL **during an approved step still sends** the command, and the run shows `success`. (d) 64 **unauthenticated** slow requests fill every API slot, so STOP ALL gets 503. (e) Engine shutdown **starts the next queued run**. (f) `detached_scripts` forgets scripts after 200. (g) STOP ALL with 6 detached scripts took 30 s (one 5 s stop after another). | E-1, E-10, E-11, E-13, R-2, R-8, R-18, R-22, S-9, S-10, P-6, P-7 | `engine.py:76, 109-121, 613-668, 727-779, 839-852`; `contacts.py:1144-1148`; `store.py:102-103`; `runner.py:288-293, 396-403, 868-884`; `http_api.py:728-811` | (1) One engine-wide `_halted` flag, set first under the lock and checked by every start path, including on-finish queue starts and shutdown. (2) STOP ALL, disarm and pause change memory first, then persist with deferred writes (add `defer` to `FlowStore.save_flow`), returning `persisted:false` instead of raising (Q5). (3) No Valkey I/O while holding `engine.lock`: `_remember_run` deferred; arm and delete persist first. (4) Give the plugin its own Valkey client with `socket_timeout`/`socket_connect_timeout` of about 2 s, so a hung Valkey errors instead of hanging (no core change). (5) Check `run.cancelled()` after `_gate` returns and right before every backend call; cancellation wins in `_wait_for_approval`. (6) Check the token before reading the body, reserve 2 slots for `stop_all` and `health`, and add a whole-request deadline. (7) Stop scripts in parallel with a timeout; keep detached scripts in a pruned hash. | M | `test_stop_all_works_while_valkey_is_down`; `test_status_answers_while_valkey_is_paused` (a blackhole fake, `/status` < 0.5 s); `test_stop_all_during_approved_step_sends_nothing`; `test_shutdown_starts_nothing`; `slots.py` repro: STOP ALL works with 64 slots held. | Fixed in 1.12.0 |
| B4 | **Relaxing an armed flow can leave it armed.** Missed changes: turning **dry run off** (the demo "Hot temperature watch" would then send real commands on its next trigger); editing the **command** of an Allow-hazardous node (`INST NOOP` → `INST SELFDESTRUCT`); adding `run_script` or `set_tlm` nodes; relaxing a **sub-flow** that an armed flow calls. | E-7, R-10, S-6 | `engine.py:30-42, 397-403` | `less_safe_changes` should also flag: dry_run true→false; any change to command, type or flow_id on a node with Allow hazardous or Allow raw values; new `send_cmd`/`run_script`/`set_tlm` nodes. When sub-flow S is saved with a less safe change, disarm every armed flow whose sub-flow closure contains S, under the same lock and with a reason. | S | `test_turning_dry_run_off_disarms`, `test_relaxing_a_subflow_disarms_armed_callers`, `test_editing_hazardous_command_disarms`. | Fixed in 1.12.0 |
| B5 | **Scheduled activities and rules bypass the mode floor.** A crawl template can be scheduled, by hand or by a rule, in `run` mode, and runs with no approvals. | E-6, R-6, DOC-1 (Q1) | `engine.py:625-664`; `contacts.py:749-756, 814-820`; `runner.py:119` | Enforce `strictest_mode(requested, flow.mode)` for every non-dry run in `FlowRun.__init__`. The planner rejects a less cautious mode on activity create, update and refresh, and in `save_rule`. Update README:211 and D6. | S | `test_activity_mode_cannot_be_less_cautious_than_template` (400 on create; a scheduled run uses crawl). | Fixed in 1.12.0 |
| B6 | **Walk/crawl can send a command the operator never approved.** (a) The gate decides hazard from the preview and fails open on a lookup error. The executor re-renders and, because the node has *Allow hazardous*, sends a hazardous command with no approval. (b) If the preview fails to render, the operator approves the node label or "(preview unavailable)", and the executor sends whatever renders later. `_preview` has no `send_cmd` case. (c) The plugin's hazard check ignores parameter defaults and is case-sensitive, unlike COSMOS. (d) Re-render-and-refuse means a command using `now()` or changing telemetry can never be sent. (e) Hazard is detected in the UI by substring-matching the reason text. | E-3, E-9, R-7, R-9, U-20 (Q2–Q4) | `runner.py:330-357, 772-810, 868-884`; `cosmos_backend.py:54-77, 163-175`; `ApprovalPanel.vue:48, 272` | In crawl/walk: (1) if the preview can't be rendered, fail the step; (2) store the rendered string and send it verbatim (Q2); (3) only `step["approved_hazardous"]` acknowledges a hazard, never the node flag (Q3); (4) a lookup exception counts as hazardous; (5) on `HazardousError` from COSMOS, ask with the final string, then send exactly that (Q4); (6) add `hazardous: bool` to the approval record for the UI. | S–M | `test_walk_never_sends_hazardous_without_approval` (TOCTOU, lookup error, hazardous default); `test_crawl_never_sends_a_command_the_operator_did_not_see`; `test_now_in_approved_command_is_sent_as_approved`. | Fixed in 1.12.0 |
| B7 | **A flow can carry hidden sub-flow content.** `save_flow`, import and restore keep a user-supplied `subflow_snapshots`, and runs use it: the visible child sends `INST SAFE`, the run sent `INST EVIL`. The editor doesn't show it, and the disarm check doesn't see it. | R-5, S-4 | `engine.py:336-421`; `runner.py:76`; `backup.py:203` | Strip `subflow_snapshots` (and other server-owned keys: `created_by`, `version`, `saved_by`) on save, import, restore and `_recreate`. Pass snapshots to `FlowRun` only from the planner, as a constructor argument. | S | `test_r_snap.py` inverted: the stored flow has no such key, and the run sends `INST SAFE`. | Fixed in 1.12.0 |
| B8 | **Write users can exfiltrate any COSMOS secret, and inject into Space-Track queries.** `PUT /api/tle_sources` accepts any `spacetrack.url` (plain http too) and any secret names, and the fetcher POSTs those secrets to `<url>/ajaxauth/login`. A restored backup can carry the same settings. The catalog number isn't validated: `>0/limit/100000` lands in the query URL. | S-1, C-5, R-3, R-13 (Q8) | `tle_sources.py:154-170, 180-183, 247-284`; `http_api.py:167`; `backup.py:162`; `contacts.py:419-420` | Pin the host to `www.space-track.org`, overridable only by plugin variable; https only. Secret names must start with `SPACETRACK_`. A restore can't set the URL or secret names. Accept only digit or Alpha-5 catalog numbers (and convert Alpha-5 for Space-Track, C-17). | S | A non-Space-Track host, http, or an unprefixed secret name → 400; a restore can't change them; `test_catalog_path_injection_into_spacetrack_query` is refused. | Fixed in 1.12.0 |
| B9 | **Expressions can still OOM the operator container.** List, tuple and dict literals skip the D22 size caps; `render()` has no total output cap; step messages build a full `repr` before truncating. One Set variable node of doubling lists: exit 137 (OOMKilled) under a 1 GB limit. Separately, an expression within the caps can still take 2–4.6 s of CPU, which stalls the trigger thread. | E-5, S-3, R-19, S-11 | `expressions.py:380-389, 435-439`; `runner.py:554, 660` | (1) `_too_big` on List/Tuple/Dict results in `_eval`. (2) One `run.set_variable` helper that checks `size_of`, used by set_var, read_packet and ask. (3) Total output cap in `render()`. (4) `reprlib.Repr` for step messages. (5) A node budget in `_json_safe`. (6) An operation or time budget per evaluation. | S | `test_doubling_lists_are_refused` (fails at the node in < 1 s, heartbeat stall < 0.2 s); `test_oom_flow.py` inverted. | Fixed in 1.12.0 |
| B10 | **Read-only users can see secrets, and attribution can be forged.** Webhook connection errors store the full URL (secret path included) in `last_error`, returned by `GET /api/alerts` and logged (the unfixed half of SEC-13). Readers also see script environment values, Ask answers and stash results. A user-supplied `created_by` is kept, and restore writes backup version history straight into the store. `FLOW_RESULT_KEY` can be overridden so the engine reads and deletes any stash key. | S-2, S-5, S-13, S-14 | `alerts.py:323, 335`; `engine.py` (save); `backup.py`; runner run_script | Mask URLs in every error and log (reuse `alerts._mask`, made public). Redact env values and answers for readers. Ignore `created_by` from clients. Restore marks imported history `restored_from`. Make the result key server-chosen. | S | `t1_webhook_leak.py` inverted; a reader's `/api/alerts` has no path segment after the host. | Partly: S-2, S-5, S-14 fixed (1.12.0); S-13 reader redaction open |
| B11 | **A station edit (mask or altitude) leaves activities on the old AOS/LOS.** Mask 0°→25°: AOS +227 s, LOS −225 s, same contact id. The activity starts 227 s before the antenna can track, and approvals close 195 s *after* the real LOS. | C-1 | `contacts.py:578-596`, `:1427` | Refresh the contact reference whenever aos, los, tca, duration or max_elevation differ. Mark it `needs_review` when the anchor moves more than `retime_tolerance`. Re-check `_rule_matches` and the approval cutoff. | S | A pytest edits a station mask: activity AOS/LOS match the new prediction, and it is `needs_review`. | Fixed in 1.12.0 |
| B12 | **A Script Runner poll error orphans the script.** When `script_state` raises (502 or timeout), the run fails, but the script is never stopped and isn't in `detached_scripts`, so STOP ALL can't reach it. | E-8 | `runner.py:912-923` | Retry up to N consecutive failures; `try/finally` calls `_stop_script` unless the script finished. | S | `test_script_is_stopped_when_polling_fails`. | Fixed in 1.12.0 |
| B13 | **The editor silently changes what a flow does.** (a) Deleting a Switch case re-routes the next case to the deleted case's target (handles are positional `case_<index>`). (b) The starting-variables editor corrupts values on every keystroke: `1.5` → `15`, `-3` → `''-'3'`, `'hi'` → `'''''h'i''`, because a reactive Proxy never equals `lastEmitted`. A row typed value-first loses its value; this also affects the Set variable *More* and Run script *Environment* fields. | U-1, U-2 | `nodeTypes.js:194`; `flowautomator.vue:2400-2411`; `NodeInspector.vue:349-355, 502-505`; `KeyValueEditor.vue:58-69, 89-98` | (a) On list removal, delete the `case_<index>` edge and renumber higher cases; later, stable case ids with a format bump and migration (D26). (b) Compare `toRaw()`; never rebuild rows while focused; keep empty-key rows locally; parse only on emit. | S | Browser test: delete the middle of three wired cases, and the third keeps its target. `pressSequentially` `'hello'`, `1.5`, `-3` saves `{name:"hello", n:1.5, m:-3}`. | Fixed in 1.12.0 (browser-tested) |
| B14 | **The Armed switch can show the wrong, unsafe state.** It's never refreshed from the engine, and a failed **disarm** turns it off and says "Not armed" while the flow is still armed. | U-3 | `flowautomator.vue:1870, 1905-1919, 2086-2119` | On a failed disarm keep the old value and raise a serious toast. `/status` returns `armed_ids`; `pollStatus` syncs the switch and the selector chips. | S | Disarm via API → switch updates within 2 s. `/disarm` → 500 → stays Armed plus toast. | Fixed in 1.12.0 (browser-tested) |
| B15 | **Rule activities can be lost or run outside their window.** (a) Saving a higher-priority rule deletes every lower-priority rule activity and re-creates only those > 10 s away, with new ids and fresh snapshots (bypassing D11): an activity due in 5 s vanished with no alert. (b) Rules skip the approval-cutoff check, so crawl/walk rule activities can start after approvals have closed and fail every pass. (c) A GEO activity is cancelled at the next recompute on its own day ("contact moved +86400s"). | C-8, R-17, C-4, R-4 | `contacts.py:559-606, 905-962, 1015-1081, 1542-1571`; `RULE_LEAD_SECONDS` | (a) Re-plan by difference: remove only activities actually pre-empted, never anything starting soon, keep snapshots, and audit each pre-emption. (b) Validate in `save_rule`, and skip a contact in `_rule_matches` when start ≥ LOS − approval margin and the template asks. (c) Floor the window start for continuous satellites to 00:00 UTC. | M | After an unrelated rule save, ids and snapshots are unchanged and the 5 s activity runs; `save_rule(anchor=los, offset=-10, mode=crawl)` → 400; `test_r_geo.py` inverted. | Fixed in 1.12.0 |
| B16 | **Bad TLEs get through the safeguards.** (a) A newer TLE that can't propagate gives NaN distance, `NaN > 100` is false, and it is applied, wiping contacts. (b) A future-epoch TLE is held, and once accepted every real update is skipped as "not newer" (CON-18). (c) Accepting a held TLE after a newer paste regresses the epoch. | C-7, C-6, C-12 | `tle_inputs.py:84-88, 250-279`; `contacts.py:463-466` | Check the SGP4 error and `math.isfinite`, and hold with a reason. Reject automated TLEs more than 1 day in the future, and ignore a future current epoch in the "newer" check. Clear the held TLE when a newer one is applied; refuse Accept if held ≤ current. | S | All three repros in `eval_contacts/test_eval*.py` inverted. | Fixed in 1.12.0 |
| B17 | **Internet TLE fetching can get the host banned.** Fetch times are in memory only, so every restart (or crash loop) re-fetches everything; Fetch now clears a 403 back-off; a mid-batch failure discards the whole batch, which is re-fetched after 10 min; there are no failure alerts. CelesTrak blocks exactly this pattern, and Space-Track can suspend the account. | C-10, R-12, DOC-9, S-15 | `tle_sources.py:128-146, 213-221, 295-306, 353-358` | Persist per-catalog fetch times and back-off (format-stamped, D26). Record each success as it happens. Fetch now respects 403/429. Back off on secrets errors. Raise a `tle_source_failing` alert. | S–M | `test_rate_limit_survives_restart`; 5 × Fetch now after a 403 sends 1 request. | Fixed in 1.12.0 |
| B18 | **The planner blocks the scheduler at realistic scale.** `_apply_rules` grows as rules × contacts × taken passes, and runs under the planner lock after every satellite merge and every 60 s on the tick thread. Measured idle cost: 0.5 ms (1×1), 244 ms (10×5), 4.4 s (20×10), **64.5 s (50×20)**. At 10×5 one tick took 12.6 s during a recompute. At 50×20 one satellite's merge held the lock for 60 s, so activity starts are late by up to a minute and passes can be missed. `save_rule` took 60 s. | C-11, P-1, CON-14 | `contacts.py:262-306, 1019-1081`; `tick` | Index taken passes by (rule, satellite, station) with sorted TCAs; cache `_check_template`; apply rules once after the prediction queue drains (this also fixes the priority inversion C-2); run them off the tick thread, and launch due activities without waiting on the planner lock. | M | At 20×10, `_apply_rules` with nothing new < 50 ms, recompute < 15 s, and tick latency < 100 ms throughout. | Fixed in 1.12.0: 20×10 idle 2,152 → 21 ms, recompute 175 → 21 s (20 s is SGP4, off the lock) |

### 2.2 Correctness and robustness (next)

| # | Issue | IDs | Where | Fix | Effort | Status |
|---|---|---|---|---|---|---|
| B19 | Rule priority inverts on batch prediction (window roll, restart, recompute): low wins 5/5 overlaps that high won at save. | C-2 (CON-6) | `contacts.py:262-306` | Apply rules once after the queue drains (with B18). | S | Fixed in 1.12.0 |
| B20 | A GEO day contact counts as using the antenna for 24 h: one 10-min GEO activity → ISS rule schedules 0/6 that day; two GEO satellites → permanent conflict. | C-3 (CON-5, CON-19) | `contacts.py:640-666` | Occupancy from each activity's own window (start → start + duration, or LOS); continuous contacts need a duration. | M | Fixed in 1.13.0 |
| B21 | A hand-edited (detached) rule activity is cancelled and replaced when its pass moves (against D23). | C-9, DOC-4 (Q6) | `contacts.py:597-603` | Treat detached like manual: needs_review plus a suggested contact. | S | Fixed in 1.13.0 |
| B22 | Template `pattern` regex runs under the planner lock on every merge and tick (ReDoS). | C-13 | `templates.py:165`; `contacts.py:1046` | Bound regex length, reject nested quantifiers, check once at rule save. | S | Fixed in 1.13.0 |
| B23 | A buffered write brings a deleted record back; deletes aren't buffered. | R-11 | `store.py:147-215`; `contacts.py:496, 552, 892, 1045, 1245` | Put deletes in the same ordered buffer, or drop pending writes for that key. | S | Fixed in 1.12.0 |
| B24 | Migrations never upgrade flows stored inside activities and their `subflow_snapshots` (which are what actually run). | R-14 | `migrations.py:38-43` | Upgrade `activities.flow` and each snapshot too. | S | Fixed in 1.13.0 |
| B25 | Any read-only user can build an unbounded backup (every version, and all history with `history=1`); concurrent requests can OOM. | R-15, DOC-11 | `backup.py:42-75`; `http_api.py:130-132` | Require write, one backup at a time, stream it out. | S | Fixed in 1.13.0 |
| B26 | Two engines during an upgrade: the new one fails the old one's live runs and stops their scripts. There's no leader lock. | R-16, P-9 | `engine.py:208-264` | Owner plus heartbeat per active run; recover only stale ones. | M | Fixed in 1.13.0 |
| B27 | Planner load failure at startup is never retried, so there's no schedule until a restart. | R-28 | `engine.py:97-100` | Retry in the background and alert until it works. | S | Fixed in 1.13.0 |
| B28 | Startup with interrupted runs blocks the HTTP server: 4 runs and 8 scripts with a 5 s stop each = 40 s before the API answers. | P-7 | `flow_engine.py` `run()`; `engine.py:219-250` | Start HTTP first; stop scripts in parallel with timeouts. | S | Fixed in 1.12.0: scripts stopped in parallel |
| B29 | Run finish: removed from `runs` before history is written ("Run not found" window), and the active record is deleted first, so a crash in between loses the run. | E-12 | `engine.py:722-736` | Write history, then delete the active record, then remove from memory. | S | Fixed in 1.12.0 |
| B30 | A COSMOS API blip quarantines every telemetry ref for 30 s (triggers go blind). | R-23 | `triggers.py:166-171` | If every ref fails, treat it as an outage and don't quarantine. | S | Fixed in 1.13.0 |
| B31 | Cron: `*/2` day-of-month is treated as restricted (OR with weekday; cron uses AND); a clock step back re-fires a minute; a tick over 60 s skips minutes silently. | E-14 | `cron.py:323-334`; `triggers.py:206-219` | `startswith("*")` means unrestricted; fire only when minute > last; log skipped minutes. | S | Fixed in 1.13.0 |
| B32 | Non-string node id, type or edge source returns 500. | R-21 | `graph.py` `_shape_problem` | Type-check. | S | Fixed in 1.13.0 |
| B33 | Webhook check is open to DNS rebinding; an unresolvable host is accepted at save; AWS IPv6 and Alibaba metadata addresses are allowed; `/test` can map internal ports; `base_url` can point anywhere. | R-24, S-12, S-17 | `alerts.py:375-391` | Connect to the address that was checked; block `fd00:ec2::254` and `100.100.100.200`; rate-limit `/test`; validate `base_url`. | M | Fixed in 1.13.0, except an unresolvable host is still accepted at save (a backup restored elsewhere must load); delivery requires it to resolve |
| B34 | Stash take is still racy (get, get, delete). | R-25 (CON-15) | `cosmos_backend.py:115-126` | Atomic `GETDEL`, or a Lua script. | S | Fixed in 1.13.0 |
| B35 | TLE responses are read in full before the 5 MB check; CelesTrak requests go back to back. | C-17, R-20, S-8 | `tle_sources.py:98-105, 128` | Stream with a byte cap; a short delay between requests. | S | Fixed in 1.13.0 |
| B36 | "Run vN" asks up to 3 times, and the Run dialog shows the draft's parameters and mode. | U-4 | `flowautomator.vue:1921-2001` | `ensureSaved` returns `{ok, version}`; build the dialog from vN. | S | Fixed in 1.13.0 |
| B37 | Ask answer buttons skip the 750 ms guard; the answer field steals focus; an existing card sliding under the cursor isn't guarded. | U-5, R-26 | `ApprovalPanel.vue:89-133, 255-270` | Guard every answer; `@keydown.enter`; autofocus only when nothing else has focus. | S | Fixed in 1.13.0 |
| B38 | A hung engine shows online for up to 60 s, polls pile up (and may queue STOP ALL behind them), and stale cards stay clickable. | U-6 (UX-10) | `flowautomator.vue:1452, 2086-2156`; `ContactsView.vue:1480-1497`; `flowApi.js:29` | One poll in flight; 4 s timeout; "not answering" after 2 misses; gray out cards; slow polling when hidden. | S–M | Fixed in 1.13.0 |
| B39 | Unsaved edits are lost silently when you navigate to another COSMOS tool. | U-7 | `flowautomator.vue:1462, 1883-1888` | `single-spa:before-routing-event` plus `cancelNavigation()`. | S | Fixed in 1.13.0 |
| B40 | Out-of-order responses overwrite newer ones (validate, history diff, run search); the picker caret is shared across nodes. | U-17, U-18 | `flowautomator.vue:2208-2218`; `HistoryDialog.vue`; `RunHistory.vue`; `NodeInspector.vue:395, 473-491` | Sequence numbers; key carets by node id. | S | Fixed in 1.13.0 |
| B41 | Needs-review alerts aren't batched; the jump threshold doesn't scale with TLE age, so a feed outage re-holds and re-alerts. | C-15, C-14 | `contacts.py:626-631`; `tle_inputs.py:262` | Batch like `_alert_missed`; scale the threshold with age. | S | Fixed in 1.13.0 |
| B42 | Contacts UI doesn't mark partial or GEO contacts, Schedule stays enabled on partial ones (the API rejects them), and LOS shows no date. | C-16 | `ContactsView.vue:569-610` | A chip, disable Schedule, show the date. | S | Fixed in 1.13.0 |

### 2.3 Performance (measured; see section 10)

| # | Issue | IDs | Fix | Effort | Done when | Status |
|---|---|---|---|---|---|---|
| B43 | `/schedule` is huge because each activity carries its full flow snapshot (7.2 KB each): 1-day view 1.3 MB at 10×5, 4.6 MB at 20×10, 12.7 MB / 7.9 s at 50×20; full window 26 MB / 42 s. | P-2, CON-14 | List view returns slim activities (no `flow`/`subflow_snapshots`); details on demand; `from`/`to` params; ETag or `since`. | M | 1-day view at 20×10 < 300 KB and < 200 ms. | Fixed in 1.14.0 (D30; measured on the soak: 1-day view 34 ms, 1.2 MB / 123 KB gzipped at 16×13) |
| B44 | Run history doesn't scale: `list_runs` across flows 1.4 s (20 flows × 200 runs × 100 steps) and 18.4 s at 1,000 steps; a missing `get_run` scans everything (18 s); Valkey 919 MB at 1,000 steps. The UI calls it every second per client. | P-3, ENG-9, UX-10 | Store run summaries separately from steps (a hash and a sorted index), a run-id → flow index, steps in their own capped list, `GET /runs/<id>?since=<seq>`. | M | `list_runs` all flows < 50 ms at 20×200×1,000. | Fixed in 1.16.0 |
| B45 | Engine `/status` and the tick suffer under several clients: at 20×10 with 5 clients, `/status` p50 2.2 s and tick p50 3.9 s (triggers and scheduling delayed). `planner_status` takes 234 ms (20×10) to 755 ms (50×20) under the lock. | P-5 | Cache `planner_status` (rebuild on change); keep the planner out of `/status`; B38 cuts the polling. | S–M | 5 clients at 20×10: `/status` p95 < 100 ms, tick p95 < 50 ms. | Fixed in 1.14.0 (D30; /status 3 ms; tick ~25 ms p50, 107 ms max with 5 clients polling flat out) |
| B46 | Versions: a 2,000-node flow with 200 versions is a 92 MB key; listing versions takes 2.2 s, and the oldest version or a diff takes 2.4 s. History of deleted flows is never purged. | P-4, ENG-19 | Version metadata in its own hash; bodies per key; purge history with the flow (after a grace period). | M | `list_versions` < 50 ms. | Fixed in 1.16.0 |
| B47 | Run log UI refetches up to 1,000 steps every 0.5 s with index keys and a linear label lookup. | U-12 (UX-9) | Id → label map, key by sequence number, `since=`, virtual scroll, follow-tail. | M | < 20 ms scripting per poll at 1,000 steps. | Fixed in 1.16.0 |

### 2.4 Engineering and docs (do alongside)

| # | Issue | IDs | Fix | Effort | Status |
|---|---|---|---|---|---|
| B48 | **No CI** (TOOL-1). | M-1, M-14 | `flowautomator.yml`, path-filtered: pytest + coverage ≥ 85 on 3.11–3.13, ruff check and format, pnpm frozen install + lint `--max-warnings 0` + build + source-hash check, gem build, CHANGELOG gate. | S | Workflow added in 1.16.0 (`.github/workflows/flowautomator.yml`); not run yet: Actions is off on the fork |
| B49 | **Contract tests** for the node registries and backend parity. | M-3 | `test_contracts.py`: `nodeTypes.js` keys and handles = `NODE_TYPES` = `EXECUTORS`; `CosmosBackend` vs `FakeBackend` method signatures. | S | Fixed in 1.16.0 |
| B50 | **API tests:** `http_api` 76%, `cosmos_backend` 39%; the PUT every save uses is untested. | M-4, TOOL-8 | Table-driven `test_http_api.py` over all 62 routes (read/write status, happy shape, 4xx); a stubbed `openc3.api` for `CosmosBackend`. | M | Open |
| B51 | **Frontend unit tests** (TOOL-2). | M-2 | vitest + @vue/test-utils: `nodeTypes`, `flowApi.errorInfo`, `format.js`, then FlowNode, ParamForm, ApprovalPanel, NodeInspector, KeyValueEditor (B13). | M | Open |
| B52 | **Browser tests** miss every new safety feature and both editor P1s; brittle sleeps; boilerplate copied 9 times; `run.sh` picks the installed lib lexically (1.9.0 sorts after 1.11.0); screenshots predate 1.8. | U-14, M-6 (TOOL-3, UX-20) | `@playwright/test`, shared fixtures, `expect.poll`, `SCOPE` env, `sort -V`, a lockfile, `@slow` tags; add the "done when" checks from B13, B14, B36–B38 and computed danger colours. | M | Open |
| B53 | Docs say things the code doesn't do: the mode floor (until B5), upgrades restart the engine (add a checklist), restore matches flows by id only, a "Run…" button, the Allow-hazardous hint, alert event list, `system` (not `tlm`) permission, backup contents and safe wiping. | DOC-1–7, DOC-11, DOC-19 | Fix the text (see section 7 for exact wording). | S | Open |
| B54 | Missing references: expressions, API (with permission per route), troubleshooting, limits, first-flow tutorial; README serves three audiences in 658 lines. | DOC-8, 10, 12, 17, 18, 25 | Follow the doc map in section 7: EXPRESSIONS.md, API.md (with a route-coverage test), OPERATOR-GUIDE, ADMIN-GUIDE, SECURITY.md, a short README. | L | Open |
| B55 | Stale developer docs; CLAUDE.md points at evaluations as "known issues" with no status. | DOC-14–16, DOC-20–22, M-15 | Fix counts and maps; point CLAUDE.md at section 2 of this document and its Status column; one authoritative flow JSON description. | S | Open |
| B56 | Leaky boundaries (`engine.flows` read directly from 9 places, private cross-imports, runner↔engine cycle); tests organised by evaluation round; high complexity unlinted. | M-8, M-9, M-10 | Accessors on `FlowEngine`; public helpers; move `run_path`; per-module test files; C901 ≤ 20 in a plugin `ruff.toml`. | M | Open |
| B57 | God files grew (TOOL-5). | M-5 | Refactor plan in section 8, one step per PR, after B48–B51. | L | Open |
| B58 | Metadata and release hygiene: gemspec placeholders and minimum COSMOS 6.0.0, `LICENSE.md` `<COPYRIGHT HOLDER>`, `package.json` 1.0.0, no `packageManager`, 59 `.gem` files, no tags, `sleep 20` in setup. | M-12, M-13, DOC-13, DOC-26 (TOOL-6/7) | Real metadata, minimum 7.4.1, one VERSION source, `rake dev` and `rake clean_gems`, tags, poll `/status`. | S–M | Open |
| B59 | Duplicated format/status helpers (same status, different colours); unused deps; 1.26 MB main chunk. | M-7, M-16, S-16 | `format.js` and `status.js`; remove 5 unused deps; lazy-load ContactsView. | S | Open |
| B60 | Lint config depends on the parent repo; 526 lint problems in scripts/demo/tests; 52 broad excepts; slow real-time tests. | M-11, M-17, M-18 | Plugin `ruff.toml`; `eslint .`; BLE001 with reasons; injectable poll interval. | S–M | Open |

### 2.5 Features (the operator-workflow roadmap, section 11)

| # | Feature | IDs | Effort | Status |
|---|---|---|---|---|
| B61 | `on_failure`/`on_stop` cleanup (safing) sub-flow, also run at LOS stop (and STOP ALL, Q7). | G-1 | M | Open |
| B62 | Retry / Skip / Abort on a failed step in crawl/walk; per-node `retries`; run-level Pause/Resume. | G-2 (GAP-1, GAP-3) | M | Open |
| B63 | Verify on Send command (`verify` expression + timeout, `not_verified` outcome). | G-4 | S | Open |
| B64 | Enterprise critical commands and command queues: pass `queue=False`, wait on `critical_cmd_status`, reject the pending command on Stop. | G-15 | S | Open |
| B65 | Lights-out pre-authorization of exact rendered commands when scheduling (telemetry-dependent commands still ask). | G-3 | M | Open |
| B66 | Limits triggers from limits events, with group / "any RED" scope and persistence. | G-7 | S–M | Open |
| B67 | As-run record: HTML/CSV/JSON report, uncapped JSONL archive in the bucket at run end, campaign labels. | G-5 (GAP-21) | M | Open |
| B68 | COSMOS operations node (interfaces, routers, limits sets, metadata, Autonomic), allow-listed. | G-6 (GAP-8, GAP-10) | S–M | Open |
| B69 | Undo/redo, copy/paste, Ctrl+S, Delete-only key. | U-8 (UX-3) | M | Open |
| B70 | Show hazard on the canvas (HAZ/RAW badges, run-mode outline); deep links open in View; View is read-only. | U-9, U-10 (UX-6, UX-8) | S–M | Open |
| B71 | Dry run a draft without saving (`POST /api/dry_run`). | U-11 | M | Open |
| B72 | Accessibility: keyboard palette and rows, aria-labels, contrast, storage banner. | U-13 (UX-12) | M | Open |
| B73 | Smaller UX items: toast burst on load, `window.confirm` ×10, error reporting, command builder replacing templates. | U-15, U-16, U-19, U-21 | S–M | Open |
| B74 | The rest of the G- list: contact awareness in triggered runs, Stop by target and locks, a Check node, step numbers/instructions, per-target triggers, shift summary with notes, Script Runner link, Calendar, `tlm_age`, scheduling outputs, escalation, sub-flow inputs/outputs. | G-8 to G-21 | L | Open |

---

## 3. Engine (grade C+)

**Strengths:**

- Lock order (planner → engine) is correct. `status()` and `on_run_finished`
  release the engine lock before calling the planner. No deadlock path found.
- Cancellation is uniform (100 ms waits; a cancelled parent cancels children).
- ENG-3, ENG-13, ENG-14, ENG-15, ENG-16 are fixed. The `approved_command`
  check is correct whenever a preview rendered.
- Triggers: batched reads, per-item back-off (ENG-11), edge semantics, and
  `trigger()` re-checks `armed` under the lock, so a tick can't race STOP ALL.
- Crash recovery (D25) and the outage buffer's ordering (D28) work.

**Findings:**

| ID | Sev | Finding | Backlog |
|---|---|---|---|
| E-1 | P0 | STOP ALL during a Valkey outage stops nothing and raises. | → B3 |
| E-2 | P0 | Nodes after a Join still run twice (logic and race). | → B2 |
| E-3 | P1 | Walk/crawl send hazardous with no approval when the node has Allow hazardous; the check fails open and disagrees with COSMOS. | → B6 |
| E-4 | P1 | Loops in parallel branches reset each other. | → B1 |
| E-5 | P1 | Container literals bypass D22 → GIL freeze, OOM. | → B9 |
| E-6 | P1 | Activities and rules run below the mode floor. | → B5 |
| E-7 | P1 | Dry run off and sub-flow edits don't disarm. | → B4 |
| E-8 | P1 | A Script Runner poll error orphans the script. | → B12 |
| E-9 | P1 | A failed preview → a command nobody saw is sent. | → B6 |
| E-10 | P2 | Shutdown starts the next queued run. | → B3 |
| E-11 | P2 | Valkey calls under `engine.lock`; memory and Valkey disagree after a failed write (a failed disarm re-arms after restart). | → B3 |
| E-12 | P3 | Run-finish ordering windows. | → B29 |
| E-13 | P3 | Fire-and-forget script tracking is lossy. | → B3 |
| E-14 | P3 | Cron edge cases. | → B31 |
| E-15 | P3 | Dead code (`Graph.triggers`, the `parallel` executor entry), O(N·E) `join_owners`, a `flush(wait=False)` race (`store.py:180-181`). | → B2; delete dead code; re-check `_pending` after the flush lock |

**Decisions challenged:** D28 (writes that reduce activity must never fail →
Q5), D6 (floor in `FlowRun`, only approvals acknowledge hazards → Q1, Q3), D9
(let COSMOS detect hazards → Q4).

**Not checked:** real Valkey and Script Runner timings, multi-scope.

---

## 4. UI/UX (grade C+)

**Strengths:**

- Danger colours now beat tool-base's `!important` (`ApprovalPanel.vue:362-380`).
  Go is green, hazardous red, Abort and Stop amber.
- Hazardous approval asks twice with Cancel focused, and re-checks the
  approval still exists (`:186-221, 286`).
- STOP ALL confirms, shows failures with Retry, and checks within about 5 s
  that nothing is still armed or running.
- Run and Arm ask before saving; there's a discard guard, a 409 conflict
  dialog, and Save As Copy switches only after success.
- Clear status everywhere: the engine chip, approval cards, node icon plus
  ring (not colour alone), All running, and a Problems tab.

**Findings:**

| ID | Sev | Finding | Backlog |
|---|---|---|---|
| U-1 | P1 | Deleting a Switch case re-routes another case (repro'd). | → B13 |
| U-2 | P1 | The variables editor corrupts values as you type (repro'd). | → B13 |
| U-3 | P1 | The Armed switch is stale; a failed disarm shows Disarmed. | → B14 |
| U-4 | P2 | "Run vN" asks 3 times; the dialog uses the draft. | → B36 |
| U-5 | P2 | Ask answers skip the click guard; focus stealing. | → B37 |
| U-6 | P2 | A hung engine shows online, polls pile up, stale cards are clickable. | → B38 |
| U-7 | P2 | Unsaved edits lost on in-app navigation (suspected). | → B39 |
| U-8 | P2 | No undo/copy/paste; Backspace deletes (UX-3). | → B69 |
| U-9 | P2 | Hazard is invisible on the canvas (UX-6). | → B70 |
| U-10 | P2 | Deep links open in Edit; View isn't read-only (UX-8). | → B70 |
| U-11 | P2 | Dry run of a draft forces a new version. | → B71 |
| U-12 | P2 | The run log doesn't scale (UX-9). | → B47 |
| U-13 | P2 | Keyboard and screen-reader gaps (UX-12). | → B72 |
| U-14 | P2 | Browser tests miss the new safety features; stale shots. | → B52 |
| U-15 | P3 | Toast burst on load; `seenApprovals` grows (UXBUG-7). | → B73 |
| U-16 | P3 | `window.confirm` in 10 places (UX-11). | → B73 |
| U-17 | P3 | Out-of-order responses. | → B40 |
| U-18 | P3 | Picker caret shared across nodes. | → B40 |
| U-19 | P3 | Inconsistent error reporting (double toast, "no runs" on error, HH:MM-only times, Fetch now saves even if the dialog is cancelled). | → B73 |
| U-20 | P3 | Hazard detected by substring match on the reason. | → B6 |
| U-21 | P3 | The command builder silently replaces a templated command. | → B73 |

**Old items confirmed fixed:** UX-1, UX-2, UX-4, UX-5, UX-7, UX-13, UX-19,
UXBUG-1/2/3/4/6, and the revoke part of UXBUG-7.

**Still open, not in the backlog above:** UX-14 (no "Show step", cards cover
the canvas at 1100 px), UX-16 (white on green chips), UX-17 (minimap smear,
no auto-layout or find).

**Top 5 by operator value:** B13, then B14 + B38, then B69, then B70, then
B36 + B71.

**Not checked:** a live browser (no login), the light theme, screen readers.

---

## 5. Contacts and scheduling (grade B−)

**Strengths:**

- **Accuracy, re-checked against skyfield** with a live CelesTrak TLE over
  3 days and four stations (antimeridian, 4 km altitude, 60°N with a 0° mask,
  −3° mask). Pass counts identical; worst AOS 0.16 s, LOS 0.28 s, TCA 0.11 s,
  max elevation 0.006°. A station at 85°N correctly gets no ISS passes.
  Unix UTC throughout, so leap seconds and DST don't matter.
- **Confirmed fixed:** CON-1/13, CON-3, CON-7, CON-8, CON-11, CON-12,
  CON-17, and D11 snapshots.
- D29's design (ingest safeguards, secrets read when needed, no redirects,
  batched Space-Track queries) is sound apart from B8 and B17.
- Run safety: `_launch` re-checks pause under the lock, approvals close at
  LOS minus the margin, and review-needed activities become missed.

**Findings:**

| ID | Sev | Finding | Backlog |
|---|---|---|---|
| C-1 | P0 | A station edit leaves activities on the old AOS/LOS. | → B11 |
| C-2 | P1 | Rule priority inverts on batch prediction (CON-6). | → B19 |
| C-3 | P1 | A GEO day contact blocks the station for 24 h (CON-5). | → B20 |
| C-4 | P1 | Rules skip the approval-cutoff check (CON-10). | → B15 |
| C-5 | P1 (P0 Ent.) | Any write user can send any secret to any host. | → B8 |
| C-6 | P1 | A future-epoch TLE blocks every later update (CON-18). | → B16 |
| C-7 | P1 | The NaN jump check lets a bad TLE through. | → B16 |
| C-8 | P1 | Saving a higher-priority rule deletes imminent activities. | → B15 |
| C-9 | P2 | Hand-edited rule activities are replaced. | → B21 |
| C-10 | P2 | TLE fetch rate limits don't hold. | → B17 |
| C-11 | P2 | `_apply_rules` cost under the lock. | → B18 |
| C-12 | P2 | Accepting a held TLE regresses the epoch. | → B16 |
| C-13 | P2 | Template regex ReDoS under the lock. | → B22 |
| C-14 | P3 | The jump threshold doesn't scale with TLE age. | → B41 |
| C-15 | P3 | Needs-review alerts not batched. | → B41 |
| C-16 | P3 | UI doesn't mark partial/GEO contacts. | → B42 |
| C-17 | P3 | Fetchers: full read before the size cap, no spacing, no Alpha-5. | → B35, B8 |

**Still open:** CON-18 (a stale TLE never stops an unattended run), CON-19
(no refraction).

**Decisions challenged:** D12/D23 (cancelled passes come back silently → Q6),
D24 (GEO day windows as occupancy break D14 → B20), D29 (pinning the host
matters more than the redirect ban → Q8).

**Not checked:** live Space-Track, Enterprise secrets, Molniya/HEO passes
with two elevation peaks (the TCA search assumes one).

---

## 6. Security (grade C+)

**Threat model:**

| Actor | Can do today |
|---|---|
| Anonymous | Only `/health`. But it can fill all 64 request slots with a fake token (blocking STOP ALL), and while the cmd-tlm-api is down any fake token can press STOP ALL and choose the name in the audit log (S-9). |
| Read-only user | Read every flow, run and a full backup with history (B25); recover webhook URL secrets (B10); see script env values and Ask answers. |
| Write (`script_run`) user | Everything in section 2.1: exfiltrate secrets (B8), hide sub-flow content (B7), forge attribution (B10), OOM the container (B9), keep a relaxed flow armed (B4), run a crawl template in run mode (B5). Plus SEC-3: no per-user target permissions; can approve their own hazardous step. |
| Admin | No route needs more than write. |
| Malicious backup file | Retarget Space-Track (B8), forge history (B10), inject hidden sub-flows (B7). Restored flows are still disarmed and the scheduler paused (D27 holds). |
| Malicious TLE mirror / MITM (http allowed) | Gets the Space-Track login (B8); exhausts memory with a large response (B35). |

**Strengths:** every route makes a COSMOS token check; nothing escapes the
expression sandbox; D7 stopped every injection tried; the mode is a floor for
`trigger()` runs; the approved command is the one sent when a preview
rendered; `pnpm audit --prod` is clean; no `v-html`, `innerHTML` or `eval`
in `src/`.

**Findings:**

| ID | Sev | Finding | Backlog |
|---|---|---|---|
| S-1 | P0 Ent. / P1 Core | Secret exfiltration via the Space-Track settings (repro'd). | → B8 |
| S-2 | P0 | Webhook secrets reach read-only users (repro'd). | → B10 |
| S-3 | P0 | Dry-run OOM kill (repro'd, exit 137). | → B9 |
| S-4 | P1 | Hidden sub-flow snapshots (repro'd). | → B7 |
| S-5 | P1 | Forged attribution (repro'd). | → B10 |
| S-6 | P1 | Unsafe edits keep the flow armed (repro'd). | → B4 |
| S-7 | P1 Ent. | SEC-3 still open: no per-user RBAC. | Phase 3 |
| S-8 | P2 | TLE response read before the size check; URL not re-checked at fetch. | → B35, B8 |
| S-9 | P2 | STOP ALL with a fake token while the API is down. | → B3 (record "unverified" in the audit; rate-limit) |
| S-10 | P2 | Body read before auth (slot exhaustion). | → B3 |
| S-11 | P2 | Audit gaps (denied attempts, hazardous sends, trigger starts; SEC-10); one expression holds the CPU 4.6 s. | → B9; add audit events |
| S-12 | P2 | Cloud metadata addresses allowed; port mapping via webhook test. | → B33 |
| S-13 | P2 | Script env, Ask answers and stash results visible to readers. | → B10 |
| S-14 | P3 | `FLOW_RESULT_KEY` override reads and deletes any stash key. | → B10 |
| S-15 | P3 | A secrets error stops all TLE intake, retried every second. | → B17 |
| S-16 | P3 | Unused npm deps; minimatch dev advisory; wrong permission in a docstring. | → B59, B53 |
| S-17 | P3 | `base_url` can point anywhere. | → B33 |

**Confirmed fixed:** ENG-1/SEC-2, SEC-8 (for `trigger()`), SEC-1/4 (apart
from B9), SEC-5, SEC-7 (apart from S-2), SEC-9/11, SEC-12 (apart from S-14).

**Not checked:** Enterprise roles, Traefik's behaviour for slot exhaustion.

---

## 7. Documentation (grade B)

**Strengths:** plain, direct English throughout; the README tracks most of
1.8–1.11; DECISIONS.md is excellent (amendments dated); CLAUDE.md is a strong
agent entry point; the CHANGELOG is disciplined and gated; plugin variables,
all 26 node types and their outputs, contact variables, TLE safeguards,
Valkey outage behaviour and STOP ALL are documented accurately.

**Findings (exact fixes):**

| ID | Sev | Where | Wrong today | Change to | Backlog |
|---|---|---|---|---|---|
| DOC-1 | P1 | README.md:211-214, 507; D6 | "A flow's mode is the least cautious a real run can use." Activities bypass it. | Fix the code (B5); keep the sentence. | B5 |
| DOC-2 | P1 | README.md:33-44 | No warning that an upgrade restarts the engine. | Checklist: check All running and the timeline → Pause scheduler → Back Up Everything → upload as upgrade → wait for the green chip and prediction → resume. Say what fails if you skip it. | B53 |
| DOC-3 | P2 | README.md:51-52; CHANGELOG:72; D27 | Flows matched "by id, or by name". | "Flows are matched by id. Satellites, stations and rules by id, then by name. A same-named flow with a different id is added as a second flow." | B53 |
| DOC-4 | P2 | README.md:519-522, 536-538 | Moved pass → rule activities "need review". | "One-off activities need review. A rule's activities are cancelled and re-planned with the rule's settings" (until B21). | B53 |
| DOC-5 | P2 | README.md:163-164, 211-212 | A "Run…" button; a "Mode" label. | "click **Dry run**, turn *Dry run* off, and pick the mode"; "**Flow mode**". | B53 |
| DOC-6 | P2 | nodeTypes.js:370 | "Hazardous commands are blocked unless this is on." | "In run mode a hazardous command fails unless this is on. In crawl and walk the operator's approval is enough. Turning it on disarms an armed flow." | B53 |
| DOC-7 | P2 | README.md:366-382; D13; alerts.py:9 | Misses `tle_held`, `storage_unavailable`, `storage_recovered`, `storage_format`; lists a nonexistent "stuck" event; *Include dry runs* undocumented. | A table built from `EVENTS` (alerts.py:42-75). | B53 |
| DOC-8 | P2 | — | No expression reference; the editor has no function list (ARCHITECTURE:294 says it does). | `docs/EXPRESSIONS.md`, linked from a "?" on condition fields. | B54 |
| DOC-9 | P2 | README.md:421-428; D29 | "Never asks more often than the minimums." | Fix the code (B17), then document the cap and the alert. | B17 |
| DOC-10 | P2 | http_api.py:9-82 | Missing the rehearse route; `GET /api/runs` twice; missing fields; DELETE /rules wording. | `docs/API.md` plus a test that every `_routes()` entry is documented. | B54 |
| DOC-11 | P2 | README.md:557; cosmos_backend.py:221 | Reads need `tlm`. | "Reading needs COSMOS's `system` permission; changes need `script_run`; results are cached for 30 s; read access includes Back Up Everything" (until B25). | B53 |
| DOC-12 | P2 | — | No troubleshooting. | Table: exact message → cause → what to do, for every user-facing error in `runner.py` and `engine.py`. | B54 |
| DOC-13 | P2 | README.md:20-31; plugin.txt:9-10; gemspec:36 | No scope, COSMOS version or one-scope limit. | Requirements: Core 7.4.x tested, Enterprise untested, one scope, outbound HTTPS, a PyPI mirror for sgp4; `…gem DEFAULT`. | B58 |
| DOC-14 | P2 | DEVELOPMENT.md:73, 84-89, 258 | 167 tests; test map missing 6 files; internet TLE "not started". | Update. | B55 |
| DOC-15 | P2 | CLAUDE.md:28 | Points at the old evaluation as known issues; it has no status. | Point at section 2 of this file and its Status column. | B55 |
| DOC-16 | P2 | graph.py:1-18 | The "authoritative" flow JSON lacks a dozen keys. | One complete description. | B55 |
| DOC-17 | P2 | README.md | 658 lines for three audiences, no TOC. | Split per the doc map below. | B54 |
| DOC-18 | P2 | — | No first-flow tutorial; Save As Copy and Follow Live Runs undocumented. | Tutorial plus a menu reference. | B54 |
| DOC-19 | P2 | README.md:39, 46-61 | Backup coverage overstated; "delete those keys" while the engine runs. | List what is and isn't in a backup; "uninstall first, then delete `flow_automator__<SCOPE>__*`" with the command; a nightly curl example; the 25 MB limit. | B53 |
| DOC-20 | P3 | D7; ARCHITECTURE:249-252; runner.py:834-836; nodeTypes.js:377 | Command-safety rules miss `\ [ ]` and control chars. | Copy the README wording. | B55 |
| DOC-21 | P3 | ARCHITECTURE.md | Missing backup.py, migrations.py, new record fields. | Add them. | B55 |
| DOC-22 | P3 | ARCHITECTURE.md:288 | 3 of 8 expression caps. | Link D22. | B55 |
| DOC-23 | P3 | README.md:294, 331, 395, 454, 476 | Limits default states, Ask in dry run, skyfield figure, dialog rename, `poll_seconds`. | Fix each. | B53 |
| DOC-24 | P3 | nodeTypes.js:61, 697; NodeInspector.vue:315; ContactsView.vue:26, 729, 1449-1468 | Stale hints ("TLE inputs", Ask "0 = none"). | Reword. | B53 |
| DOC-25 | P3 | README.md:650-658 | Limits section misses 50 runs, 10 queued, 16 branches, 200 threads, 200 versions, `max_steps`, 5,000 buffered writes. | One limits table (plus the capacity limits in section 10). | B54 |
| DOC-26 | P3 | gemspec; plugin.txt; LICENSE.md; package.json | Placeholders. | Fill in. | B58 |
| DOC-27 | P3 | CHANGELOG 1.11.0 | No Docs group. | Add it. | B55 |
| DOC-28 | P3 | README.md:255-257, 366, 503; DEVELOPMENT.md:175 | Prose nits. | Rewrap and reword. | B55 |

**Proposed doc map:**

| Doc | Audience | Contents | Exists |
|---|---|---|---|
| README.md | Everyone | What it is, requirements, 3-line install, demo, TOC. ≤ 150 lines. | Overloaded |
| docs/OPERATOR-GUIDE.md | Operators | Tutorial; editor tour; arming; modes and approvals; dry runs; variables; history; Contacts; alerts; STOP ALL; troubleshooting. | In README |
| docs/NODES.md | Flow authors | Per node: outputs, fields and defaults, variables set, dry-run and approval behaviour, errors. Could be generated from `nodeTypes.js`. | Partly |
| docs/EXPRESSIONS.md | Flow authors | Syntax, operators, every function, the triplet rule, templates, command safety, limits. | No |
| docs/ADMIN-GUIDE.md | Admins | Requirements; install; upgrade checklist; uninstall and wipe; variables; scope; egress; secrets; permissions; backup; monitoring; Valkey outages; limits. | Scattered |
| docs/SECURITY.md | Admins, reviewers | The threat model in section 6, D5/D6/D7/D21/D22/D29, known gaps. | No |
| docs/API.md | Integrators | Route table with permission and body; examples; errors; limits. | Partial docstring |
| ARCHITECTURE / DECISIONS / DEVELOPMENT | Developers, agents | As now, fixed; DEVELOPMENT is the single home of build/test commands. | Yes |
| CHANGELOG.md | Everyone | Add an "Upgrade notes" line per release when defaults change. | Yes |

---

## 8. Maintainability (grade B−)

**Metrics (measured):**

| Metric | Value |
|---|---|
| pytest | 266 pass, 33–44 s, 0 flakes over 4 runs. Slowest: 7.8 s (`test_as_json_while_variables_change`) |
| Coverage (`lib/`) | 87%. `http_api` 76%, `cosmos_backend` 39%, `graph` 81%, `templates` 80%, `flow_engine.py` 0%; `backup` 93, `migrations` 91, `tle_sources` 93, `tle_inputs` 92 |
| Lint | ruff clean; eslint `src` clean; eslint over `scripts demo test/ui` 526 problems |
| Complexity | 29 functions over C901 10. Worst: `graph._validate_node_data` 43, `validate_flow` 26, `triggers._check` 25, `expressions._eval` 23 |
| Build | 6.2 s, source hash reproduces. `main` chunk 1.26 MB, `flowautomator` 520 KB |
| Size | Python 9,224 lines; Vue/JS 10,045; pytest 4,411; browser tests 2,049 |
| Largest files | `flowautomator.vue` 2,762 (was 2,421); `ContactsView.vue` 2,082; `contacts.py` 1,571 (one 1,386-line class); `runner.py` 1,123; `engine.py` 984; `http_api.py` 830 |
| Registry parity | 26 = 26 = 26, backend parity identical, neither guarded by a test |

**Strengths:** hermetic engine tests (FakeBackend, MemoryRedis, manual
`tick()`); excellent agent docs and recipes; versioned stored data; a working
release gate; every first-evaluation repro has a named regression test;
dependencies pinned with reasons.

**Findings:** M-1 → B48, M-2 → B51, M-3 → B49, M-4 → B50, M-5 → B57,
M-6 → B52, M-7 → B59, M-8 → B56, M-9 → B56, M-10 → B56, M-11 → B60,
M-12 → B58, M-13 → B58, M-14 → B48, M-15 → B55, M-16 → B59, M-17 → B60,
M-18 → B60.

**Old items:** TOOL-1, -2, -3, -4, -5 (worse), -6, -10, -11, -12 still
open; TOOL-7 partly fixed (CHANGELOG and gate added); TOOL-8 partly fixed
(87% overall); TOOL-9 partly fixed (route list fixed, other drift remains).

### Refactor plan (B57)

Do B48 (CI), B49 (contract test), B50 (API tests) and the B51 vitest
scaffold first, so every step lands with a safety net. Keep public names
stable (`FlowRun`, `EXECUTORS`, `ContactPlanner`). Update DEVELOPMENT.md's
recipes in the same PR as each step. One step per PR.

**`runner.py` → `runner.py` + `nodes/` (≈ 450 lines left):**

| Step | Module | Moves |
|---|---|---|
| R1 | `nodes/__init__.py` | builds `EXECUTORS` (runner re-exports it) |
| R1 | `nodes/flow_control.py` | trigger, if, switch, loop (+ `_reset_loops_since`, after B1), passthrough, set_var, subflow, end |
| R1 | `nodes/commanding.py` | `render_command` and its regexes, send_cmd, set_tlm, `_preview` |
| R1 | `nodes/scripts.py` | run_script, `_stop_script`, `SCRIPT_*` |
| R1 | `nodes/waits.py` | wait_time, `_wait_for`, wait_tlm, wait_check, read_packet, wait_packet |
| R1 | `nodes/contacts.py` | wait_contact, contact_check, `CONTACT_ANCHORS` |
| R1 | `nodes/interaction.py` | ask, `_check_answer`, notify, log |
| R2 | `run_approvals.py` | `ApprovalGate` (`_gate`, `_wait_for_approval`, `approve`, `pending_approvals`); best done together with B6 |
| R3 | `values.py` | `_json_safe`, `_float`, number regexes, `render_value` |

Per-node validators move next to each `nodes/*.py`, replacing the 43-branch
`_validate_node_data` (M-10).

**`contacts.py` → `planner/` (the `ContactPlanner` façade keeps the lock and
the public API):** P1 `assets.py` (satellites, stations, TLEs) → P2
`prediction.py` (queue, merge, stable ids, retiming, tombstones, GEO) → P3
`activities.py` (activities, capacity, template checks) → P4 `rules.py` →
P5 `scheduler.py` (tick, launch, finish, missed alerts, pause, rehearse).
Do B18/B19 while P2 and P4 are being moved.

**`flowautomator.vue` (≈ 600 lines left):** F1 `format.js`, `status.js` →
F2 `FlowToolbar.vue` (template `:11-413`) → F3 `dialogs/*` (StopAll, Run,
SaveConflict, SaveNote, Discard; `:657-991`) → F4 `useFlowDocument.js`
(`:1480-1752, 1883-1904`) → F5 `useRunMonitor.js` (`:2029-2207`; do B38 here)
→ F6 `useFlowCanvas.js` (`:2295-2470`; do B13a here) → F7 `useLayout.js` →
F8 `flowFiles.js` (`:1753-1882`).

**`ContactsView.vue` (≈ 300 lines left):** C1
`contacts/{Satellite,Station,Rule,Activity}Dialog.vue` (`:626-1263`) → C2
`AssetSidebar.vue` (`:10-213`) → C3 `PassTimeline.vue` (`:214-624`; do B42
here).

**`http_api.py`:** split by resource into `api/{flows,runs,contacts,admin}.py`,
only after B50 exists.

---

## 9. Review of the 1.8–1.11 fixes (grade C+)

About 3,500 changed lines that no one had reviewed independently.

| Old ID | Verdict | Notes |
|---|---|---|
| 2.1 #1 ENG-1/SEC-2 | Partial | TOCTOU closed; gaps in B6 and B3(c) |
| 2.1 #2 ENG-2 | Partial | The engine reviewer broke it two ways (B2); the fix reviewer's simpler shapes held |
| 2.1 #3 ENG-6 | Holds | |
| 2.1 #4 STOP ALL | Partial | Pause-first and the `_launch` re-check hold; see B3 |
| 2.1 #5 expressions | Holds for operators | Literals bypass it (B9) |
| 2.1 #6 fan-out | Holds | Sub-flow cycles bounded, not detected |
| 2.1 #7 SEC-8/ENG-18 | Partial | B4, B5, B7 |
| 2.1 #8 UX-1 | Untested in browser | Selectors look right |
| 2.1 #9 UX-2 | Mostly | B37 |
| 2.1 #10 CON-1/13 | Holds | |
| 2.1 #11 CON-4/GAP-14 | Partial | B7, B15 |
| 2.1 #12 SEC-7 | Holds | B10 (S-2), B33 |
| ENG-3, 10, 13, 14, 15, 17 | Holds | |
| ENG-7 | **Broken (regression)** | B1 |
| ENG-8 | Partial | By design (D25), plus B23, B26 |
| ENG-11 | Holds | B30 |
| CON-5 | **Broken** | B15(c), B20 |
| CON-6 | Partial | B19 |
| CON-10 | Partial (manual only) | B15(b) |
| CON-7, 8, 9, 11, 12, 17; SEC-12 | Holds | |
| CON-15 | Partial | B34 |
| SEC-9/11 | Partial | B32, B3 |
| UX-4, 7, 19, UXBUG-1..7 | Fixed per the UI reviewer (code); untested in browser | |
| A-1 migrations | Partial | B24 |
| A-2 backup | Partial | B7, B25 |
| A-3 outage | Partial | B23, B27, B3 |

R- findings: R-1 → B1, R-2 → B3, R-3 → B8, R-4 → B15, R-5 → B7, R-6 → B5,
R-7 → B6, R-8 → B3, R-9 → B6, R-10 → B4, R-11 → B23, R-12 → B17,
R-13 → B8, R-14 → B24, R-15 → B25, R-16 → B26, R-17 → B15, R-18 → B3,
R-19 → B9, R-20 → B35, R-21 → B32, R-22 → B3, R-23 → B30, R-24 → B33,
R-25 → B34, R-26 → B37, R-27 → B1, R-28 → B27.

---

## 10. Performance and reliability (grade C)

This reviewer hit a usage limit before writing its report. The numbers
below are its saved benchmark results, written up by the coordinator. All
were measured on Redis 7 in a private Docker network, with the plugin's
FakeBackend for COSMOS; COSMOS-like retry settings were used for the outage
tests. Scripts and raw JSON are in `eval_perf/`.

### Measurements

| Scenario | Size | Result | OK? (threshold) |
|---|---|---|---|
| Trigger tick | 10 / 50 / 200 / 500 armed flows (30–2,000 watched refs) | 1.0 / 4.7 / 28 / 62 ms; 0.4 / 1.9 / 11 / 25% of a core at 4 Hz | Yes up to 200 flows (< 50 ms) |
| Trigger tick, 503-node flows | 50 flows | 8.5 ms | Yes |
| Slow `get_tlm_values` | 200 ms / 1 s per call | tick period 0.24 s / 1.0 s | Acceptable; document it |
| Save a 2,000-node flow | 200 versions | median 197 ms, p95 257 ms; validate 8 ms | Yes |
| List versions, 2,000-node flow | 200 versions (92 MB key) | 2.2 s; oldest version 2.4 s; diff 2.5 s | **No** (< 200 ms) → B46 |
| `list_runs`, one flow | 200 runs × 100 / 1,000 steps | 67 ms / 694 ms | 1,000 steps: **no** |
| `list_runs`, all flows | 20 flows × 200 runs × 100 / 1,000 steps | 1.4 s / **18.4 s** | **No** → B44 |
| `list_runs`, all flows | 100 flows × 200 × 100 steps | 8.9 s | **No** |
| `get_run` for a missing id | as above | 1.4 s / 18 s / 9.3 s (scans everything) | **No** |
| Valkey size | 20 flows × 200 × 1,000 steps | 919 MB | **No** (capped lists are still too big) |
| Planner predict | 1×1 / 10×5 / 20×10 / 50×20, 7 days | 0.2 s / 10.7 s / 30.6 s / 161 s (background) | Yes (off-lock) |
| `_apply_rules`, nothing new | same | 0.5 ms / 244 ms / 4.4 s / **64.5 s** | **No** past 10×5 → B18 |
| `save_rule` | same | 13 ms / 0.96 s / 5.7 s / 60 s | **No** past 10×5 |
| One satellite merge under lock | 20×10 / 50×20 | 3.7 s / 60 s (full recompute ≈ 73 s / ≈ 50 min of lock) | **No** |
| Tick during recompute | 10×5 | max 12.6 s | **No** (< 1 s); activities start late |
| `/schedule`, 1 day | 1×1 / 10×5 / 20×10 / 50×20 | 24 KB, 1 ms / 1.3 MB, 185 ms / 4.6 MB, 1.2 s / 12.7 MB, 7.9 s | **No** past 1×1 → B43 |
| `/schedule`, full window | 50×20 | 26 MB, 42 s | **No** |
| Planner status | 10×5 / 20×10 / 50×20 | 44 / 234 / 755 ms | **No** past 10×5 → B45 |
| Flows tab polling, 1 client | 10×5 | 1 `/status` + 1 `list_runs` + 2 `get_run` per s; 50 KB/s; tick p50 0.8 ms | Yes |
| Flows tab polling, 5 clients | 10×5 | `list_runs` p50 600 ms; 0.78 cores | Borderline |
| Flows tab polling, 1 / 5 clients | 20×10 | `/status` p95 5.6 s / p50 2.2 s; tick p50 2.7 ms / **3.9 s** | **No** → B45 |
| Valkey refused (outage) | — | each Valkey call 7.4 s before failing; manual trigger 6.4 s; `/status` max 6.3 s; STOP ALL fails after 6.9 s with 1 run active and 2 flows armed | **No** → B3 |
| Valkey paused (no answer) | — | 0 of 20 `/status` calls answered within 1 s; the trigger never returned | **No (P0)** → B3 |
| Recovery after outage | — | buffer flushed in 1.4 s | Yes |
| Startup, 4 interrupted runs + 8 scripts (5 s stop each) | — | 40 s before HTTP answers | **No** → B28 |
| STOP ALL, 6 detached scripts (5 s stop each) | — | 30 s | **No** → B3 |
| Soak: 5,000 short runs | — | 325 runs/s; RSS 66 → 85 MB; Valkey flat at 12 MB (caps work); threads 7 → 507 during, back to 7 after 31 s idle | Mostly yes; watch RSS over a day |
| Restart with 20 active runs | — | start 15 ms; 20 scripts stopped; 20 interrupted runs recorded | Yes (but see B26: no leader lock) |

### Findings

| ID | Sev | Finding | Backlog |
|---|---|---|---|
| P-1 | P0 at 20×10+ | Planner rule application and merges hold the lock for seconds to minutes; activity starts are late and passes can be missed. | → B18 |
| P-2 | P1 | `/schedule` payloads of 1–26 MB (full activity snapshots in the list). | → B43 |
| P-3 | P1 | Run history listing and lookup scan everything (up to 18 s); 919 MB in Valkey. | → B44 |
| P-4 | P2 | Version listing for large flows (2.2 s, 92 MB). | → B46 |
| P-5 | P1 | `/status` and the tick degrade with several clients at 20×10. | → B45 |
| P-6 | P0 | A paused or partitioned Valkey holds `engine.lock` indefinitely; a refused one adds about 7 s per call. | → B3 |
| P-7 | P2 | Serial script stops: startup blocked 40 s, STOP ALL 30 s. | → B28, B3 |
| P-8 | P3 | Triggers fine to 200 armed flows; 500 flows = 25% of a core. A slow COSMOS API stretches the tick period. | Document |
| P-9 | P2 | No leader lock: an old engine object keeps writing history after a restart. | → B26 |

### Recommended capacity limits (document until B18, B43–B45 land)

- **Contacts:** up to **10 satellites × 5 stations** over a 7-day window,
  with up to 3 rules. Above that, scheduling latency reaches seconds.
- **Armed flows:** up to **200** (≈ 600 watched telemetry refs).
- **Run steps:** keep typical runs under about 100 recorded steps; avoid
  long loops that record 1,000 steps on every run.
- **Open Flow Automator tabs:** at most 2–3 at once on a 20×10 schedule.
- **Large flows:** 2,000 nodes save fine (200 ms), but avoid hundreds of
  versions of them.

**Not measured:** a full day of uptime (RSS trend), the UI's own render
cost, real COSMOS/Traefik latencies.

---

## 11. Operator workflows and feature gaps (grade B−)

**Scenario walkthroughs:**

| Scenario | Works | Stuck on |
|---|---|---|
| S1. Nightly LEO pass, walk mode | Rules with snapshots; Wait for contact/packet; Stop at LOS; re-timing; auto TLEs; Rehearse; approvals close before LOS | Nobody approves at night (only run mode + Allow hazardous) → B65; no Retry/Skip, so one failed check fails the pass → B62; no cleanup at failure or LOS → B61; commands not verified → B63; no check that downlinked files arrived; no ground-station interface step → B68 |
| S2. Anomaly response from limits | Limits trigger → run-mode flow; skip re-trigger; failure alert; dry-run rehearsal on live telemetry | One item only, polled at 4 Hz (short violations missed), no persistence → B66; no contact awareness; can't stop routine flows on the same satellite; no `tlm_age` → B74 |
| S3. Bench test campaign | Loops, parameters, cron, Run Script results, version pinned per run, history search | No export → B67; 1,000-step and 200-run caps lose the record; no PASS/FAIL-and-continue Check node; no step numbers or instructions; no campaign labels → B67, B74 |
| S4. Constellation, shared stations | Capacity, pre-empting priority, one row per station, `contact_target` templating, one Space-Track query | No alternate station or slew time; triggers can't be templated over targets; Stop can't be scoped to one satellite; stale TLEs don't block runs; no masks or blackouts → B20, B74 |
| S5. Shift handover | Audit log, All running, bell and webhooks, 409 on conflicts, version notes, `decided_by` | No handover summary, notes or acks; can't claim a run; approver can be the starter; no escalation → B74 |
| S6. Integration | Script Runner via Run Script | A prompting script blocks unseen, with no link to it; no Calendar publishing; no Autonomic or limits-set nodes. **Enterprise:** a `CriticalCmdError` becomes a generic failure while the command waits in COSMOS's critical queue (someone can send it later, outside the flow); with `OPENC3_DEFAULT_QUEUE` set, commands are silently queued but recorded as sent → B64 |

**Old GAP list:**

| Old ID | Status |
|---|---|
| GAP-2 durable runs | Partial (recorded, not resumed; D25) |
| GAP-5 run timeout | Partial (Stop at LOS only) |
| GAP-14 sub-flows | Partial (pinned; no inputs/outputs) |
| GAP-19 template gallery | Partial (demo flows) |
| GAP-26 scheduling | Partial (GEO, priority and internet TLEs done; exports, masks, blackouts open) |
| GAP-18 text/YAML round-trip | Drop for now |
| GAP-23 | Becomes escalation (G-20) |
| GAP-24 | Not an operator gap |
| All others (GAP-1, 3, 4, 6–13, 15–17, 20–22, 25, 27) | Open |

**G- findings:** G-1 → B61, G-2 → B62, G-3 → B65, G-4 → B63, G-5 → B67,
G-6 → B68, G-7 → B66, G-15 → B64, G-8 to G-14 and G-16 to G-21 → B74. Full
designs are in `eval_ops/report.md`.

---

## 12. Roadmap

### 1.12.0: "Safe to leave running" (section 2.1)

B1–B18, plus B48 (CI) and B49 (contract tests) so the fixes land on a safety
net. Answer Q1–Q5 and Q8 first. Turn every repro listed in the appendix into
a regression test. Then run the browser suite against the installed build
(it hasn't run since 1.8.0).

Suggested order inside the release (each step a commit):

1. Runner integrity: B1, B2, B6, B12 (`runner.py`, `graph.py`).
2. STOP ALL and Valkey: B3 (`engine.py`, `store.py`, `http_api.py`, own Valkey client).
3. Arming and modes: B4, B5, B7 (`engine.py`, `contacts.py`).
4. Inputs and secrets: B8, B9, B10.
5. Scheduling: B11, B15, B16, B17, B18 (with the `planner/` split if wanted).
6. Editor: B13, B14 (with B51's first vitest specs).

### 1.13.0: "Correct and fast"

Section 2.2 (B19–B42) and 2.3 (B43–B47); B50, B52, B53. Measure again with
the `eval_perf/` benchmarks and publish the capacity limits.

### 1.14.0: "A failed step isn't the end of the pass"

B61–B64, B69, B70, B71; B54 docs split; B57 refactor steps alongside.

### 1.15.0 and later

B65–B68, B72–B74, Enterprise testing (A-5, SEC-3, B64), the release workflow
(GAP-15).

---

## Appendix: repro scripts

Base path:
`/tmp/claude-1000/-home-alex-Projects-cosmos/01366e74-7070-4a6b-805c-59c8f04f1abe/scratchpad/`

The scratchpad is temporary: copy what you want into `test/` before it goes.
Each `report.md` there is the reviewer's full report. Several are condensed
copies saved by the coordinator because subagents couldn't write files.

| Folder | Files | Covers |
|---|---|---|
| `eval_engine/` | `test_eval_engine.py`, `test_e3.py`, `test_e4_control.py`, `test_e4_race.py`, `test_e_more.py` | B1–B6, B9, B12, B29 |
| `eval_regress/` | `test_r_runner.py`, `test_r_geo.py`, `test_r_snap.py`, `test_r_tle.py`, `test_r_store.py`, `test_r_stopsend.py`, `slots.py`, `expr_bomb.py`, `expr_bomb2.py`, `ssrf.py`, `fuzz.py`, `old/` (1.7.8 source) | B1, B3, B6, B7, B8, B9, B15, B17, B23, B32 |
| `eval_contacts/` | `test_eval.py`, `test_eval2.py`, `test_eval3.py`, `xcheck.py` (skyfield), `conftest.py` | B8, B11, B15–B22 |
| `eval_security/` | `test_sec_repro.py`, `test_oom_flow.py`, `test_hidden_snapshot.py`, `t1_webhook_leak.py`, `t_cpu.py` | B4, B7–B10 |
| `eval_ux/` | `switch_repro.mjs`, `kv_repro.cjs` | B13 |
| `eval_perf/` | `bench_*.py`, `common.py`, `results_*.json` | Section 10 |
| `eval_maint/` | `cov.txt`, `flaky.txt`, `node.txt` | Section 8 |
| `eval_ops/`, `eval_docs/` | `report.md` | Sections 7, 11 |

**How to run.** Python repros:

```
docker run --rm -e PYTHONPATH=/p/test:/p/lib:/s \
  -v $PWD/openc3-cosmos-flowautomator:/p:ro -v <folder>:/s -w /s \
  fa-pytest python -m pytest -q -p no:cacheprovider -s /s/<file>
```

JS repros: `docker run --rm -v <plugin>:/repo:ro -v <folder>:/w:ro -w /w openc3inc/openc3-node:latest node <file>`.

Benchmarks need a Redis on a private network (`docker network create fa-perf-net`,
`docker run -d --network fa-perf-net --name fa-perf-redis redis:7-alpine`);
see `eval_perf/common.py`. Remove them afterwards.
