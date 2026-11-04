#!/usr/bin/env bash
# Browser tests for the Flow Automator against a running COSMOS with the plugin
# (and the COSMOS demo plugin, for the INST target) installed.
#
#   COSMOS_PASSWORD=... ./run.sh                     # every test
#   COSMOS_PASSWORD=... ./run.sh rules_test.mjs      # one test
#
# Each test runs in the Playwright container with host networking. COSMOS_URL
# defaults to http://localhost:2900. Screenshots go to ./shots. The contact
# tests make a TLE with epoch now (gen_tle.py, needs uv) so a real pass starts
# a few minutes in; contacts_test.mjs waits for it, so it takes about 5 minutes.
set -euo pipefail
cd "$(dirname "$0")"
: "${COSMOS_PASSWORD:?set COSMOS_PASSWORD}"
IMAGE=mcr.microsoft.com/playwright:v1.63.0-noble
TESTS=("$@")
[ ${#TESTS[@]} -gt 0 ] || TESTS=(ui_test.mjs layout_test.mjs modes_test.mjs large_test.mjs history_test.mjs pickers_test.mjs tle_inputs_test.mjs rules_test.mjs contacts_test.mjs)
OPERATOR=${OPERATOR_CONTAINER:-cosmos-openc3-operator-1}

# history_test.mjs sends alerts to a webhook: a tiny receiver inside the operator container (where the
# engine runs) that appends each POST body to /tmp/flow_hooks.log
start_webhook_receiver() {
  docker exec "$OPERATOR" sh -c ': > /tmp/flow_hooks.log'
  docker exec -d "$OPERATOR" /openc3/python/.venv/bin/python -c "
import http.server
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        open('/tmp/flow_hooks.log', 'ab').write(self.rfile.read(int(self.headers.get('Content-Length') or 0)) + b'\n')
        self.send_response(200); self.end_headers()
    def log_message(self, *a): pass
try:
    http.server.HTTPServer(('127.0.0.1', 7799), H).serve_forever()
except OSError:
    pass  # already running
"
  sleep 1
}
docker_run() {
  docker run --rm --network host --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -e COSMOS_PASSWORD -e COSMOS_URL -e CONTACT_JSON -e WEBHOOK_URL -e SHOTS=shots \
    -v "$PWD:/work" -w /work "$IMAGE" "$@"
}
[ -d node_modules/playwright ] || docker_run npm install --no-audit --no-fund
# A TLE with epoch now for the contact tests (needs sgp4): with uv if it's here, otherwise in the
# COSMOS operator container, which has sgp4 and the installed plugin's lib
gen_tle() {
  if command -v uv >/dev/null 2>&1; then
    uv run --no-project --quiet --with sgp4 --with numpy python gen_tle.py ../../lib
  else
    docker exec -i "$OPERATOR" sh -c 'cd /tmp && /openc3/python/.venv/bin/python - "$(ls -d /gems/gems/openc3-cosmos-flowautomator-*/lib | tail -1)"' < gen_tle.py
  fi
}
mkdir -p shots
failed=0
for test in "${TESTS[@]}"; do
  echo "=== $test"
  CONTACT_JSON=""
  case "$test" in
    contacts_test.mjs | rules_test.mjs)
      CONTACT_JSON=$(gen_tle) || { echo "FAIL  couldn't generate a TLE for $test"; failed=1; continue; }
      ;;
  esac
  export CONTACT_JSON
  WEBHOOK_URL=""
  if [ "$test" = history_test.mjs ]; then
    start_webhook_receiver
    WEBHOOK_URL=http://127.0.0.1:7799/hook
  fi
  export WEBHOOK_URL
  docker_run node "$test" || failed=1
  if [ "$test" = history_test.mjs ]; then
    if docker exec "$OPERATOR" grep -q '"approval_waiting"' /tmp/flow_hooks.log; then
      echo "PASS  the webhook received the approval event"
    else
      echo "FAIL  the webhook received the approval event"; failed=1
    fi
  fi
done
exit $failed
