#!/bin/bash
# Runs benchmark_server.py inside a plain python:3-slim container on the
# COSMOS Docker network so it can reach the operator without exposing any ports.
#
# All benchmark_server.py flags are forwarded after "--". The --host default
# is set to the operator container name so no flag is needed for normal use.
#
# Usage:
#   ./run_benchmark.sh [options] [-- benchmark_server.py args]
#
# Options:
#   -n NAME     Container name            (default: httpjson-benchmark)
#   -N NETWORK  Docker network            (default: cosmos_default)
#   -H HOST     COSMOS operator hostname  (default: cosmos-openc3-operator-1)
#   -P PORT     COSMOS server port        (default: 4567)
#   -a KEY      API key for X-Api-Key     (default: none)
#   -h          Show this help
#
# Examples:
#   ./run_benchmark.sh
#   ./run_benchmark.sh -- --requests 5000 --concurrency 32
#   ./run_benchmark.sh -a mysecret -- --packets INBOUND,METRICS

set -euo pipefail

CONTAINER_NAME="${CONTAINER_NAME:-httpjson-benchmark}"
DOCKER_NETWORK="${DOCKER_NETWORK:-cosmos_default}"
COSMOS_HOST="${COSMOS_HOST:-cosmos-openc3-operator-1}"
COSMOS_SERVER_PORT="${COSMOS_SERVER_PORT:-4567}"
API_KEY="${API_KEY:-}"

while getopts "n:N:H:P:a:h" opt; do
  case $opt in
    n) CONTAINER_NAME="$OPTARG" ;;
    N) DOCKER_NETWORK="$OPTARG" ;;
    H) COSMOS_HOST="$OPTARG" ;;
    P) COSMOS_SERVER_PORT="$OPTARG" ;;
    a) API_KEY="$OPTARG" ;;
    h)
      sed -n '/^# Usage/,/^[^#]/{ /^[^#]/d; s/^# \{0,2\}//; p }' "$0"
      exit 0
      ;;
    *) echo "Unknown option -$OPTARG. Use -h for help." >&2; exit 1 ;;
  esac
done
shift $((OPTIND - 1))

# Everything after "--" is forwarded verbatim to benchmark_server.py
EXTRA_ARGS=("$@")

# Remove any existing container with the same name
if docker inspect "$CONTAINER_NAME" &>/dev/null; then
  echo "Removing existing container: $CONTAINER_NAME"
  docker rm -f "$CONTAINER_NAME" >/dev/null
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

AUTH_STATUS=$([ -n "$API_KEY" ] && echo "enabled (X-Api-Key)" || echo "disabled")

echo "Starting $CONTAINER_NAME"
echo "  Network:   $DOCKER_NETWORK"
echo "  Endpoint:  http://$COSMOS_HOST:$COSMOS_SERVER_PORT"
echo "  Auth:      $AUTH_STATUS"
[ ${#EXTRA_ARGS[@]} -gt 0 ] && echo "  Extra args: ${EXTRA_ARGS[*]}"
echo ""

# Build the arg list: fixed host/port/api-key, then any caller overrides.
BENCH_ARGS=(
  "--host" "$COSMOS_HOST"
  "--port" "$COSMOS_SERVER_PORT"
)
[ -n "$API_KEY" ] && BENCH_ARGS+=("--api-key" "$API_KEY")
BENCH_ARGS+=("${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}")

docker run --rm \
  --name "$CONTAINER_NAME" \
  --network "$DOCKER_NETWORK" \
  -v "$SCRIPT_DIR/benchmark_server.py:/benchmark_server.py:ro" \
  python:3-slim \
  python /benchmark_server.py "${BENCH_ARGS[@]}"
