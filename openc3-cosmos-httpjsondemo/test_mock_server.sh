#!/bin/bash
# Starts a mock HTTP container on the COSMOS Docker network for integration testing:
#
#   LISTENER (port 80)
#     Accepts inbound HTTP POSTs from HttpJsonClientInterface.
#     Logs the raw request with socat -v and returns a JSON response that
#     matches the HTTPJSONDEMO RESPONSE telemetry packet.
#
#   POSTER (every second)
#     POSTs JSON to HttpJsonServerInterface at /TARGET_NAME/INBOUND_PACKET.
#     temperature and pressure walk upward each iteration so values are
#     visibly changing in the Telemetry Viewer.
#
# Usage:
#   ./test_mock_server.sh [options]
#
# Options (override with env vars or flags):
#   -n NAME     Container name            (default: httpjson-mock)
#   -N NETWORK  Docker network            (default: cosmos_default)
#   -p PORT     Listener port             (default: 80)
#   -H HOST     COSMOS operator hostname  (default: cosmos-openc3-operator-1)
#   -P PORT     COSMOS server port        (default: 4567)
#   -t TARGET   Target name (lowercase)   (default: httpjsondemo)
#   -k PACKET   Inbound packet (lowercase)(default: inbound)
#   -i SECS     Post interval in seconds  (default: 1)
#   -a KEY      API key for X-Api-Key header (default: none, auth disabled)

set -euo pipefail

CONTAINER_NAME="${CONTAINER_NAME:-httpjson-mock}"
DOCKER_NETWORK="${DOCKER_NETWORK:-cosmos_default}"
LISTEN_PORT="${LISTEN_PORT:-80}"
COSMOS_HOST="${COSMOS_HOST:-cosmos-openc3-operator-1}"
COSMOS_SERVER_PORT="${COSMOS_SERVER_PORT:-4567}"
TARGET_NAME="${TARGET_NAME:-httpjsondemo}"
INBOUND_PACKET="${INBOUND_PACKET:-inbound}"
POST_INTERVAL="${POST_INTERVAL:-1}"
API_KEY="${API_KEY:-}"

while getopts "n:N:p:H:P:t:k:i:a:h" opt; do
  case $opt in
    n) CONTAINER_NAME="$OPTARG" ;;
    N) DOCKER_NETWORK="$OPTARG" ;;
    p) LISTEN_PORT="$OPTARG" ;;
    H) COSMOS_HOST="$OPTARG" ;;
    P) COSMOS_SERVER_PORT="$OPTARG" ;;
    t) TARGET_NAME="$OPTARG" ;;
    k) INBOUND_PACKET="$OPTARG" ;;
    i) POST_INTERVAL="$OPTARG" ;;
    a) API_KEY="$OPTARG" ;;
    h)
      sed -n '/^# Usage/,/^[^#]/{ /^[^#]/d; s/^# \{0,2\}//; p }' "$0"
      exit 0
      ;;
    *) echo "Unknown option -$OPTARG. Use -h for help." >&2; exit 1 ;;
  esac
done

AUTH_STATUS=$([ -n "$API_KEY" ] && echo "X-Api-Key header enabled" || echo "disabled (no -a flag)")

# Remove any existing container with the same name
if docker inspect "$CONTAINER_NAME" &>/dev/null; then
  echo "Removing existing container: $CONTAINER_NAME"
  docker rm -f "$CONTAINER_NAME" >/dev/null
fi

# Write container scripts to a temp directory that gets mounted read-only
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

# --- respond.sh -----------------------------------------------------------
# socat EXEC's this for each inbound connection.
# The body matches the HTTPJSONDEMO RESPONSE telemetry packet fields.
cat > "$TMPDIR/respond.sh" << 'RESPOND'
#!/bin/sh
BODY='{"status":"ok","message":"Hello from mock","temperature":98.6,"pressure":14.7}'
LEN=$(printf '%s' "$BODY" | wc -c | tr -d ' ')
printf 'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %s\r\nConnection: close\r\n\r\n%s' "$LEN" "$BODY"
RESPOND
chmod +x "$TMPDIR/respond.sh"

# --- entrypoint.sh --------------------------------------------------------
# Variables from the outer script are expanded here (no quoting on the
# heredoc delimiter). Inner variables are escaped with \$ so they survive
# into the container at runtime.
cat > "$TMPDIR/entrypoint.sh" << ENTRYPOINT
#!/bin/sh
set -e
apk add --quiet socat curl 2>&1

echo ""
echo "=== LISTENER: socat on port ${LISTEN_PORT} ==="
echo "    Requests from HttpJsonClientInterface will appear below with '>' prefix"
echo ""
echo "=== POSTER: POSTing to http://${COSMOS_HOST}:${COSMOS_SERVER_PORT}/${TARGET_NAME}/${INBOUND_PACKET} every ${POST_INTERVAL}s ==="
echo "    Auth: ${AUTH_STATUS}"
echo ""

# Start the socat listener in the background; -v logs raw traffic to stderr
socat -v -d TCP-LISTEN:${LISTEN_PORT},fork,reuseaddr EXEC:/scripts/respond.sh &
SOCAT_PID=\$!

# Give socat a moment to bind before the first POST
sleep 0.5

# API key baked in at script-generation time; empty = no auth header sent.
HTTPJSON_API_KEY="${API_KEY}"

# Wrapper so the X-Api-Key header is included only when a key is configured.
post_json() {
  url=\$1
  body=\$2
  if [ -n "\$HTTPJSON_API_KEY" ]; then
    curl -s -o /dev/null -w '%{http_code}' \\
      -X POST "\$url" \\
      -H 'Content-Type: application/json' \\
      -H "X-Api-Key: \$HTTPJSON_API_KEY" \\
      -d "\$body" \\
      2>/dev/null
  else
    curl -s -o /dev/null -w '%{http_code}' \\
      -X POST "\$url" \\
      -H 'Content-Type: application/json' \\
      -d "\$body" \\
      2>/dev/null
  fi || echo "000"
}

# POST loop — temperature and pressure increment each iteration
i=0
while true; do
  TEMP=\$(awk -v i=\$i 'BEGIN {printf "%.2f", 72.5 + i * 0.1}')
  PRES=\$(awk -v i=\$i 'BEGIN {printf "%.2f", 101.3 + i * 0.05}')
  LABEL="sensor_\$i"
  HTTP_CODE=\$(post_json \\
    "http://${COSMOS_HOST}:${COSMOS_SERVER_PORT}/${TARGET_NAME}/${INBOUND_PACKET}" \\
    "{\\"temperature\\":\$TEMP,\\"pressure\\":\$PRES,\\"label\\":\\"\$LABEL\\"}")
  echo "[\$(date -u +%H:%M:%S)] POST #\$i  temperature=\$TEMP  pressure=\$PRES  label=\$LABEL  -> HTTP \$HTTP_CODE"
  i=\$((\$i + 1))
  sleep ${POST_INTERVAL}
done
ENTRYPOINT
chmod +x "$TMPDIR/entrypoint.sh"

echo "Starting $CONTAINER_NAME"
echo "  Network:   $DOCKER_NETWORK"
echo "  Listener:  port $LISTEN_PORT  (socat → HTTPJSONDEMO RESPONSE packet)"
echo "  Poster:    http://$COSMOS_HOST:$COSMOS_SERVER_PORT/$TARGET_NAME/$INBOUND_PACKET  every ${POST_INTERVAL}s"
echo "  Auth:      $AUTH_STATUS"
echo ""
echo "Press Ctrl+C to stop and remove the container."
echo ""

docker run --rm \
  --name "$CONTAINER_NAME" \
  --network "$DOCKER_NETWORK" \
  -v "$TMPDIR:/scripts:ro" \
  alpine:latest \
  /scripts/entrypoint.sh
