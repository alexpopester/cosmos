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
[ ${#TESTS[@]} -gt 0 ] || TESTS=(ui_test.mjs modes_test.mjs large_test.mjs rules_test.mjs contacts_test.mjs)
docker_run() {
  docker run --rm --network host --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -e COSMOS_PASSWORD -e COSMOS_URL -e CONTACT_JSON -e SHOTS=shots \
    -v "$PWD:/work" -w /work "$IMAGE" "$@"
}
[ -d node_modules/playwright ] || docker_run npm install --no-audit --no-fund
mkdir -p shots
failed=0
for test in "${TESTS[@]}"; do
  echo "=== $test"
  CONTACT_JSON=""
  case "$test" in
    contacts_test.mjs | rules_test.mjs)
      CONTACT_JSON=$(uv run --no-project --quiet --with sgp4 --with numpy python gen_tle.py ../../lib)
      ;;
  esac
  export CONTACT_JSON
  docker_run node "$test" || failed=1
done
exit $failed
