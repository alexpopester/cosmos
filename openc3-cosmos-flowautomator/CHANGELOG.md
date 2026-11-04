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
