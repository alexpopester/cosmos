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
| 3 | Real telemetry into COSMOS + replay | Flows acting on real values; recorded passes as a regression set | Not started |
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

- A COSMOS interface that polls the SatNOGS DB API for 2–3 well-documented
  satellites, decodes frames with `satnogs-decoders`, and publishes them as
  COSMOS packets (with matching packet definitions). Flows anchor at LOS
  plus a delay because of the upload lag.
- A replay target that plays a recorded pass (observation timing plus its
  frames) at real time against a crafted TLE that puts the pass "now", for
  repeatable regression runs on real data.

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
  These are backlog items B43 and B45 (EVALUATION-2026-11-05 section 2.3),
  now reproduced on real data; the soak's start-lateness numbers will show
  whether the load delays runs.

**Recording since 2026-11-05 20:19 UTC.** First read: 16 TLEs, 7,063
predicted passes at the soak stations, 4,571 activities.

**Upgrading mid-soak:** `realdata/soak.sh install <gem>` installs a new
plugin version without reloading the demo and records the engine restart as
a maintenance window in the manifest. The report leaves activities that
overlap a window out of the scheduler scores (runs interrupted by a restart
stop, by design: D25). 1.13.2 was installed this way at 21:07 UTC; the 4
runs in progress stopped.
