#!/usr/bin/env bash
# Put a local COSMOS (docker, as started by openc3.sh) in a state to demo the Flow Automator:
#
#   1. installs the COSMOS demo plugin (the INST target) if it isn't there
#   2. installs the Flow Automator plugin if it isn't running (the newest .gem
#      next to this folder, or FLOW_GEM=path), or always with --reinstall
#   3. waits for INST telemetry and the flow engine
#   4. loads the demo: satellites with today's TLEs, ground stations (one under
#      DEMOSAT-1's track so a pass starts in a few minutes), templates, flows,
#      a recurring rule and scheduled activities
#
# Run it again any time to reset the demo; it only replaces demo items.
#
#   COSMOS_PASSWORD=... demo/setup_demo.sh [--reinstall]
#
# Settings: COSMOS_URL (http://localhost:2900), SCOPE (DEFAULT),
# CMD_API_CONTAINER (cosmos-openc3-cosmos-cmd-tlm-api-1), FLOW_GEM.
set -euo pipefail
cd "$(dirname "$0")/.."

URL=${COSMOS_URL:-http://localhost:2900}
SCOPE=${SCOPE:-DEFAULT}
CMD_API=${CMD_API_CONTAINER:-cosmos-openc3-cosmos-cmd-tlm-api-1}
REINSTALL=false
[ "${1:-}" = "--reinstall" ] && REINSTALL=true
: "${COSMOS_PASSWORD:?Set COSMOS_PASSWORD to your COSMOS password}"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

say "Logging in to COSMOS at $URL"
TOKEN=$(curl -sf -X POST "$URL/openc3-api/auth/verify" -H 'Content-Type: application/json' \
  -d "{\"password\":\"$COSMOS_PASSWORD\"}") || fail "Can't log in to COSMOS at $URL (is it running? is the password right?)"

rpc() {
  local method=$1 params=${2:-[]}
  curl -sf "$URL/openc3-api/api?scope=$SCOPE" -H "Authorization: $TOKEN" -H 'Content-Type: application/json-rpc' \
    -d "{\"jsonrpc\":\"2.0\",\"method\":\"$method\",\"params\":$params,\"id\":1,\"keyword_params\":{\"scope\":\"$SCOPE\"}}"
}
engine() { curl -sf "$URL/flow-automator/api$1?scope=$SCOPE" -H "Authorization: $TOKEN" "${@:2}"; }
load_gem() {
  docker cp "$1" "$CMD_API:/tmp/$(basename "$1")" >/dev/null
  local output
  output=$(docker exec "$CMD_API" ruby /openc3/bin/openc3cli load "/tmp/$(basename "$1")" "$SCOPE" 2>&1) || true
  # COSMOS logs a harmless error when it can't make a per-plugin Python venv and falls back to the shared one
  printf '%s\n' "$output" | grep -q "Command succeeded" || { printf '%s\n' "$output" | tail -20; fail "Loading $1 failed"; }
}
wait_for() {  # description, seconds, shell function to call until it succeeds
  local what=$1 seconds=$2 check=$3
  for _ in $(seq 1 "$seconds"); do "$check" >/dev/null 2>&1 && return 0; sleep 1; done
  fail "Timed out waiting for $what"
}
has_inst() { rpc get_target_names | grep -q '"INST"'; }
engine_up() { engine /status; }
count() { rpc get_tlm_cnts '[[["INST","HEALTH_STATUS"]]]' | grep -o '[0-9][0-9]*' | tail -1; }
telemetry_moving() { [ "$(count)" != "$FIRST" ]; }

# 1. The COSMOS demo plugin, for the INST target the demo flows command
if rpc get_target_names | grep -q '"INST"'; then
  say "COSMOS demo plugin is installed (INST target found)"
else
  IMAGE=$(docker inspect -f '{{.Config.Image}}' "$CMD_API" 2>/dev/null) || fail "Can't find the $CMD_API container"
  TAG=${IMAGE##*:}
  say "Installing the COSMOS demo plugin from openc3inc/openc3-cosmos-init:$TAG"
  TMP=$(mktemp -d)
  trap 'rm -rf "$TMP"' EXIT
  docker run --rm --entrypoint sh "openc3inc/openc3-cosmos-init:$TAG" -c 'cat /openc3/plugins/gems/openc3-cosmos-demo-*.gem' \
    > "$TMP/openc3-cosmos-demo.gem" || fail "Couldn't get the demo plugin from openc3inc/openc3-cosmos-init:$TAG"
  load_gem "$TMP/openc3-cosmos-demo.gem"
  wait_for "the INST target" 120 has_inst
fi

# 2. The Flow Automator plugin
if $REINSTALL || ! engine /health >/dev/null 2>&1; then
  GEM=${FLOW_GEM:-$(ls -t openc3-cosmos-flowautomator-*.gem 2>/dev/null | head -1)}
  [ -n "$GEM" ] || fail "No Flow Automator gem found: build one with 'rake build VERSION=x.y.z' or set FLOW_GEM"
  say "Installing the Flow Automator plugin ($GEM)"
  load_gem "$GEM"
  sleep 20 # the operator restarts the engine microservice
fi
say "Waiting for the flow engine"
wait_for "the flow engine at $URL/flow-automator" 120 engine_up

# 3. INST telemetry flowing
say "Waiting for INST telemetry"
FIRST=$(count || echo 0)
wait_for "INST HEALTH_STATUS packets" 60 telemetry_moving

# 4. Load the demo
say "Loading the demo"
RESPONSE=$(curl -s -w '\n%{http_code}' -X POST "$URL/flow-automator/api/demo?scope=$SCOPE" \
  -H "Authorization: $TOKEN" -H 'Content-Type: application/json' -d '{}')
SUMMARY=$(printf '%s' "$RESPONSE" | sed '$d')
[ "$(printf '%s' "$RESPONSE" | tail -1)" = 200 ] || fail "The engine couldn't load the demo: $SUMMARY"
AOS_IN=$(printf '%s' "$SUMMARY" | grep -o '"aos_in": [0-9-]*' | grep -o '[0-9-]*$' || true)
ACTIVITIES=$(printf '%s' "$SUMMARY" | grep -o '"rule_activities": [0-9]*' | grep -o '[0-9]*$' || true)
SCRIPT_PROBLEM=$(printf '%s' "$SUMMARY" | grep -o '"script_problem": "[^"]*"' || true)

cat <<EOF

$(printf '\033[1;32mThe demo is ready.\033[0m')

  Open $URL/tools/flowautomator?view=contacts

  DEMOSAT-1 rises over the "Demo Pass Site" in about $(( ${AOS_IN:-0} / 60 )) min $(( ${AOS_IN:-0} % 60 ))s.
  The rule scheduled ${ACTIVITIES:-some} "Pass downlink" activities; the first one starts
  60s before that AOS and runs by itself (walk mode, nothing hazardous).
${SCRIPT_PROBLEM:+
  Note: $SCRIPT_PROBLEM
}
  A walkthrough is in README.md under "Demo".
EOF
