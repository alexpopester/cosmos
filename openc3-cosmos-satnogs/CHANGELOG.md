# Changelog

## [Unreleased]

## [0.1.2] - 2026-11-05

### Fixed
- **Frames reach COSMOS while a poll is still running**: one observation at a
  time, with 8 downloads in parallel. A single CANVAS pass heard by 13 stations
  is over 1,000 frame files, and the first poll used to download all of them
  one by one before delivering anything.
- **A beacon heard by several stations is delivered once** (same bytes within
  2 minutes); the copies are counted in the poll stats.
- **A station's late upload is no longer dropped** when another station's
  newer frames came first. Only frames older than what COSMOS had at startup
  are skipped.

## [0.1.1] - 2026-11-05

### Fixed
- **CANVAS and AEPEX got no telemetry.** SatNOGS lists observations newest
  first, including ones booked days ahead; for satellites with many bookings
  the page budget went on those and never reached frames. Only observations
  already over are asked for now.
- The optional `SATNOGS_TOKEN` secret is opt-in: COSMOS logged an error at
  startup when it didn't exist.

## [0.1.0] - 2026-11-05

### Added
- First version: CANVAS, AEPEX and CatSat telemetry from the SatNOGS network,
  decoded with satnogs-decoders. Packet definitions generated from 1,600 real
  frames (`tools/generate_definitions.py`).
