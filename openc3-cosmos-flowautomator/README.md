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
| `run_history` | 200 | Runs kept per flow |
| `contact_horizon_days` | 7 | How far ahead passes are predicted |
| `retime_tolerance` | 120 | Seconds a pass may move after a TLE update and still carry its activities |
| `stale_tle_days` | 3 | TLEs older than this are flagged |
| `activity_retention_days` | 7 | Finished activities are deleted this long after their pass |

## Demo

One command puts a local COSMOS (docker, started with `openc3.sh`) in a state
to demo everything:

```
COSMOS_PASSWORD=... demo/setup_demo.sh            # add --reinstall to load the newest local gem
```

It installs the COSMOS demo plugin (the INST target) if it's missing, installs
the Flow Automator if it isn't running, waits for INST telemetry, then loads
the demo. **File → Load Demo** in the tool does the last step on any COSMOS
that has the INST target. Loading again resets the demo; it only replaces
items named for it (flows and rules starting "Demo: ", DEMOSAT satellites, the
demo stations), and **File → Remove Demo** takes it all out.

What it sets up:

| | |
| --- | --- |
| Satellites | DEMOSAT-1 (ISS-like) and DEMOSAT-2 (polar), TLEs with epoch now |
| Stations | **Demo Pass Site**, under DEMOSAT-1's track so a pass starts ~5 minutes after loading; Wallops; Svalbard (capacity 2) |
| Template | **Demo: Pass downlink**: parameters (choice, number, telemetry item), wait for AOS, wait for the first packet, collect, check, report with Notify; alerts if telemetry doesn't arrive or the pass is short |
| Rule | **Demo: DEMOSAT-1 downlinks**: Pass downlink at AOS − 60s in every DEMOSAT-1 pass above 20°, stop at LOS |
| Activity | Health check over Svalbard on DEMOSAT-2's next pass |
| Flows | **Health check** (Read packet, functions, If, a step that asks first), **Collect with approvals** (crawl, hazardous state and command, Ask operator), **Parallel checks** (separate variables per branch), **Script result** (a script hands a value back), **Hot temperature watch** (armed in dry-run mode on live telemetry) |

A walkthrough:

1. **Contacts** (it opens there): the timeline, the DEMOSAT passes, the rule's
   activities. The first downlink starts a minute before the Demo Pass Site
   AOS. Watch it go *running* → *done*; open its run to see it wait for AOS,
   wait for the first packet, send the collect and report. The bell shows its
   Notify.
2. Click **Schedule** on another pass, pick *Demo: Pass downlink*, fill in the
   parameters (the sensor has a telemetry picker), see the start time preview.
   Schedule two in one pass to see the overlap warning. Try **Rehearse** on an
   activity: a dry run with the real pass and parameters.
3. **Flows → Demo: Collect with approvals → Run now**: crawl mode asks before
   every step, the hazardous ones in red, then *Ask operator* asks whether to
   clear the counters. Meanwhile the COSMOS bell gets the approval alert.
4. **Demo: Health check → Dry run**: commands are checked against the COSMOS
   definitions but not sent; the run log shows the values that flowed in from
   the packet. Open a Send command node and try **Build command…** and the
   telemetry picker.
5. **Demo: Hot temperature watch** is armed in dry-run mode: its dry runs show
   up in **View → Run History** (filter *Only dry runs*) whenever TEMP1 goes
   over 40.
6. Edit a flow and **File → Save With Note**, then **File → History** to
   compare versions and restore one; **View → Audit Log** shows who did what.
7. **View → Alerts** to add a Slack or Teams webhook and send a test.

## Flows

A flow starts at a **trigger** and follows wires: each node finishes, then the
node its output is wired to runs. Every output takes one wire, except
Parallel, whose wires all run at once until they meet at a Join. Nodes that
can fail have an **error** output; if it's wired the run continues there
(with the message in the `error` variable), otherwise the run fails.

New and imported flows start **disarmed**; saving keeps a flow armed unless
the saved version has problems, which disarms it. Arm a flow to let its
triggers fire; **Run now** works either way. **Stop all** disarms every flow, stops every run and
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
`x if c else y`, lists, dicts and indexing (`hs['TEMP1']`, `items[0]`,
`name[0:3]`), variables by name, and functions:

| | |
| --- | --- |
| Telemetry | `tlm`, `tlm_raw`, `tlm_formatted`, `tlm_with_units` (or COSMOS check syntax: `INST HEALTH_STATUS TEMP1 > 50`) |
| Numbers | `abs`, `min`, `max`, `round`, `int`, `float`, `sqrt`, `floor`, `ceil`, `log10`, `sum`, `hex`, `format(x, '.2f')` |
| Text | `str`, `upper`, `lower`, `strip`, `replace`, `split`, `join(sep, list)`, `startswith`, `endswith`, `contains`, `len` |
| Lists, dicts | `list`, `sorted`, `reversed`, `range` (up to 10,000), `any`, `all`, `keys`, `values`, `get(container, key, default)` |
| Time | `now()`, `utc(seconds)`; in scheduled runs `time_to_aos()`, `time_to_tca()`, `time_to_los()` |
| Variables | `var(name, default)` |

There's no attribute access (`x.upper()`), import or lambda, so a condition
can't run arbitrary code; the functions above cover what methods would.

Text fields (commands, script names, log messages) take `{{ expression }}`:
`INST COLLECT with TYPE {{ collect_type }}, DURATION {{ round(tlm('INST HEALTH_STATUS TEMP1') / 10, 1) }}`.
A value inserted into a command can't change it: outside quotes it can't
contain commas, quotes or `with`, inside quotes it can't contain that quote,
or the step fails. Quote string values in the command (`NOTE '{{ note }}'`),
or turn on the node's *Allow raw values* when the value is meant to be command
text (a command parameter).

Every condition and text field has a telemetry picker (the satellite icon),
and Send command has **Build command…**, which opens COSMOS's Command Sender
form (states, ranges, required and hazardous checks come from the command
definition).

### Variables

A run's variables start from the flow's *starting variables* and template
parameters, and nodes add to them:

- **Set variable** sets one or more variables from expressions, in order.
- **Read packet** reads a whole packet into one variable (`hs['TEMP1']`).
- **Ask operator** stores the operator's answer.
- **Run script** sets `script_state`, and `script_result` (or the name you
  choose) if the script hands a value back:
  `stash_set(os.environ['FLOW_RESULT_KEY'], {'ok': True, 'temp': 21.5})`
  (Ruby: `stash_set(ENV['FLOW_RESULT_KEY'], ...)`). Scripts also get
  `FLOW_RUN_ID` and `FLOW_NAME`.
- Loops set their counter; an error output sets `error`; Send command sets
  `last_command`; triggers set `packet_count`, `limits_state`,
  `trigger_value`; scheduled runs get the `contact_*` variables.
- A **Sub-flow** gets the caller's variables and can copy its own back.
- **Parallel** branches share variables unless *Separate variables per branch*
  is on: then each branch works on a copy, their changes merge at the Join in
  wire order, and `branch_results` lists each branch's changes.

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
| Set variable | out, error | name, expression, more | Store one or more values |
| Read packet | out, error | packet, variable, type | Read every item of a packet into a variable |
| **Actions** | | | |
| Send command | out, error | command, allow hazardous | Send a command (checked and logged by COSMOS like any other) |
| Run script | out, error | file, wait, timeout, allow errors, environment | Start a script in Script Runner, optionally wait for it |
| Set telemetry | out, error | item, value, type | Set a current value |
| Log | out | message, level | Write to the COSMOS log |
| Notify | out | title, message, level | Alert operators (COSMOS bell, webhooks) |
| **Waits** | | | |
| Wait | out | seconds | Pause |
| Wait for telemetry | ok, timeout, error | condition, timeout, poll | Wait for a condition, or time out |
| Wait check | out, error | condition, timeout, poll | Wait for a condition; fail on timeout |
| Wait for packet | received, timeout, error | packet, count, timeout | Wait until new packets arrive (first telemetry after AOS) |
| **Contacts** | | | |
| Wait for contact | out, error | AOS/TCA/LOS, offset | Wait until a time relative to the run's pass |
| Contact time left | ok, short, error | seconds | Branch on whether that much of the pass is left |
| **Flow control** | | | |
| Ask operator | answered, timeout, error | question, answer type, choices, variable, default, timeout | Ask for a value or yes/no |
| Sub-flow | out, error | flow, copy variables back | Run another flow from its Manual trigger and wait |
| End | | success/failed, message | Finish the whole run |

## Dry runs

**Dry run** (toolbar, or a scheduled activity's *Rehearse*) runs a flow
without sending anything:

- Commands are checked against the COSMOS command definitions (unknown
  commands or parameters, required parameters, states, ranges) but not sent.
- Scripts, telemetry writes and Notify nodes are recorded, not done.
- *Skip waits* makes waits instant; Wait for telemetry, Wait check and Wait
  for packet assume their condition comes true, Ask operator uses its default.
- Approvals still ask, so a crawl or walk rehearsal walks through them.
- A run can use a **simulated contact** (AOS in N seconds, lasting M) or a
  real predicted pass, so contact templates can be rehearsed any time.

A flow's *Dry-run mode* setting makes every run of it a dry run, triggers
included: arm it to watch what it would do on live telemetry before it's
allowed to command. Dry runs are marked in every run list and never ring the
COSMOS bell.

## Version history

Every save is a numbered version with who saved it, an optional note
(**File → Save With Note**) and a summary of what changed. **File → History**
lists versions, shows how any version differs from the current flow (settings,
nodes added, removed and changed field by field), exports one, or restores
it, which saves it again as a new version, so nothing is lost. If someone else
saved the flow after you opened it, saving asks whether to reload theirs or
save yours over it (theirs stays in the history). Deleted flows keep their
history and runs: **File → Restore Deleted Flow**.

Every run records what started it (who pressed Run, which trigger and why,
which activity, rule and pass, or which flow called it), the version that ran,
its starting variables and whether it was a dry run. **View → Run History**
searches runs across every flow, including deleted ones. A run's version links
to that version in the history. Activities say when their template has changed
since they were scheduled (*template v3 → v5*).

## Alerts

**View → Alerts** sets where events go:

- **COSMOS notifications**, the bell in every COSMOS tool, linking back to the
  run. COSMOS shows *alerts* as toasts again on every page load until someone
  acknowledges them, so only events someone has to act on are alerts: an
  approval nobody has answered after *Alert about an approval after* seconds
  (default 30; someone in the tool answers sooner), a failed run nobody
  started by hand (triggers, activities), a missed or stuck activity, failed
  prediction, and Notify nodes set to alert. Runs you start yourself, Stop all
  and routine events only go to the bell. Dry runs never ring it.
- **Webhooks**: Slack or Teams incoming webhooks, or generic JSON for
  anything else. URLs are shown masked once saved. Set the COSMOS address so
  links in messages open the tool.

Events: approval waiting, run failed or stopped, activity missed, failed,
needing review, started or done, Stop all, scheduler paused, TLE stale,
prediction failed, and Notify nodes. Each destination picks its own events;
**Send test** checks a destination.

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
TLEs and records where each came from.

### Feeding TLEs from other apps

Paste a TLE in the satellite dialog, upload a file of them (the upload button
next to Satellites), or let another app keep them current through any of:

| Input | How | Source recorded as |
| --- | --- | --- |
| HTTP | `POST /flow-automator/api/tles` with `{"tle": "<any number of TLEs>", "source": "fds"}` and a COSMOS token; the response says what happened to each | `source` |
| COSMOS stash | from any COSMOS script or microservice: `stash_set("flow_automator_tles", {"tle": text, "source": "fds"})` (or just the text, or a list); the outcome appears in `flow_automator_tles_result` | `source`, or `stash` |
| Bucket file | list target paths (e.g. `INST/tle/latest.tle`) in **TLE inputs**; write them with `put_target_file(...)` or Bucket Explorer; read again whenever they change | `file:<path>` |
| Telemetry | list packets in **TLE inputs**: one item holding the TLE text, or line 1 and line 2 items; read whenever the packet's count goes up | `tlm:<TGT PKT>` |

Text may hold any number of 2- or 3-line TLEs. Each is matched to a satellite
by catalog number (the satellite dialog shows it; it's set from the first TLE),
then by name. Every TLE from these inputs must pass these checks:

- **catalog number** matches the satellite's (else *rejected*),
- **epoch** is newer than the current TLE's (else *skipped*: an old file or a
  cached download can't roll the orbit back),
- the satellite's **preferred source**, if set, wins while its TLE is fresher
  than `stale_tle_days` (others are *skipped*),
- it doesn't move the satellite more than the **jump limit** (100 km by
  default) from where the current TLE puts it now: otherwise it's *held* for
  review, alerting, and waits for **Accept** or **Reject** in the satellite
  dialog (a maneuver, or a wrong TLE?).

Pasting in the satellite dialog is an operator's decision and applies as is.
**TLE inputs** (the import button next to Satellites) sets the stash key, files,
packets, jump limit and polling interval, and shows each input's last result.
Every TLE that applies re-times scheduled activities like any other update,
and the audit log records every batch.

### Templates

Any flow with a Manual trigger can be scheduled. Turn on **Template for
scheduled activities** in its flow settings to list it first and declare
**parameters**: text (with an optional pattern and maximum length), number
(minimum, maximum, whole numbers), yes/no, choice, telemetry item, command or
list, each with a default and a description. Item and command parameters get
the pickers in the Run and Schedule dialogs. Parameters become run variables
of the same name. Runs started by an activity also get:

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

Developer documentation lives in [`docs/`](docs/):
[ARCHITECTURE.md](docs/ARCHITECTURE.md) (how it works inside),
[DECISIONS.md](docs/DECISIONS.md) (why it's built this way) and
[DEVELOPMENT.md](docs/DEVELOPMENT.md) (build, install, test, recipes).
Agents start at [CLAUDE.md](CLAUDE.md).

Layout:

- `lib/flow_automator/`: engine (`engine.py`, `runner.py`, `graph.py`,
  `expressions.py`, `triggers.py`), contacts (`orbit.py`, `contacts.py`,
  `templates.py`, `tle_sources.py`), `audit.py`, `http_api.py`, `store.py`
- `microservices/FLOW_ENGINE/flow_engine.py`: the COSMOS microservice
- `src/tools/flowautomator/`: the Vue tool

Build the gem either way:

```
rake build VERSION=X.Y.Z                    # with local Node, pnpm and Ruby: builds the tool, then the gem
./build_tool.sh                             # without them: builds the tool (local pnpm, or the openc3-node container)
/path/to/cosmos/openc3.sh cli rake build VERSION=X.Y.Z   # then packages it in the COSMOS container
```

`openc3.sh cli` runs rake in the cmd-tlm-api container, which has no Node, so it
packages the tool already built in `tools/flowautomator` instead of building
it. It checks the build matches the current source (`pnpm run build` writes a
hash of it to `tools/flowautomator/.source-hash`) and stops with a message if
the tool hasn't been built or the source changed since.

Both pnpm 10 and 11 install from the lockfile. `pnpm-workspace.yaml` pins the
d3 packages Vue Flow uses to their long-standing releases: new releases of all
of them appeared on 2026-11-03, and pnpm 11 refuses packages that new (its
`minimumReleaseAge` policy).

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
- Predicting 50 satellites × 20 stations for 7 days takes about a minute in
  the background after a restart or a full recompute.
- No built-in internet TLE fetch (CelesTrak, Space-Track) yet; times are UTC; stations have one elevation
  mask, not a horizon profile.
