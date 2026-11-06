# Real-data testing plan

How we prove the Flow Automator's contact scheduling against real satellites
and real ground stations, rather than only against data we generate
ourselves. Started 2026-11-05. Keep the status column current.

## Why, and the limit

The browser tests and the demo use TLEs we make up and stations we place
under the track. That proves the code agrees with itself, not with reality.
We want evidence from real passes over real stations, and real telemetry.

We can't command a real satellite (that needs our own spacecraft, or an
amateur licence and radio hardware). So the proof comes in two halves:

| Half | Source | How real |
|---|---|---|
| Pass geometry, downlink, scheduling | [SatNOGS](https://satnogs.org) global ground-station network | Real satellites, real stations, real received frames |
| Command loop (send, verify, react) | [NASA NOS3](https://github.com/nasa/nos3) | Real cFS flight software flying a real TLE |

## Data sources

**SatNOGS Network API** (`https://network.satnogs.org/api/`). Read access
works without a token; anonymous requests are rate limited (roughly 60
observation requests an hour), a token raises it.

- `observations/?status=good&norad_cat_id=N&start=..&end=..` returns 25 per
  page, cursor paginated (`Link: rel="next"`). Each observation carries:
  - `start`, `end`: the scheduled window (normally the full pass above the
    station's `min_horizon`, unless the user scheduled part of a pass)
  - `tle0..2` and `tle_source`: the exact TLE SatNOGS used
  - `station_lat`, `station_lng`, `station_alt`
  - `rise_azimuth`, `set_azimuth`, `max_altitude` (integer degrees)
  - `demoddata`: one file per decoded frame, named
    `data_<obs id>_<YYYY-MM-DDTHH-MM-SS>`, so the file names give the time
    each real frame was received
- `stations/` returns every station in one response (about 4,500), with
  `min_horizon`, `min_culmination`, `lat`, `lng`, `altitude`.

**SatNOGS DB API** (`https://db.satnogs.org/api/`): decoded telemetry frames
per satellite (`telemetry/`). Decoders: the `satnogs-decoders` Python
package (Kaitai Struct). Frames are uploaded after the pass, from minutes to
hours later, so they can't drive a flow live during that pass.

**CelesTrak / Space-Track**: current TLEs, already supported (D29).

SatNOGS is a volunteer network. Cache everything, keep request rates low,
and don't book observations on other people's stations at scale.

## Steps

| # | Step | What it proves | Status |
|---|---|---|---|
| 1 | Predictions vs SatNOGS observations | `orbit.py` pass geometry against real passes and real frame times | Done 2026-11-05 (see Results) |
| 2 | Real-pass scheduling soak | Planner, rules, retiming and capacity on live passes over real stations | Recording since 2026-11-05 (final report after ≥ 1 week) |
| 3 | Real telemetry into COSMOS + replay | Flows acting on real values; recorded passes as a regression set | Done 2026-11-06: 3a live telemetry, 3b post-pass checks, 3c replay regression (9/9) |
| 4 | NOS3 command loop | Contact flows commanding real cFS on a real orbit | Not started |

### Step 1: predictions vs SatNOGS observations

A command-line checker in `realdata/` (not packaged into the gem). For a
sample of recent `good` observations it uses SatNOGS's own TLE and station
position, so it tests our propagation and look-angle code on its own, and
reports:

1. **Window**: our predicted AOS/LOS above the station's `min_horizon`
   against the observation's `start`/`end` (full-pass observations only;
   partial ones are reported separately).
2. **Geometry**: our maximum elevation in the window against `max_altitude`,
   and our azimuth at start/end against `rise_azimuth`/`set_azimuth`.
3. **Real signal**: our elevation at every decoded frame's receive time.
   A frame received while we say the satellite is below the horizon (beyond
   a refraction tolerance) is hard evidence of a prediction error.

Expected differences, not bugs:
- SatNOGS uses PyEphem with atmospheric refraction; `orbit.py` is geometric.
  Near the horizon refraction lifts the satellite about 0.5°, so their AOS is
  a few seconds earlier and their LOS a few seconds later than ours.
- `max_altitude` and the azimuths are rounded to whole degrees.
- Station clocks are usually NTP synced, but not always.

Output: a summary on stdout (median, 95th percentile and worst case per
metric, plus every flagged observation) and a JSON report. API responses are
cached under `realdata/cache/` (git-ignored) so reruns are free, and the
cache doubles as the start of the step 3 recording set.

Done when: the checker runs over at least 200 observations across many
satellites and stations, the results are written up below, and any real
disagreement has either a fix or a DECISIONS entry explaining it.

### Step 2: real-pass scheduling soak

What's set up (`realdata/soak_setup.py`; everything is named `Soak: ...`):

- **18 real satellites** (`soak_common.SATELLITES`): the most-heard named
  satellites from the step 1 sample that CelesTrak publishes, including
  KNACKSAT-2 and CATSAT (fast decay), METEOR M2-3 (higher orbit), LASARSAT
  (45° inclination) and the ISS. TLEs come from CelesTrak through the
  engine's own fetcher (D29), so TLE ageing and updates are the real thing.
  Temporary analyst catalog numbers (80000–99999) are left out: CelesTrak
  doesn't publish them (SatNOGS gets them from Space-Track).
- **13 real SatNOGS stations** (`soak_common.STATIONS`): busy, online,
  spread over six continents (41° S to 65° N), at their real position and
  `min_horizon`, capacity 1.
- **Template "Soak: Pass log"**: log before AOS, wait for AOS, read INST
  HEALTH_STATUS, wait for TCA, check the time left, wait for LOS − 5 s, log.
  It sends nothing, but each run stays open for the whole pass, so many run
  at once.
- **One rule**: that template into every pass of those satellites over those
  stations, AOS − 60 s, run mode, stop at LOS, priority 20, skipping
  stations already at capacity.

The recorder (`realdata/soak_recorder.py`, in a container that restarts
with Docker) reads the schedule every 5 minutes and appends what changed to
`realdata/cache/soak/events-*.jsonl`. It logs TLE updates, passes
predicted, moved or dropped, and activity changes, plus hourly status. It
pairs a pass the planner re-predicts under a new id (a move of more than 5 s)
with its earlier prediction, so the drift is measured, not hidden.

The report (`realdata/soak_report.py`) fetches every good SatNOGS observation
of the soak satellites since the start (any station) and reports:

1. Operational accuracy: frames and windows against our prediction from the
   TLE we actually held at the time, by TLE age.
2. Coverage at the soak stations: SatNOGS-heard passes we predicted,
   scheduled and ran.
3. Scheduler behaviour: outcomes, start lateness, retimes and drift, misses,
   failures and cancellations, recorder errors.
4. Prediction churn: how far AOS moved between the first prediction and the
   pass, by lead time.

```bash
cd openc3-cosmos-flowautomator
COSMOS_PASSWORD=... realdata/soak.sh setup   # create it; stores a session token (not the password)
realdata/soak.sh start                       # recorder container
realdata/soak.sh status
realdata/soak.sh report                      # any time; the final one after at least a week
realdata/soak.sh stop                        # end; COSMOS_PASSWORD=... realdata/soak.sh clear removes it
```

Watch for: retiming on TLE updates (D12), rule pre-emption (D23), station
capacity (D14), activity pruning, engine memory and Valkey growth, alerts
noise.

### Step 3: real telemetry and replay

**3a. Real telemetry in COSMOS (built 2026-11-05):** the standalone plugin
`openc3-cosmos-satnogs` (repo root; see its README).

- Source: the public SatNOGS Network API, plus the frame files on each
  observation. The DB telemetry API needs a token, and these don't.
- Satellites: CANVAS and AEPEX (CU Boulder / LASP; CCSDS beacons with command
  counters, mode, power switches, battery, temperatures) and CatSat (GomSpace
  power, radio and ADCS beacons, 14 types). All three are in the soak, so
  their passes are also scheduled. A survey of 10 soak satellites with
  decoders picked them: 97–100% of their frames decode, against 1% for
  FRONTIERSAT and 42% for REAL.
- Packet definitions are generated from 1,600 real frames
  (`tools/generate_definitions.py`). There's one packet per APID or beacon
  type with real content, plus OTHER (header only) and UNDECODED (raw bytes
  kept). Values are raw: the decoders have no units.
- `PACKET_TIME` is when the station received the frame, so a pass's data
  lands where it happened even though it arrives up to hours later.
- Found on the first live run (fixed in 0.1.1): SatNOGS lists observations
  booked days ahead first, which used up the page budget for CANVAS and
  AEPEX. Only finished observations are asked for now.

**3b. Post-pass checks on real values (built 2026-11-05):**
`realdata/realtlm_setup.py` creates one template per satellite, named
"RealTLM: <SAT> post-pass" (like a mission's own post-pass procedure), and a
rule that runs it after every pass at two soak stations that work that
satellite. They act as its ground stations:

| Satellite | Stations | Healthy means (raw values) | Seen Nov 2026 |
|---|---|---|---|
| CANVAS | EU1AEM, MAUSyagi | rejected cmds 0, fail code 0, FP errors 0, ADCS alive 2, battery temps 2000–2250 | 0, 0, 0, 2, 2108–2126 |
| AEPEX | MAUSyagi, MAUSyagi-AK | rejected 0, fail code 0, mode 1, battery and UHF alive, uplink < 2 days ago | 0, 0, 1, alive, 4–8 h |
| CatSat | JH4XSY, EU1AEM | battery ≥ 14,500 mV, no latch-ups, board temp 300–420 | 15,576–16,313, 0, 352–382 |

Each check:
1. starts 30 s before LOS (rules can't start activities after LOS) and runs
   on past it;
2. waits up to 90 min for a beacon heard at or after this pass's AOS, from
   any SatNOGS station;
3. logs who heard it, when and the key values;
4. raises an alert naming what's off, or a notification when nothing was
   heard.

The recorder logs each check's outcome (healthy, anomaly or not heard) with
its summary, and the soak report adds them as section 5.

Limits:
- **The check reads the latest beacon, not this pass's.** If a later pass's
  frames arrive first, it checks those, which is still a health check within
  an orbit. A per-pass archive would need a history query node.
- **Nothing compares one pass with the last** (for example a command counter
  that moved without an uplink): runs have no memory between them.

**3c. Replay regression on recorded real passes (built 2026-11-06):**

- **Recordings:** one real pass per satellite (`openc3-cosmos-satnogs/lib/recordings`,
  made with `tools/record_pass.py`):
  - CANVAS: 204 frames, 10 min, heard by F1SXJ
  - AEPEX: 140 frames, 10 min, heard by mpippins
  - CatSat: 68 frames, 6.5 min, heard by DUS-NORD
- **Replay:** the plugin's `REPLAY PLAY` command plays a recording into its own
  `REPLAY_<SAT>` target as if the pass were happening now. It can run faster,
  arrive as one upload after LOS or live, have decoded fields overridden (a
  fault injected into real data), or lose some or all frames.
- **Runner:** `realdata/replay_test.py` replays each recording three ways and
  runs the step 3b post-pass template (reading the replay target) against a
  contact timed to match:

| Case | Expected | 2026-11-06, 10x |
|---|---|---|
| as recorded | healthy | 3/3 pass |
| injected fault (CANVAS rejected commands 3, AEPEX mode 3, CatSat battery 14,000 mV) | an alert naming it | 3/3 pass |
| lost pass (every frame dropped) | not heard | 3/3 pass |

```bash
cd openc3-cosmos-flowautomator
realdata/soak.sh replay              # 10x, about 6 min
realdata/soak.sh replay --speed 1    # real time, about 35 min
```

**Found while building it (fixed in openc3-cosmos-satnogs 0.2.1):** COSMOS
keeps the last packet delivered, and a station's late upload of an older pass
can arrive after a newer one. Seen live: the latest CANVAS beacon went from
22:59 back to 21:20. So a post-pass check waiting on `FRAME_TIME` could miss its
pass. Every packet now also carries `NEWEST_FRAME_TIME`, which never goes back,
and the checks wait on that.

### Step 4: NOS3 command loop

- Run NOS3 without its own ground software; install its command/telemetry
  definitions as a plugin in our COSMOS and connect to its radio ports.
- Configure 42 to fly a real satellite's TLE starting at the current UTC, at
  1x speed, and gate the link with a small proxy that only passes traffic
  while a station can see the spacecraft (computed from 42's position, not
  from our planner).
- Contact templates that command cFS (NOOP, counters, mode changes, dumps)
  and react to resets and early LOS.

## Open questions

- A SatNOGS API token for higher rate limits (step 2 onwards)?
- Which satellites for steps 2–3: ones with good decoders and frequent
  frames.
- Whether to add a sim-time clock to the engine later (needed only if
  real-time soaks prove too slow).

## Results

### Step 1 (2026-11-05)

Run: `realdata/satnogs_check.py`, three batches of 250 `good` observations
(the most recent, before 2026-10-20 and before 2026-10-01), each checked with
the TLE SatNOGS recorded for it.

**Sample:** 750 observations, 172 satellites, 182 stations, 590 full passes,
13,436 decoded frames in 497 observations.

**Real signal: 0 of 13,436 frames** arrived while we predicted the satellite
was below the horizon. The lowest elevation at which we predicted any frame
was heard is −0.01° (DJIBOUTI-1B at MAUSyagi), right on the horizon and
within what refraction allows (about −0.5°). The next lowest are 0.05° and
0.38°, so frames heard at the very edge of a pass line up with our horizon
crossing.

| Metric (ours − SatNOGS) | n | Median | 95th pct abs | Max abs |
|---|---|---|---|---|
| AOS, full passes (s) | 590 | −0.5 | 1.7 | 20.7 |
| LOS, full passes (s) | 590 | −0.5 | 1.2 | 9.2 |
| Max elevation (°) | 750 | −0.04 | 0.48 | 0.84 |
| Rise azimuth, full passes (°) | 590 | 0.0 | 0.5 | 0.9 |
| Set azimuth, full passes (°) | 590 | 0.0 | 0.5 | 0.6 |

SatNOGS rounds max elevation and azimuths to whole degrees, so up to 0.5° is
rounding. The AOS/LOS tails are a handful of windows that started late or
ended early (one edge moves, the other matches).

**Independent check:** for every KNACKSAT-2 pass, `orbit.py` AOS matched
skyfield (same TLE) to within 0.3 s.

**SatNOGS record quirks found** (the checker reports them as notes, not
errors, and keeps them out of the timing statistics):

- **A recorded TLE isn't always the one the window was planned from.**
  KNACKSAT-2 (decaying fast, ṅ = 0.00116 rev/day²): 11 observations at
  different stations were all 8 s late against their recorded TLE (epoch
  day 308.70) with exactly the right length. Against the previous TLE (day
  307.30) every one of their edges sits on the station mask (5.0/5.0°,
  10.0/10.0°). So the windows were planned with the older TLE and the record
  shows the newer one. Reported as `window_shifted`.
- **Many windows use another horizon than the station's current mask**
  (170 of 750): manual schedules, or the mask changed since. Our elevation at
  both edges lands on a whole degree (often 0.00 ± 0.05°), which is itself a
  good match. Reported as `scheduled_horizon`.
- **A few schedulers use refracted elevations** (5): edges sit about 0.5°
  below the geometric horizon, which is standard refraction at 0°. Reported
  as `refracted_horizon`.

**Still flagged (3):** window shape anomalies, none involving frames. In each,
the edge elevations look like another TLE mismatch combined with a manual
horizon. Not investigated further:
- METEOR M2-3 @ Piszkesteto VHF (edges 9.98°/10.45°)
- NIS-PQ @ Dunchurch (edges +0.57°/−0.63°)
- OBJECT C @ Piszkesteto UHF (window 10 s shorter than the pass)

**Conclusion:** pass geometry in `orbit.py` matches real passes to about
a second and matches what stations actually heard. No code change needed.

**What this means for operations:** for a fast-decaying satellite, a TLE a
day old is already about 8 s out along track. Pass times are only as good as
the TLE, which supports keeping automated TLE fetching on (D29) and retiming
activities on TLE updates (D12). Step 2 should measure this drift directly.

### Step 2 (started 2026-11-05)

**Found while setting up, fixed in 1.13.1:**

- **One satellite CelesTrak doesn't publish blocked TLEs for all the
  others.** CelesTrak answers HTTP 404 "No GP data found" for catalog numbers
  it doesn't publish, and the engine treated that as the whole source
  failing. On the first setup, 8 of 18 picks had temporary analyst numbers
  (98xxx–99xxx), so 2 satellites got TLEs and 16 never would. The unit tests
  had missed it because their CelesTrak double answered 200; it now answers
  what CelesTrak really does.
- **CelesTrak gives intermittent 500s for single catalog numbers** (5 of 17
  in one sweep, all fine on retry). A per-satellite server error now leaves
  the others alone and is retried in 10 minutes instead of the 4-hour
  interval.
- The planner gives a pass a new contact id when a re-prediction moves its
  TCA more than 5 s (activities still follow it, by `retime_tolerance`).
  Anything tracking passes by id must pair them up again; the recorder does.

**Found after the fix:**

- **A re-added satellite can wait hours for its first TLE, with no reason
  shown** (fixed in 1.13.2: the engine keeps the last answer per catalog
  number and applies it at once; a satellite still waiting shows when).
  The fetcher remembers when each catalog number was last fetched,
  even after its satellite is deleted (that's what keeps us within
  CelesTrak's rate rules). Deleting and re-adding FRONTIERSAT and ERNST left
  them with no TLE until their catalog's next normal fetch (up to 4 h), and
  their fetch status was empty.
- **Load at realistic scale.** 18 satellites × 13 stations over the 7-day
  horizon is 10,325 contacts and 4,571 scheduled activities. `/schedule` is
  10.8 MB and takes 3 s, and the idle engine uses a steady ~20% of a CPU.
  These are backlog items B43 and B45 (EVALUATION-2026-11-05 section 2.3).
  Profiling at this scale showed worse: one scheduler tick took 4–10 s with a
  few clients polling, because every capacity check scanned every activity.
  **Fixed in 1.14.0** (D30): on the live soak, the Contacts view's day now
  loads in 64 ms (159 KB on the wire, was 1.4 s and 6 MB), `/status` takes
  7 ms (was 0.3–0.4 s) and the operator's CPU fell from ~40% to ~23%.
  `realdata/bench_soak.py` reproduces the soak in memory for these numbers.

**Recording since 2026-11-05 20:19 UTC.** First read: 16 TLEs, 7,063
predicted passes at the soak stations, 4,571 activities.

**Upgrading mid-soak:** `realdata/soak.sh install <gem>` installs a new
plugin version without reloading the demo and records the engine restart as
a maintenance window in the manifest. The report leaves activities that
overlap a window out of the scheduler scores (runs interrupted by a restart
stop, by design: D25). 1.13.2 was installed this way at 21:07 UTC (the 4
runs in progress stopped), and 1.14.0 at 21:31 UTC.
