# Changelog

Every version of the Flow Automator plugin gem, newest first. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
versions follow [Semantic Versioning](https://semver.org/).

**Rules (see `CLAUDE.md`):**

- Every version bump gets an entry here, in the same commit as the change.
  `rake build VERSION=X.Y.Z` refuses to build a version that has no
  `## [X.Y.Z]` heading here.
- Work that hasn't been built yet goes under `## [Unreleased]`. When you
  build, rename that heading to the version and date.
- Write entries for operators and developers: what changed and why it
  matters, not how. Group them under Added / Changed / Fixed / Security /
  Removed / Docs.
- **Patch** (x.y.Z) is for fixes. **Minor** (x.Y.0) is for new features or
  behaviour changes. **Major** (X.0.0) is for changes that break stored
  flows, the API or `plugin.txt` variables.

## [Unreleased]

### Docs
- **Commanding real flight software in contacts** (`realdata/cfs_setup.py`,
  `realdata/soak.sh cfs-faults`, step 4): pass ops on NASA cFS behind a contact
  gate, in every real NOAA 19 pass over Fairbanks, plus 7 staged fault
  scenarios (dead pass, early LOS, reboots, packet loss, link after LOS).
- **Replay regression on recorded real passes** (`realdata/soak.sh replay`,
  step 3c): replays real CANVAS, AEPEX and CatSat passes as recorded, with a
  fault injected, and lost, and checks that the post-pass templates say
  healthy, raise an alert, and say not heard.
- **Post-pass checks on real telemetry** (`realdata/realtlm_setup.py`, step 3b
  of `docs/REAL-DATA-TESTING.md`): one template per satellite (CANVAS, AEPEX,
  CatSat) that waits after LOS for the pass's SatNOGS beacon, then checks
  command counters, mode, power and temperatures. The soak records each
  outcome and reports them.

## [1.14.1] - 2026-11-05

### Fixed
- **Adding a station or satellite re-plans faster.** After each satellite's
  passes were predicted, rules re-planned every contact of every satellite;
  now only that satellite's (the full re-plan every minute still runs).
  Adding one station at the soak's scale (16 satellites) went from 5.4 s to
  3.6 s in the benchmark, most of it now orbit propagation.

## [1.14.0] - 2026-11-05

Scale fixes found by the real-pass soak (`docs/REAL-DATA-TESTING.md`,
EVALUATION-2026-11-05 B43 and B45). Measured at the soak's scale, 16
satellites × 13 stations with 4,600 scheduled activities.

### Fixed
- **Scheduler ticks no longer stall with many activities.** Station capacity
  checks scanned every activity for every pass, so one tick could take 4 to
  10 seconds with a few Contacts views open, delaying activity starts. Now
  about 25 ms (D30).
- **`/status` is 3 ms instead of 255 ms**: station conflicts are cached until
  something changes.
- **The Contacts view loads a day in 34 ms instead of 1 s**, and 123 KB
  instead of 6 MB.

### Changed
- **`/schedule` lists only the activities in the requested time range**,
  plus any running or needing review, without template parameter definitions.
  It counts the scheduled ones it left out (`activities_outside`). The
  Contacts view's table is now "Activities in view" and says how many more
  are scheduled outside the range.
- **Large API responses are gzipped** when the client accepts it.

### Added
- `GET /activities/<id>`: one activity in full. The activity dialog loads
  its parameters from it.
- `realdata/bench_soak.py`: rebuilds the soak in memory and times
  `/schedule`, `/status` and the scheduler tick.

## [1.13.2] - 2026-11-05

Found by the real-pass soak (`docs/REAL-DATA-TESTING.md` step 2).

### Fixed
- **A satellite added for a recently fetched catalog number gets its TLE at
  once.** The engine keeps CelesTrak's and Space-Track's last answer for
  each catalog number, so deleting and re-adding a satellite (or adding a
  second one for the same object) no longer leaves it without a TLE until
  the next fetch, up to 4 hours later. No extra request is made.
- **A satellite waiting for its next fetch says so**, with the time, in the
  Contacts view, instead of showing nothing.

## [1.13.1] - 2026-11-05

Found by the real-pass soak (`docs/REAL-DATA-TESTING.md` step 2).

### Fixed
- **One satellite CelesTrak doesn't publish no longer stops TLE updates for
  all the others.** CelesTrak answers 404 "No GP data found" for catalog
  numbers it doesn't publish (temporary analyst numbers 80000-99999,
  decayed objects), and the fetch used to give up there, on every retry, so
  satellites after it never got a TLE. Now that satellite says why (and
  suggests Space-Track) and the rest are fetched.
- **A CelesTrak server error for one satellite is retried in 10 minutes**,
  not after the whole fetch interval (4 hours by default), and doesn't stop
  the others. CelesTrak gives intermittent 500s for single catalog numbers.
  Only when every request fails is CelesTrak treated as down.

### Docs
- **Real-data testing plan** (`docs/REAL-DATA-TESTING.md`): how contact
  scheduling gets proven against real satellites (SatNOGS) and real flight
  software (NOS3), in four steps.
- **Pass predictions checked against real passes.** New developer tool
  `realdata/satnogs_check.py` (not part of the gem) compares our predictions
  with SatNOGS observations. Over 750 real passes (172 satellites, 182
  stations), none of 13,436 decoded frames arrived while we predicted the
  satellite was below the horizon, and AOS/LOS agree to within 1.7 s / 1.2 s
  (95th percentile).
- **Real-pass soak tooling** (`realdata/soak.sh`): sets up real SatNOGS
  stations and CelesTrak-tracked satellites with one rule, records every
  prediction and activity change, and scores the soak against what SatNOGS
  stations heard.

## [1.13.0] - 2026-11-05

Correctness fixes from the second evaluation (`docs/EVALUATION-2026-11-05.md`,
section 2.2).

### Fixed
- **"Run vN" asks once.** Choosing to run the last saved version used to ask up
  to three times, and the run dialog showed the draft's parameters and mode;
  it now describes the saved version and says the edits aren't included (B36).
- **Approval cards are harder to answer by accident.** Ask answers get the
  same short click guard as Go, a card that slides up under the cursor after
  another is answered is guarded too, and a new question no longer takes the
  keyboard from a field you're typing in (B37).
- **A hung engine shows *Engine not answering* within a few seconds**, status
  polls no longer pile up (one at a time, 4 s timeout), approval cards are
  locked and marked as possibly out of date until it's back, and a hidden tab
  polls less (B38).
- **Leaving for another COSMOS tool with unsaved edits asks first** (B39).
- **Slow answers no longer overwrite newer ones** in the Problems list, the
  version diff and Run History search; Run History says when it couldn't load
  instead of showing no runs; the telemetry picker remembers the cursor per
  node (B40).
- **Contacts marks GEO day contacts (*all day*) and partial ones**, Schedule
  is disabled on partial contacts, and LOS shows its date when it's on another
  day (B42).
- **A COSMOS API blip no longer blinds every telemetry trigger for 30 s.** When
  every read fails, nothing is set aside as a bad item (B30).
- **Cron:** a day-of-month like `*/2` combines with the weekday the way cron
  does (AND), a clock that steps back never fires a minute twice, and minutes
  the engine didn't get to check are logged (B31).
- **A node id, type or edge end that isn't text is a 400**, not a 500 (B32).
- **A script's stash result is taken atomically** (one transaction), so a
  value written between reading and deleting can't be lost (B34).
- **One TLE update that moves many passes raises one review alert** listing
  them, not one per activity (B41).
- **The TLE jump limit grows with the current TLE's age**, so good updates
  after a feed outage aren't all held (B41).
- **One GEO activity no longer blocks its station for the whole day.** A GEO
  day contact uses the antenna only while its activities run, so a GEO
  activity needs a duration; rules without one skip GEO days and say so (B20).
- **A hand-edited rule activity is flagged for review when its pass moves**,
  keeping the edits, instead of being cancelled and replaced (B21).
- **Migrations now upgrade the flow snapshots inside scheduled activities**,
  sub-flow snapshots included: these are what actually run (B24).
- **During an upgrade the new engine leaves the old one's runs alone.** Each
  engine has an id and a heartbeat; runs are recorded as interrupted only once
  their engine is gone (B26).
- **A planner that fails to load at startup is retried** every 30 s, with an
  alert, instead of leaving no schedule until a restart (B27).

### Security
- **Webhooks connect to the address that was checked** (no DNS rebinding),
  AWS IPv6 and Alibaba metadata addresses are refused, and **Test** can't be
  used to probe the network (once every 5 s per webhook) (B33).
- **TLE answers are read with a size cap** instead of in full, and CelesTrak
  requests go one a second (B35).
- **Template parameter patterns can't freeze the scheduler.** Patterns that
  can backtrack for ever (`(a+)+`, `(a|ab)*`) or are over 200 characters are
  refused, and values are checked only up to 1,000 characters (B22).
- **Backups need write permission**, run one at a time and are audited. A
  read-only user could have the engine build any number of full backups (B25).

## [1.12.0] - 2026-11-05

Fixes from the second evaluation (`docs/EVALUATION-2026-11-05.md`, backlog
B1–B19; owner's answers to Q1–Q4; D6, D22, D23, D24, D28 and D29 amended).

### Changed
- **The approved command is sent exactly as approved.** It used to be
  rendered again and refused if it differed, so a command using `now()` or
  changing telemetry could never be sent after approval.
- **In crawl and walk, *Allow hazardous* no longer skips the approval.** Only
  an operator acknowledges a hazardous command; the flag counts in run mode.
- **If COSMOS refuses a command as hazardous that the plugin's check passed,**
  crawl and walk ask for approval then, showing the exact command. The check
  now also sees parameter defaults and state names in any case.

### Fixed
- **The scheduler no longer stalls on large plans.** Applying rules scanned
  every taken pass for every contact under the planner lock: at 20 satellites
  × 10 stations it took 2 s with nothing to do (64 s at 50 × 20), and a full
  recompute 175 s, delaying activity starts. Taken passes are indexed now, and
  re-timing after predicting one satellite only looks at that satellite's
  activities: 21 ms idle, and about 70 ms under the lock per satellite (B18).
- **Saving a rule no longer deletes other rules' activities.** It used to
  remove every lower-priority rule activity (even one due in seconds) and
  re-create them with new ids and fresh snapshots. Now only that rule's own
  activities are re-planned, and a higher-priority rule pre-empts
  lower-priority ones one at a time, audited, never within 10 minutes of
  their start or once edited by hand (B15).
- **Rule priority holds when passes are predicted in batches** (window roll,
  restart, Recompute); the low-priority rule used to win (B19).
- **Rules skip passes too short for their approvals**, and a rule anchored at
  LOS inside the approval margin is refused (B15).
- **A GEO activity is no longer cancelled by the next recompute on its own
  day**: today's day contact starts at 00:00 (B15).
- **Bad TLEs no longer get through the safeguards.** An automated TLE dated
  more than a day ahead is rejected, a future-dated current TLE no longer
  blocks real updates, a TLE that can't be propagated to now is held instead
  of applied, and accepting a held TLE can't replace a newer one (B16).
- **Internet TLE fetching stays polite across restarts and failures.** Fetch
  times and back-off are kept in Valkey, Fetch now waits out a refusal (403,
  429), a failure part way through keeps what was fetched, and a failing
  source raises the new `tle_source_failing` alert once (B17).
- **A station edit no longer leaves activities on the old AOS and LOS.** They
  follow every time of the re-predicted pass, are flagged for review when the
  time they're anchored to moves too far, and are re-checked against LOS and
  the approval cut-off (B11).
- **Deleting a Switch case no longer re-routes another case** to the deleted
  case's target: later cases' wires move down with them (B13).
- **Starting variables are no longer corrupted while typing** (`1.5` saved as
  `15`, extra quotes around text) (B13).
- **The Armed switch follows the engine** (another operator, Stop all, a
  restore), and **a failed disarm shows *Still armed***, never Disarmed (B14).
- **Loops in parallel branches no longer reset each other.** Two branches each
  running a loop could send their commands extra times (a regression in
  1.9.0). Each branch now counts its own loops (B1).
- **Nodes after a Join can no longer run twice.** Two Parallels side by side
  sharing one Join with no enclosing Parallel is now a validation problem.
  Which Parallel owns each Join is worked out when the flow loads, not lazily
  by racing branch threads (B2).
- **Stop all works during a Valkey outage.** It used to stop nothing and
  return an error. It now takes effect at once and is saved when Valkey is
  back; the response says `persisted: false` until then (B3).
- **A Valkey that stops answering no longer freezes the engine.** The engine
  never waits on Valkey while holding its lock, and has its own Valkey client
  with a 5 s timeout (B3).
- **Stop all during an approved step no longer sends the command** (B3).
- **Clients with no valid token can't lock out Stop all** by filling the
  request slots: tokens are checked before a body is read, and Stop all and
  `/health` have a reserved lane (B3).
- **Shutdown no longer starts the next queued run** (B3).
- **Stop all stops every fire-and-forget script, all at once.** It used to
  forget scripts after 200 and stop them one slow call at a time (B3).
- **A Script Runner error while a step waits on a script no longer orphans
  the script.** Polling retries 5 times, and the script is always stopped
  when the step ends without it finishing (B12).
- **Pausing the scheduler works even when the planner is busy**, and the
  engine refuses scheduled starts while it's paused.
- A failed disarm no longer re-arms the flow after a restart, and a failed
  delete no longer leaves a flow that comes back after a restart.
- A delete can't be undone by a write retried after a Valkey outage.
- A finished run is never briefly "not found", and its history is queued
  before its active-run record is cleared, so a crash can't lose it.
- Startup stops interrupted runs' scripts all at once instead of one at a
  time.
- **Scheduled activities and rules can no longer run a flow below its mode.**
  A crawl template scheduled in run mode used to run with no approvals. The
  planner refuses it, and every real run is held to its flow's mode (B5).
- **An operator is never asked to approve a command that can't be shown.**
  The step fails instead; it used to approve the node's label and send
  whatever rendered later (B6).
- **A failed hazard lookup counts as hazardous**, not safe (B6).
- **More saves now disarm an armed flow:** turning dry run off, adding a node
  that acts, and changing what a node with *Allow hazardous* or *Allow raw
  values* sends or runs. Relaxing a sub-flow disarms the armed flows that call
  it (B4).

### Security
- **Any write user could send any COSMOS secret to any host** through the
  Space-Track settings. The login now only goes to Space-Track's fixed https
  address (`FLOW_SPACETRACK_URL` for an admin), secret names must start with
  `SPACETRACK_`, and stored or restored settings can't override either (B8).
- Catalog numbers are validated, so one can't change a Space-Track query;
  Alpha-5 numbers are converted for the query (B8).
- **One dry run could run the operator container out of memory.** List and
  dict literals now count toward the expression size limits, templates are
  capped, an expression may compute for 0.25 s, and a run's variables are
  capped together (B9).
- **Webhook URL secrets no longer leak to read-only users** through error
  text in the alert settings or the log (B10, S-2).
- A node's environment can no longer set `FLOW_RESULT_KEY`, which let a flow
  read and delete any stash key (S-14).
- **A flow can no longer carry hidden sub-flow content.** `subflow_snapshots`
  is stripped from saved, imported and restored flows; only the planner's
  activity snapshots pin sub-flows (B7).
- Version history restored from a backup is marked `from_backup` with who
  restored it, since its authors come from an editable file (S-5).
- Approvals carry a `hazardous` flag; the tool uses it instead of matching
  text in the reason (U-20).

### Docs
- Browser tests: new `editor_fixes_test.mjs` (Switch cases, typed variables, Armed switch); fixed
  `modes_test` (a duplicate declaration), `large_test` (the 16-branch cap) and `rules_test` (other rules
  holding the test station). All 10 pass against 1.12.0.
- README: Parallel/Join rule, Stop all during outages, modes and approvals, what
  disarms a flow; D6 and D28 amended; *Allow hazardous* hint corrected (DOC-6).

## [1.11.0] - 2026-11-04

CelesTrak and Space-Track TLE sources.

### Added
- **Keep the TLE current from CelesTrak or Space-Track.** Choose it in the
  satellite's dialog. Satellites are looked up by catalog number and fetched
  every 4 hours by default. A fetched TLE goes through the same safeguards as
  every automated TLE: newer epoch, preferred source, and big jumps held for
  review.
- **TLE sources and inputs** (formerly TLE inputs) has a new section for the
  internet sources:
  - addresses (for a local mirror) and how often to fetch;
  - for Space-Track, the names of the COSMOS secrets holding the login;
  - each source's last result, and a **Fetch now** button.
- **API:** `GET` and `PUT /api/tle_sources`, and `POST /api/tle_sources/fetch`.
- Backups include the TLE source settings, which hold the secret names but
  never the credentials.

### Changed
- **The services' rate limits are respected.**
  - Each satellite is fetched at most every 2 hours from CelesTrak and every
    hour from Space-Track, even with Fetch now.
  - A failing source retries after 10 minutes, doubling each time.
  - Space-Track gets one query for every satellite, not one per satellite.
- Satellites using an internet source must have a catalog number. Pasting a
  TLE sets it.
- A satellite whose last fetch found nothing, or whose fetched TLE was
  rejected, shows why in the satellite list.

### Security
- The Space-Track login is read from COSMOS secrets (Admin Console →
  Secrets) when needed. It's never stored by the plugin, returned by its API
  or written to backups (D29).
- The fetcher doesn't follow redirects. A source address can't point at a
  link-local address, which covers cloud metadata endpoints.

## [1.10.0] - 2026-11-04

Upgrades, backups and Valkey outages: the gaps the evaluation didn't cover
(`docs/EVALUATION-2026-11-04.md`, addendum A-1 to A-3 and A-6).

### Added
- **File → Back Up Everything** downloads one JSON file with the scope's
  flows and their versions, satellites with their TLEs, stations, rules, TLE
  input settings and alert settings. Webhook URLs are left out.
  `GET /api/backup?history=1` adds run history and the audit log.
- **File → Restore From Backup** merges a backup, into this COSMOS or
  another one:
  - Items are matched by id, then by name, and nothing else is deleted.
  - Restored flows are left disarmed.
  - The contact scheduler is paused until you resume it.
  - The result lists anything that wasn't restored, and why.
- **Storage formats.**
  - Everything the engine saves is stamped with a format version, and
    records from older versions are upgraded as they're read.
  - After a downgrade, records in a newer format are left untouched and an
    alert says so. A flow file exported by a newer version is refused.
- **Alert events:**
  - `storage_unavailable`, on by default: Valkey refused a write.
  - `storage_recovered`: writes are going through again.
  - `storage_format`, on by default: stored data is from a newer version.

  Existing installations keep their saved event choices, so turn these on
  in Alerts.

### Changed
- **The engine keeps going when Valkey stops taking writes.**
  - Run history, audit entries and the scheduler's own updates wait in
    memory, up to 5,000, and are written in order when Valkey is back.
  - The engine chip turns orange (*storage problem*), and `/health` reports
    `"store": "degraded"`.
  - Saving a flow still fails visibly.
  - At startup the engine waits for Valkey instead of exiting.
- `sgp4` is capped below 3 in `requirements.txt`.

### Security
- The tool bundles DOMPurify through `@openc3/vue-common`, and that copy
  (3.4.8 and 3.4.15) had published XSS advisories. A pnpm override now pins
  3.4.17, and `pnpm audit --prod` is clean.

### Docs
- **README:** upgrading, uninstalling and moving, backups, and what happens
  when Valkey goes away.
- **DEVELOPMENT:** a recipe for changing the shape of stored data.
- **DECISIONS:** D26 (formats), D27 (restore never starts anything) and D28
  (outage buffering).
- **EVALUATION:** an addendum listing the six areas the original review
  missed.

## [1.9.0] - 2026-11-04

Sub-flow pinning, plus the correctness fixes from the evaluation
(`docs/EVALUATION-2026-11-04.md`, section 2.2).

### Changed
- **Run now and Arm ask before saving unsaved edits.** You can save them as
  a new version and go on, use the last saved version, or cancel. The
  toolbar's mode control is labelled *Flow mode*, because it changes the
  saved flow.
- **Imported flow files without a mode open in walk**, not run.
- **Rules re-plan only when it matters.** Renaming a rule keeps its
  activities. Saving a change that affects which passes it takes re-plans
  that rule and every lower-priority rule, highest priority first. A
  higher-priority rule now takes station capacity from lower-priority ones,
  even when it is added later.
- **Edited rule activities are kept.** An activity a rule made, then edited
  by hand, is marked detached. Saving the rule no longer replaces it, and
  deleting the rule keeps it as a manual activity.
- **GEO satellites can be scheduled.** A satellite that never sets now gets
  one contact per UTC day (AOS 00:00, LOS 24:00), so rules and activities can
  anchor to a time of day. Before, it got a single window-long contact that
  nothing could schedule into.
- **Contacts only partly in the prediction window can't be scheduled.**
  These are passes in progress or past the horizon, whose times aren't known
  yet.
- **Approvals must close after the activity starts.** A crawl or walk
  activity (or one with an Ask node) is refused if approvals would close,
  at LOS minus the margin, before it starts.
- **The TLE stash key must start with `flow_automator_`.** It is read and
  then deleted, so it can't be pointed at another app's key.

### Fixed
- **Loops:** a loop left early (for example, breaking out of an inner loop)
  starts again from 0 the next time it is entered. Before, it resumed at its
  old count.
- **Interrupted runs:** runs active when the engine dies are recorded as
  failed at the next start, with an alert, and the Script Runner scripts they
  started are stopped. Before, they vanished without history. On a graceful
  shutdown, runs are stopped before alerts, so their alerts go out.
- **Run status:** `/status` no longer fails now and then with "dictionary
  changed size" while a run is adding variables.
- **Ask and approvals:**
  - An Ask with no timeout of its own follows *timeout* after the flow's
    approval timeout, or at the activity's approval cut-off. Before, it could
    wait forever.
  - An answer that arrives just as an approval times out is refused, instead
    of being reported as accepted while the run failed.
- **Sub-flows:** aborting a sub-flow stops its caller with the reason
  ("Aborted by …"), and the sub-flow step shows *stopped*.
- **Numbers in templates:** text like "nan", "inf", "1_000" or "007" in a
  multi-part template stays text.
- **Packet triggers:** one packet trigger naming a missing packet no longer
  breaks every packet trigger. Bad telemetry items or packets are left out
  of the batch read for 30 s instead of being retried on every tick.
- **TLE inputs:**
  - Polling now runs on its own thread, so a slow bucket or API call can't
    make activities miss their start.
  - A packet resending the same TLE isn't ingested or audited again, and
    unchanged ingests no longer fill the audit log.
  - The stash input only deletes the value it read.
- **Prediction errors:** a TLE that SGP4 can't propagate (decayed orbit, bad
  elements) shows a prediction problem and raises an alert, instead of
  silently showing no contacts.
- **Re-timing:** activities are no longer re-timed against an empty
  prediction while their satellite is being predicted after a restart.
- **Missed-activity alerts:** after downtime, many missed activities give
  one alert instead of one each.
- **Webhooks:** a webhook that keeps failing is skipped for 5 minutes, so it
  doesn't hold up the others, and pending deliveries are capped at 1,000.
- **HTTP API:**
  - Malformed requests (a body that isn't a JSON object, nodes or edges
    that aren't lists of objects, a bad `base_version`) get a 400 that says
    what's wrong, instead of a 500.
  - Internal errors no longer return exception text.
  - A token check that can't reach COSMOS no longer names internal hosts.
  - The engine's HTTP server limits concurrent requests to 64 and times
    out idle sockets after 30 s.
  - A request with a body but no token, or a bad `Content-Length`, is
    refused before the body is read.
  - The `Server` header no longer shows the Python version.
- **Editor:**
  - Restoring a version or a deleted flow now warns that unsaved edits in
    the editor will be lost.
  - *Save As Copy* creates the copy before switching to it, so a failed
    save no longer leaves the editor on a renamed, unsaved flow.
  - Delete flow, Clear demo, Reload theirs, scheduler Pause/Resume and
    version Export now report failures instead of failing silently.
  - Opening an approval from the toolbar while in Contacts switches to
    Flows.
  - The telemetry picker inserts at the cursor instead of at the end.
  - Importing a file that isn't a flow says so, instead of showing a
    TypeError.
  - Exports no longer risk being cancelled by revoking the download link
    too early.
  - A condition's hint no longer overlaps the next field.
- **Demo:** the demo's stations are now *Demo Wallops* and *Demo Svalbard*.
  Clearing the demo no longer deletes a station of yours with an old demo
  name.
- A scheduled activity now runs the sub-flows it was scheduled with. It keeps
  a copy of every sub-flow its template calls, directly or not, and its runs
  and rehearsals use those copies. Before, an edit to a sub-flow silently
  changed tonight's passes. A changed sub-flow shows as *sub-flow changed*;
  **Update** copies the current versions. Activity runs record the sub-flow
  versions they used (D11).

### Docs
- README: scheduling (priority, detached activities, GEO, partial contacts,
  approval timing), the stash key prefix, saving before Run/Arm, and
  interrupted runs. ARCHITECTURE: the `flow-tle-inputs` thread and the
  `active_runs` key. DECISIONS: D11 amended; D23–D25 added.

## [1.8.0] - 2026-11-04

Safety hardening from the evaluation (`docs/EVALUATION-2026-11-04.md`,
section 2.1).

### Changed
- **A flow's mode is now a floor for real runs.** **Run…** and the API can
  pick the flow's mode or a more cautious one. A dry run can use any mode.
  To run with fewer approvals, change the flow's mode (DECISIONS D6,
  amended).
- **Saving an armed flow that relaxes safety disarms it.** That means a less
  cautious mode, or *Allow hazardous* or *Allow raw values* turned on. The
  notice says why; arm it again to accept the change.
- **Hazardous approvals show a red *Send hazardous…* button.** It asks once
  more, showing the exact command. New approval cards ignore clicks for
  0.75 s, so a fast double-click can't approve the next step.
- **Buttons whose colour matters now show it.** COSMOS's global button style
  had turned Stop all, Abort, Stop, Discard and Save mine the same blue as
  Go; they are now red or amber.
- **A failed STOP ALL stays on screen.** A dialog stays open with Retry.
  After a successful one, the tool checks that nothing is still armed or
  running.
- **Validating a flow (`/api/validate`) needs write permission**, as saving
  does.

### Security
- Expressions are bounded so one can't stall or exhaust the engine:
  - limits on length, syntax tree size, result size and integer size;
  - limits on `format` widths and `round` digits;
  - sizes are checked before the result is built;
  - `sum` adds numbers only, and `%` formatting of text is refused.

  Before, one 2 MB expression sent to `/api/validate` held the engine for
  16 s and kept 400 MB in memory (DECISIONS D22).
- Runs can no longer fan out without limit:
  - sub-flow runs count against the 50 active runs;
  - Parallel branch threads share an engine-wide budget of 200;
  - a Parallel can have at most 16 branches;
  - a flow's `max_steps` is capped at 1,000,000.
- Webhooks can't target link-local addresses (cloud metadata services).
  They don't follow redirects, and a failed delivery reports only the HTTP
  status, never the response body (DECISIONS D21).
- A command approved in crawl or walk mode is only sent if it is exactly what
  the operator approved. Before, it was rendered again after approval, so a
  telemetry or variable change could send a different command, with the
  hazardous check skipped. Now the step fails and nothing is sent.
- Template values can no longer inject command parameters through a
  backslash inside quotes, through `[ ]` array brackets, or through spaces in
  the parameter list.
- STOP ALL:
  - It pauses the contact scheduler *before* stopping runs, and an activity
    can no longer launch after STOP ALL.
  - It also stops scripts that flows started without waiting.
  - It still works when COSMOS can't be reached to verify the token. The
    audit then records the user as unverified.

### Fixed
- A Parallel inside another Parallel, whose branches go straight to the
  outer Join, no longer runs the nodes after that Join twice.
- A trigger arriving while a "queue" flow hands over to its next queued run
  no longer starts a second run at the same time.
- Queued triggers are dropped if the flow is disarmed before they start. A
  queued run that can't start now raises an alert.
- Load Demo no longer resumes a contact scheduler that STOP ALL paused. It
  says the scheduler is paused instead.
- A rule activity an operator cancelled no longer comes back after a routine
  TLE update. Cancelled passes are matched by satellite, station and time,
  and they follow their pass like scheduled ones do.
- Contact ids stay the same when passes are predicted again. A pass no
  longer changes id because its TCA moved by a few milliseconds.

### Docs
- README: Stop all, approvals, the mode floor, expression limits and
  webhook rules. DECISIONS: D6 amended; D21 and D22 added.
- Added this changelog and the rule that every version bump updates it.
- Added `docs/EVALUATION-2026-11-04.md`: a full review of the engine, UI,
  scheduling, security and tooling, with a prioritised backlog.

## History before this changelog (1.0.0 – 1.7.8, 2026-11-03 – 2026-11-04)

These versions were built before the changelog existed. Each group lists
the commit that shipped it and the approximate range of versions.

### 1.7.x: editor layout and demo recording (`286708420`, `c2a0120fb`)
- Added View/Edit mode, foldable palette and settings panels, and a Show
  Flow/Both/Log switch with a resizable log.
- Added an All running tab and minimizable approval cards.
- The tool now fits above the COSMOS footer.
- Added a captioned demo walkthrough (`demo/walkthrough.sh`) that can drive
  OBS.

### 1.6.x: TLE inputs, docs, robust browser tests (`f9982dd37`, `6f257c603`, `46d20e6d0`)
- Added TLE inputs: a bulk endpoint, a COSMOS stash key, watched bucket
  files and telemetry packets.
- Automated TLE updates are checked before they are applied: catalog
  number, newer epoch, preferred source. A TLE whose orbit jumps is held
  for review.
- Added the developer docs: `CLAUDE.md`, `docs/ARCHITECTURE.md`,
  `docs/DECISIONS.md` and `docs/DEVELOPMENT.md`.
- Browser tests now leave other flows and the scheduler as they found
  them.

### 1.5.x – 1.6.0: dry runs, alerts, history, pickers, demo (`d33bcb41a`, `cecb5a252`, `4165dc8bf`, `59f74a4f8`)
- Added dry runs, which check commands against COSMOS without sending
  them.
- Values from variables or telemetry can no longer change a command's
  structure.
- Added alerts to the COSMOS bell and to Slack, Teams or generic webhooks.
  Approvals alert only if they stay unanswered.
- Every save is a version, with diff, restore and save-conflict checks.
- Runs now record what started them and which version ran.
- Added telemetry and command pickers, and new nodes: Ask operator, Read
  packet, Wait for packet, Notify. Run script now returns script results.
- Added a one-command demo setup.
- Background polling no longer raises COSMOS error toasts.
- `openc3.sh cli rake build` now works without Node.

### 1.4.x: audit log, rules, station capacity (`e3ca784a0`)
- Added an audit log of flow changes, approvals and scheduling.
- Every write now needs the COSMOS `script_run` permission.
- Added scheduling rules, station capacity with conflict warnings,
  prediction in the background for each satellite, and pruning of finished
  activities.

### 1.2.x – 1.3.x: contact scheduling (`a66b6b229`)
- Added SGP4 pass prediction for each ground station.
- Flow templates can be scheduled relative to AOS, TCA or LOS.
- Activities follow their pass when the TLE changes, and approvals close
  before LOS.

### 1.1.x: execution modes (`3de4ec186`)
- Added three run modes:
  - **crawl** approves every step;
  - **walk** asks before nodes marked for approval and before hazardous
    commands;
  - **run** never asks.
- Added approval cards with Go, Go & run rest and Abort.
- A mode can be overridden for a single run, and sub-flows run at the
  stricter of their own mode and their caller's.

### 1.0.x: first release (`d7129110e`)
- Added the Python FLOW_ENGINE microservice with server-side runs and
  armed triggers: telemetry, schedule/cron, packet and limits.
- Added the control-flow nodes, the Vue Flow editor, live run view, arm and
  disarm, STOP ALL, and hazardous-command blocking.
