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
