#!/usr/bin/env bash
# Live benchmark: COSMOS inject_tlm vs this plugin's JSON interface, side by side.
# Needs a running COSMOS with this plugin installed (JSONDEMO target included).
#
#   ./test/compare_live.sh                      # prompts for anything not set
#   ./test/compare_live.sh --duration 10 --clients 1 4 16 64
#
# Credentials come from the environment (or a prompt) and are passed to the
# container by name, so they never appear on a command line:
#   OPENC3_API_PASSWORD   your COSMOS password (for inject_tlm)
#   JSON_TLM_TOKEN        the plugin's token, i.e. the JSON_TLM_TOKEN secret (for the JSON interface)
# Leave one blank to benchmark only the other.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
CONTAINER="${OPENC3_OPERATOR_CONTAINER:-$(docker ps --filter name=openc3-operator --format '{{.Names}}' | head -1)}"
if [ -z "$CONTAINER" ]; then
  echo "No running openc3-operator container found. Start COSMOS, or set OPENC3_OPERATOR_CONTAINER." >&2
  exit 1
fi

if [ -z "${OPENC3_API_PASSWORD:-}" ] && [ -t 0 ]; then
  read -rsp "COSMOS password (blank to skip inject_tlm): " OPENC3_API_PASSWORD; echo
fi
if [ -z "${JSON_TLM_TOKEN:-}" ] && [ -t 0 ]; then
  read -rsp "JSON interface token (blank to skip the JSON interface): " JSON_TLM_TOKEN; echo
fi
export OPENC3_API_PASSWORD="${OPENC3_API_PASSWORD:-}" JSON_TLM_TOKEN="${JSON_TLM_TOKEN:-}"

docker cp "$DIR/compare_live.py" "$CONTAINER:/tmp/compare_live.py" >/dev/null
exec docker exec -e OPENC3_API_PASSWORD -e JSON_TLM_TOKEN ${JSON_TLM_URL:+-e JSON_TLM_URL} \
  "$CONTAINER" /openc3/python/.venv/bin/python /tmp/compare_live.py "$@"
