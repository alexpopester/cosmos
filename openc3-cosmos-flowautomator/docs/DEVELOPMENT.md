# Developing the Flow Automator

Build, test, install and change the plugin. Read
[ARCHITECTURE.md](ARCHITECTURE.md) first for how the pieces fit, and
[DECISIONS.md](DECISIONS.md) before changing a design choice.

## Prerequisites

- A local COSMOS started with `./openc3.sh` from the repo root (Core 7.4.1 is
  what this was built against). Don't start its containers with bare
  `docker compose`: `openc3.sh` sets the UID/GID and env the containers need.
- Docker. Everything else can run in containers:
  - Python tests: `uv` locally, or the `python:3.12-slim` image.
  - Tool build: local Node 24 + pnpm, or COSMOS's `openc3-node` image
    (`build_tool.sh` picks).
  - Gem packaging: local Ruby, or `openc3.sh cli rake build`.
  - Browser tests: `mcr.microsoft.com/playwright:v1.63.0-noble`.

## The edit → install loop

1. **Engine change** (`lib/`, `microservices/`): run the pytest suite (below).
2. **Tool change** (`src/`): `pnpm lint --max-warnings 0`, then build.
3. **Build the gem** with a new version every time (COSMOS caches gems by
   version, so reusing one installs the old content):

   ```bash
   ./build_tool.sh                                   # builds tools/flowautomator
   ../openc3.sh cli rake build VERSION=1.7.1         # packages the gem
   # or, with local Node/pnpm/Ruby:  rake build VERSION=1.7.1
   ```

   Old `.gem` files pile up in this folder; they are git-ignored.

4. **Install it**, either:
   - Admin Console → Plugins → upload (choose *upgrade* on the existing one),
   - `COSMOS_PASSWORD=... demo/setup_demo.sh --reinstall` (loads the newest
     local gem into scope DEFAULT, plus the demo plugin if missing), or
   - `docker cp` the gem into the cmd-tlm-api container and run
     `ruby /openc3/bin/openc3cli load /tmp/<gem> DEFAULT` there (this is what
     `setup_demo.sh` does). Always pass the scope; don't use `cliroot`.

5. **Check it's up**: `curl -H "Authorization: $TOKEN"
   'http://localhost:2900/flow-automator/api/status?scope=DEFAULT'`, where the
   token comes from `POST /openc3-api/auth/verify {"password": "..."}`.
   Engine logs: `docker logs -f cosmos-openc3-operator-1 2>&1 | grep -i flow`
   (or Admin → Microservices / the COSMOS log viewer).

Reinstalling restarts the microservice: runs in progress fail, armed flows
re-arm, contacts are re-predicted (takes a few seconds for a handful of
satellites).

### Live tool development (optional)

`pnpm serve` rebuilds into `tools/flowautomator` on every change with the
COSMOS dev-server plugin on port 2999. Point COSMOS at it with a single-spa
import-map override for the tool (the repo's `new-tool` skill walks through
it). The engine still needs a gem install.

## Tests

### Engine (pytest, no COSMOS needed)

```bash
# with uv
uv run --no-project --with pytest --with requests --with sgp4 --with numpy python -m pytest test

# without uv (what works on the NixOS dev box)
docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/p:ro" -w /p python:3.12-slim \
  sh -c 'pip install -q --user pytest requests sgp4 numpy && python -m pytest test -q -p no:cacheprovider'
```

167 tests, ~25 s. `test/conftest.py` provides:

- `FakeBackend`: a dict of telemetry (`values`, `limits`, `counts`),
  recorded `commands`, scripted Script Runner states, a stash and bucket
  files. Set `hazardous`, `fail_commands`, `bad_commands` to exercise error
  paths.
- `engine` fixture: a `FlowEngine` on `MemoryRedis`, started without the
  background threads, so tests drive `engine.triggers.tick()` and
  `engine.planner.tick()` themselves with a fake clock where needed.
- helpers `node()`, `edge()`, `make_flow()`, `run_to_end()`, `wait_until()`.

The pytest files map onto modules: `test_engine.py` (runs, nodes, policies,
parallel, sub-flows), `test_modes.py` (approvals), `test_dry_run.py`,
`test_triggers.py`, `test_expressions.py`, `test_history.py` (versions,
run search), `test_audit.py` (auth + audit through `FlowApi`),
`test_contacts.py` (orbit, planner, rules, capacity), `test_tle_inputs.py`,
`test_demo.py`.

### Lint

```bash
pnpm lint --max-warnings 0                                    # the tool (CI fails on any warning)
ruff check --config ../openc3/python/pyproject.toml lib microservices test
ruff format --check --config ../openc3/python/pyproject.toml lib microservices test
```

### Browser tests (live COSMOS)

Need this plugin and the COSMOS demo plugin (INST target) installed:

```bash
COSMOS_PASSWORD=... test/ui/run.sh                    # all, ~10 min
COSMOS_PASSWORD=... test/ui/run.sh rules_test.mjs     # one
```

Each `*.mjs` is a plain Node script using Playwright that prints PASS/FAIL
lines and saves screenshots to `test/ui/shots/`. They create their own flows
and clean up, and leave the scheduler and other flows as they found them.
`contacts_test.mjs` and `rules_test.mjs` generate a TLE with epoch now
(`gen_tle.py`) and a station under its track, then wait for a real pass
(~5 min). Non-alert tests turn COSMOS notifications off while they run,
because a tool can't dismiss COSMOS alert toasts.

The editor remembers its layout (View/Edit, folded panels, flow/split/log,
log height) in `localStorage` under `openc3.flowautomator.layout`. Tests run
in a fresh browser so they get the defaults (Edit, both panels, split);
`layout_test.mjs` covers the layout and clears the key when it's done.

## Recipes

### Add a node type

1. `lib/flow_automator/graph.py`: add to `NODE_TYPES` (outputs, `errors`,
   `expressions`, `templates`, `required`); add data checks in
   `_validate_node_data` if needed.
2. `lib/flow_automator/runner.py`: write `exec_<type>(run, node, data, step)`
   returning an output handle name, register it in `EXECUTORS`. Use
   `run.sleep()` for any wait, `run.evaluator()` / `run.render_value()` for
   fields, put a human summary in `step["message"]`, honour `run.dry_run`
   (and `run.skip_waits`) if it has side effects or waits. Raise a plain
   exception for a node error (routed to the error port).
3. If it needs COSMOS, add a method to `CosmosBackend` **and** `FakeBackend`.
4. `src/tools/flowautomator/nodeTypes.js`: same type name, same output
   handles, inspector `fields` with defaults, `summary`.
5. If `_preview` in runner.py should show something specific in approval
   cards, add it.
6. Tests in `test/test_engine.py` (and `test_dry_run.py`), a row in the
   README node reference.

### Add an API endpoint

1. Add `(method, regex, handler, write)` to `_routes()` in `http_api.py`
   (`write`: `None` = no auth, `False` = read, `True` = needs script_run).
   Specific paths go before the generic `<id>` ones.
2. Add the handler method on `FlowApi` (`payload`, `query`, `user` and the
   regex groups come as keyword args); record writes with `self._audit`.
   Raise `EngineError`/`NotFoundError`/`PlannerError` for 4xx.
3. Document it in the module docstring list.
4. Add the call to `flowApi.js` (background reads use `get`, which doesn't
   toast).

### Add an alert event

Add it to `EVENTS` in `alerts.py` with its default level (alert only if
someone must act, D15), emit it with a stable `key`, list it in the README.

### Add a plugin variable / microservice option

`plugin.txt` (`VARIABLE` + `OPTION` + the comment block), parse it in
`FlowEngineMicroservice.__init__` (unknown options are logged as errors),
pass it down, add it to the README table.

### Add a TLE source

Per-satellite polled sources: subclass `TleSource` in `tle_sources.py` and
register it. Scope-wide feeds (like a new file format or protocol): add to
`TleIntake` in `tle_inputs.py` so the safeguards apply.

### Add an expression function

`_SAFE_FUNCTIONS` in `expressions.py`, its docstring, the README table,
`test_expressions.py`. Keep functions pure and bounded.

## Conventions

- Commits: Conventional Commits with scope `flow-automator`, e.g.
  `feat(flow-automator): ...` (see the repo's `/commit-message`).
- Python: ruff with the repo config (line length 120, Python 3.10+ syntax).
  Only `cosmos_backend.py` and the microservice import `openc3`; keep it that
  way so the rest is testable without COSMOS.
- Background threads catch everything and log once per distinct error
  (`last_error` pattern) so a bad trigger or tick never kills the engine.
- User-facing text (messages, README) is plain and specific: say what
  happened and what to do.
- Update the README for anything an operator would notice, and these docs
  for anything a developer would.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| Install logs an error about a per-plugin Python venv | Harmless: `/gems` is root-owned so COSMOS falls back to the shared `/gems/python_packages`, where sgp4 installs fine |
| Install/uninstall fails with EACCES | Something was installed with `cliroot`; chown the plugin's paths under `/gems` to the COSMOS UID |
| Plugin ended up in a scope named `force` | `cli load GEM force` treats `force` as the scope; pass the scope explicitly |
| Contacts view says sgp4 is missing | `requirements.txt` didn't install (air-gapped COSMOS needs a PyPI mirror: Admin → Settings) |
| Tool shows "Engine offline" | The microservice is restarting or crashed: check the operator logs |
| `rake build` in `openc3.sh cli` says the tool source changed | Run `./build_tool.sh` first (D18) |
| pnpm refuses the lockfile | `minimumReleaseAge` and a too-new dependency; see D19 |
| Redis crash-loops after starting containers by hand | Started without `openc3.sh`; restart COSMOS with `./openc3.sh run` |

## Ideas not done yet

From the README's limits and earlier discussion; none are started:

- Resume runs after an engine restart (would need persisted run state).
- Internet TLE fetch (CelesTrak / Space-Track) as a `TleSource`.
- Station horizon masks instead of one elevation; time zones other than UTC.
- Multi-scope: a per-scope route prefix that the tool discovers.
- Faster saves for very large flows (send diffs, or debounce validation).
