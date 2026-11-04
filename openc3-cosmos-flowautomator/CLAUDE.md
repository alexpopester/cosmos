# CLAUDE.md — Flow Automator plugin

Guidance for agents (and humans) working in `openc3-cosmos-flowautomator/`.
The repo-root `CLAUDE.md` still applies; this file adds what is specific to
this plugin.

## What this is

A standalone COSMOS plugin (gem) with two halves:

- **FLOW_ENGINE**: a Python microservice (`microservices/FLOW_ENGINE/`,
  `lib/flow_automator/`) that stores flows in Valkey, keeps triggers armed,
  runs flows server side, predicts satellite passes and runs scheduled
  activities. HTTP API at `/flow-automator/api/*`.
- **Flow Automator tool**: a Vue 3 / Vuetify / Vue Flow editor and live view
  (`src/tools/flowautomator/`), with Flows and Contacts views.

## Read first

| For | Read |
| --- | --- |
| What it does, every node and feature (operator view) | `README.md` |
| How it's built: threads, locks, data model, run lifecycle | `docs/ARCHITECTURE.md` |
| Why it's built that way; things not to undo casually | `docs/DECISIONS.md` |
| What changed in each version | `CHANGELOG.md` |
| Known issues and the prioritised improvement backlog | `docs/EVALUATION-2026-11-04.md` |
| Build, install, test, recipes for common changes | `docs/DEVELOPMENT.md` |
| Every API endpoint | docstring at the top of `lib/flow_automator/http_api.py` |
| Flow JSON shape | docstring of `lib/flow_automator/graph.py` |

## Rules

- **No COSMOS core changes.** Everything stays in this plugin; work around
  core bugs here (DECISIONS D1, D9). Ask the owner before touching core.
- **Keep the two node registries in sync**: `graph.py` `NODE_TYPES` +
  `runner.py` `EXECUTORS` (engine) and `src/tools/flowautomator/nodeTypes.js`
  (editor). Same type names, output handle names and data keys.
- **Only `cosmos_backend.py` (and the microservice) import `openc3`.** Any new
  COSMOS call gets a method on `CosmosBackend` and on `FakeBackend` in
  `test/conftest.py`.
- **Lock order is planner → engine.** Never call into `ContactPlanner` while
  holding `FlowEngine.lock`.
- **Waits must be cancellable**: use `run.sleep()` or short `Event.wait`
  loops checking `run.cancelled()`; never a bare `time.sleep` in a run.
- **Side effects must respect dry runs** (`run.dry_run`, `run.skip_waits`).
- **Expressions stay safe**: no attribute access, no eval; new functions go
  in `_SAFE_FUNCTIONS` and must be pure and bounded.
- **Values never change commands**: use `render_command` for anything that
  ends up in a command string.
- **Alerts are for action** (D15). Default new events to `notification`.
- Don't revisit the agreed design choices in DECISIONS.md unless the owner
  asks. "Go & run rest" still asking for hazardous commands is deliberate.

## Commands (run from this folder)

```bash
# Engine tests (no COSMOS). No uv on the dev box? use the docker form in docs/DEVELOPMENT.md
uv run --no-project --with pytest --with requests --with sgp4 --with numpy python -m pytest test

# Lint (CI fails on any eslint warning)
pnpm lint --max-warnings 0
ruff check --config ../openc3/python/pyproject.toml lib microservices test

# Build: tool, then gem. Always bump VERSION (COSMOS caches by version)
./build_tool.sh && ../openc3.sh cli rake build VERSION=X.Y.Z

# Install newest local gem + demo into a local COSMOS, load demo data
COSMOS_PASSWORD=... demo/setup_demo.sh --reinstall

# Browser tests against a live COSMOS (Playwright in docker)
COSMOS_PASSWORD=... test/ui/run.sh [one_test.mjs]
```

## Definition of done for a change

1. pytest passes (167+ tests) and new behaviour has a test.
2. `pnpm lint --max-warnings 0` and ruff are clean.
3. For UI-visible changes: built, installed on the local COSMOS with a new
   version, checked in the browser (or the relevant `test/ui` script).
4. README updated for anything an operator notices; `docs/` for anything a
   developer needs; a new DECISIONS entry for a new design choice.
5. `CHANGELOG.md` updated: add the change under `## [Unreleased]`, and when
   you bump the version, rename that heading to `## [X.Y.Z] - YYYY-MM-DD` in
   the same commit. `rake build` refuses a VERSION with no changelog entry.
6. Conventional commit with scope `flow-automator`.

## Local environment notes

- Local COSMOS runs in docker (`cosmos-openc3-*` containers), started with
  `../openc3.sh`; never bare `docker compose`.
- The plugin is installed in scope `DEFAULT`. The demo and the browser tests
  only touch what they create; don't delete other flows, satellites or
  stations you find there.
- Browser tests need host networking (`--network host`) in the Playwright
  container; `run.sh` handles it.
