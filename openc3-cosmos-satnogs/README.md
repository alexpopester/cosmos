# SatNOGS telemetry for COSMOS

Real satellite telemetry in COSMOS: frames that volunteer ground stations of the
[SatNOGS network](https://satnogs.org) received from real satellites, decoded and
delivered as COSMOS packets. Built to test automation (the Flow Automator) on real
spacecraft data; see `openc3-cosmos-flowautomator/docs/REAL-DATA-TESTING.md` step 3.

## What you get

| Target | Satellite | Packets |
|---|---|---|
| `CANVAS` | CANVAS (CU Boulder / LASP), NORAD 68635 | `APID_32` beacon: command counters, flight software version and state, storage, ADCS, power and temperatures (142 fields) |
| `AEPEX` | AEPEX (CU Boulder / LASP), NORAD 68506 | `APID_1` beacon: command counters, mode, power switch states, battery, radio (154 fields) |
| `CATSAT` | CatSat (University of Arizona), NORAD 60246 | `TYPE_1` … `TYPE_17`: GomSpace power system, radio, ADCS and payload beacons |

Every target also has `OTHER` (frames that decode only as far as the header)
and `UNDECODED` (frames the decoder can't read; the raw bytes are kept).

Every packet carries `FRAME_TIME` and `PACKET_TIME` (when the station received
the frame), `OBSERVATION`, `STATION`, `STATION_ID`, `NORAD`, `FRAME_BYTES` and
`FRAME_HEX`. Values are raw: the decoders give no units or conversions.

## How it works

The `SATNOGS_INT` interface polls the SatNOGS Network API every
`satnogs_poll_seconds` (default 15 minutes) for observations of each satellite
that ended within `satnogs_lookback_hours`. It downloads their decoded-frame
files and decodes them with [satnogs-decoders](https://gitlab.com/librespacefoundation/satnogs/satnogs-decoders),
then hands each frame to COSMOS as a JSON packet (`JsonAccessor`), oldest first.
A frame is delivered once; after a restart, frames older than the newest
`FRAME_TIME` COSMOS already has are skipped.

**Timing:** stations upload frames after the observation, so packets arrive
minutes to hours after the pass. COSMOS logs them by `PACKET_TIME`, so graphs
and the data extractor put them where they happened. Automation should treat
this as post-pass data, not live telemetry.

**Rate limits:** SatNOGS is volunteer run. Anonymous API use is about 60
requests an hour, and each poll makes 1–4 per satellite. For more, create a
SatNOGS API token as the COSMOS secret `SATNOGS_TOKEN` and add
`SECRET ENV SATNOGS_TOKEN SATNOGS_TOKEN` to the interface in `plugin.txt` (it's
left out by default because COSMOS logs an error for a missing secret).

It's receive only: the targets have no commands.

## Adding a satellite

1. Find its decoder in SatNOGS DB (`https://db.satnogs.org/api/satellites/?norad_cat_id=N`,
   `telemetries[].decoder`).
2. Add it to `SATELLITES` in `tools/generate_definitions.py` with the field that
   tells its packets apart, and run it (internet and `pip install satnogs-decoders`):
   it writes `targets/<TARGET>/cmd_tlm/tlm.txt` from real frames, and fixtures for the tests.
3. Add `TARGET`, `MAP_TARGET` and `OPTION SATELLITE` lines to `plugin.txt` and a `target.txt`.

## Develop

```bash
# tests (no COSMOS needed): uv, or the python:3.12-slim image
pip install pytest satnogs-decoders requests && python -m pytest test
# build (bump the version and add a CHANGELOG.md entry first)
../openc3.sh cli rake build VERSION=X.Y.Z
```

## Licenses

This plugin is MIT. `satnogs-decoders` is AGPL-3.0: COSMOS installs it from PyPI
when the plugin is installed (`requirements.txt`); it isn't bundled here.
SatNOGS data is published under CC BY-SA 4.0.
