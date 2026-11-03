# Flow Automator

A COSMOS plugin for building automation as flow graphs: drag nodes onto a
canvas, wire them together (if, loops, parallel branches, send commands, run
scripts, wait for telemetry…) and let the engine run them on triggers. A
contact planner predicts satellite passes from TLEs and schedules flow
templates into them, so a flow can start before telemetry is flowing and run
relative to AOS and LOS.

The plugin has two parts:

- **Flow Automator tool**, the editor and the live view of runs, under Tools
  in COSMOS. Its toolbar switches between **Flows** and **Contacts**.
- **FLOW_ENGINE microservice** (Python), which stores the flows, keeps their
  triggers armed, predicts contacts, starts scheduled activities and runs
  everything server side, so nothing depends on the tool being open.

## Install

Upload the gem in Admin Console → Plugins, or:

```
openc3.sh cli load openc3-cosmos-flowautomator-X.Y.Z.gem
```

Pass prediction needs the `sgp4` Python package, which COSMOS installs from
the plugin's `requirements.txt` at load time (numpy comes with COSMOS). An
air-gapped COSMOS needs a PyPI mirror (Admin → Settings → PyPI URL). Without
sgp4 flows still work and the Contacts view says why passes are missing.

### Plugin variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `flow_engine_port` | 7790 | Port of the engine's HTTP API inside the operator |
| `flow_route_prefix` | `/flow-automator` | Traefik route to the engine. The tool expects `/flow-automator` |
| `trigger_period` | 0.25 | Seconds between trigger polls |
| `run_history` | 50 | Runs kept per flow |
| `contact_horizon_days` | 7 | How far ahead passes are predicted |
| `retime_tolerance` | 120 | Seconds a pass may move after a TLE update and still carry its activities |
| `stale_tle_days` | 3 | TLEs older than this are flagged |
| `activity_retention_days` | 7 | Finished activities are deleted this long after their pass |

## Flows

A flow starts at a **trigger** and follows wires: each node finishes, then the
node its output is wired to runs. Every output takes one wire, except
Parallel, whose wires all run at once until they meet at a Join. Nodes that
can fail have an **error** output; if it's wired the run continues there
(with the message in the `error` variable), otherwise the run fails.

Flows are **disarmed** when saved. Arm a flow to let its triggers fire; **Run
now** works either way. **Stop all** disarms every flow, stops every run and
pauses the contact scheduler.

### Execution modes

| Mode | Operator approval |
| --- | --- |
| Crawl | Before every step |
| Walk | Before hazardous commands and steps marked *Ask operator first* |
| Run | Never. A hazardous command fails unless its node has *Allow hazardous* |

Approvals show on the canvas and in the toolbar: **Go**, **Go & run rest**
(stop asking for routine steps in this run; hazardous commands still ask) or
**Abort run**. A flow's *Approval timeout* fails the step if nobody answers.
A sub-flow runs at least as cautiously as the run that called it.

### If triggered while running

*Skip* the new trigger, *queue* it (up to 10), or run another copy in
*parallel*.

### Expressions and templates

Conditions are a safe subset of Python: arithmetic, comparisons, `and`/`or`,
`x if c else y`, variables by name, and the functions `tlm`, `tlm_raw`,
`tlm_formatted`, `tlm_with_units`, `var(name, default)`, `abs`, `min`, `max`,
`round`, `int`, `float`, `str`, `bool`, `len`, `now`, and in scheduled runs
`time_to_aos()`, `time_to_tca()`, `time_to_los()`. COSMOS check syntax works
too: `INST HEALTH_STATUS TEMP1 > 50`. There's no attribute access, import or
lambda, so a condition can't run arbitrary code.

Text fields (commands, script names, log messages) take `{{ expression }}`:
`INST COLLECT with TYPE {{ collect_type }}, DURATION {{ duration }}`.

### Node reference

| Node | Outputs | Settings | What it does |
| --- | --- | --- | --- |
| **Triggers** | | | |
| Manual | out | | Starts on Run now, a sub-flow call or a scheduled activity |
| Telemetry | out | condition, cooldown | Fires when the condition goes from false to true (not when armed while already true) |
| Schedule | out | interval or cron (UTC) | Fires every N seconds or on a 5-field cron |
| Packet received | out | target, packet | Fires each time the packet's received count goes up |
| Limits | out | item, states | Fires when the item's limits state changes into a chosen state (RED by default) |
| **Logic** | | | |
| If | true, false, error | condition | Branch on a condition |
| Switch | case_0…, default, error | expression, cases | Branch on the first case equal to the value |
| Loop | body, done, error | count or while condition, variable, max | Repeat the body; wire the body's end back into the loop |
| Parallel | out (many) | | Run every wire at once |
| Join | out | | Wait for all parallel branches |
| Set variable | out, error | name, expression | Store a value |
| **Actions** | | | |
| Send command | out, error | command, allow hazardous | Send a command (checked and logged by COSMOS like any other) |
| Run script | out, error | file, wait, timeout, allow errors, environment | Start a script in Script Runner, optionally wait for it |
| Set telemetry | out, error | item, value, type | Set a current value |
| Log | out | message, level | Write to the COSMOS log |
| **Waits** | | | |
| Wait | out | seconds | Pause |
| Wait for telemetry | ok, timeout, error | condition, timeout, poll | Wait for a condition, or time out |
| Wait check | out, error | condition, timeout, poll | Wait for a condition; fail on timeout |
| **Contacts** | | | |
| Wait for contact | out, error | AOS/TCA/LOS, offset | Wait until a time relative to the run's pass |
| Contact time left | ok, short, error | seconds | Branch on whether that much of the pass is left |
| **Flow control** | | | |
| Sub-flow | out, error | flow, copy variables back | Run another flow from its Manual trigger and wait |
| End | | success/failed, message | Finish the whole run |

## Contacts

### Satellites and ground stations

Add a satellite and paste its TLE (two lines, or three with a name), and add
ground stations with a location, an elevation mask and a **capacity** (how
many satellites it can work at once, e.g. its antennas). The engine predicts
every pass for every satellite × station pair, from 6 hours ago to the
horizon, with SGP4. Prediction runs in the background one satellite at a
time, and only for what changed; the Contacts view shows the progress.
Predictions agree with skyfield to well under a second.

TLEs older than `stale_tle_days` are flagged. Each satellite keeps its last 5
TLEs. TLEs come from the dialog today; other sources (files, URLs such as
CelesTrak, telemetry) plug into `lib/flow_automator/tle_sources.py`.

### Templates

Any flow with a Manual trigger can be scheduled. Turn on **Template for
scheduled activities** in its flow settings to list it first and declare
**parameters** (string, number, boolean or choice, with defaults). Parameters
become run variables of the same name. Runs started by an activity also get:

`contact_satellite`, `contact_target`, `contact_station`, `contact_aos`,
`contact_tca`, `contact_los`, `contact_duration`, `contact_max_elevation`,
`contact_id`, `activity_id`, `activity_label`

A typical pre-pass flow: configure (log, set up), **Wait for contact** AOS,
send commands without waiting for telemetry, then **Wait for telemetry** for
the first packet, with **Contact time left** guarding the long steps.

### Activities

Click **Schedule** on a pass: pick a template, fill its parameters, and set
the start relative to AOS, TCA or LOS (`AOS - 120` starts two minutes before
the satellite rises). Options:

- **Mode**: crawl, walk or run. Approvals close *Approvals close before LOS*
  seconds before LOS, then the step fails.
- **May start late by**: if the engine couldn't start it on time (restart,
  scheduler paused) it starts late within this, else it's marked missed.
- **Stop the run at LOS**, and an **expected duration** for overlap warnings.

The activity keeps a copy of the template from when it was scheduled. If the
template is edited later the activity says *template changed*; **Update**
copies the new version.

When a new TLE moves a pass, its activities move with it (up to
`retime_tolerance`). A pass that moves further, or disappears, leaves its
activities *needing review*: **Accept new time** or move them. They don't run
until then.

### Rules

A rule schedules a template into every matching pass as passes are
predicted: chosen satellites and stations (empty means all), passes above a
maximum elevation and at least a duration, with the same start, mode and
parameters as an activity. Saving a rule re-plans its activities that
haven't started. Deleting one of its activities cancels it, and the rule won't
put it back. Higher **priority** rules claim passes first, and with *Skip
passes when the station is already busy* a rule leaves passes whose station
is at capacity.

### Station conflicts

Activities in passes that overlap at a station beyond its capacity are marked
*station busy*, and the timeline outlines those passes. It's a warning;
scheduling is still allowed.

### Pause and Stop all

**Pause scheduler** stops activities starting (any that pass their late
tolerance while paused are marked missed). **Stop all** pauses it too;
resume it from the Contacts view.

## Permissions and audit log

Every change goes through the engine's API with your COSMOS token. Reading
needs COSMOS's `tlm` permission; changing anything (saving, arming, running,
approving, scheduling) needs `script_run`, the Script Runner role, because
flows send commands and run scripts. COSMOS Core has one user, so any login
can do both.

**View → Audit Log** lists who did what, newest first: flows saved, armed and
run, approvals with the decision, satellites, TLEs, stations, activities and
rules, and what the scheduler did on its own (started, missed, flagged for
review, scheduled from rules). Enterprise shows the real username; Core shows
`anonymous`. The engine keeps the latest 10,000 entries, and every entry is
also an `[audit]` message in the COSMOS log.

## HTTP API

The engine serves `/flow-automator/api/*` (needs an `Authorization` token and
`scope`): flows (`/flows`, `/flows/<id>/arm|disarm|run`, `/validate`), runs
(`/runs`, `/runs/<id>/stop|approve`, `/stop_all`), contacts (`/schedule`,
`/contacts`, `/satellites`, `/satellites/<id>/tle`, `/stations`,
`/activities`, `/rules`, `/scheduler/pause|resume`), `/audit`, `/whoami`,
`/status` and `/health`. The full list is at the top of
`lib/flow_automator/http_api.py`.

## Development

Layout:

- `lib/flow_automator/`: engine (`engine.py`, `runner.py`, `graph.py`,
  `expressions.py`, `triggers.py`), contacts (`orbit.py`, `contacts.py`,
  `templates.py`, `tle_sources.py`), `audit.py`, `http_api.py`, `store.py`
- `microservices/FLOW_ENGINE/flow_engine.py`: the COSMOS microservice
- `src/tools/flowautomator/`: the Vue tool

Build: `pnpm install --frozen-lockfile --ignore-scripts`, then
`rake build VERSION=X.Y.Z`.

Engine tests (no COSMOS needed):

```
uv run --no-project --with pytest --with requests --with sgp4 --with numpy python -m pytest test
```

Lint: `pnpm lint --max-warnings 0`, and ruff with
`--config ../openc3/python/pyproject.toml lib microservices test`.

Browser tests run against a live COSMOS with this plugin and the demo plugin
(for the INST target) installed, in the Playwright container:

```
COSMOS_PASSWORD=... test/ui/run.sh                    # all of them
COSMOS_PASSWORD=... test/ui/run.sh rules_test.mjs     # one
```

`contacts_test.mjs` makes a TLE with epoch now and puts a station under its
track, so a real pass starts a few minutes in and the scheduled flows run at
AOS; it takes about five minutes.

## Limits

- A run that's in progress when the engine restarts fails; it isn't resumed.
- Each run keeps its last 1,000 steps; each flow its last `run_history` runs.
- Saving the same flow from two places at once: the last save wins.
- Predicting 50 satellites × 20 stations for 7 days takes about a minute in
  the background after a restart or a full recompute.
- TLEs come from the dialog only; times are UTC; stations have one elevation
  mask, not a horizon profile.
