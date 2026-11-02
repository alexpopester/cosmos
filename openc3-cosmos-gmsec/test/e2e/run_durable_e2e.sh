#!/usr/bin/env bash
# Live check of OPTION DURABLE against a real ActiveMQ broker: messages published
# while the interface is disconnected arrive when it reconnects.
#
#   ./test/e2e/run_durable_e2e.sh [image]     # default image: openc3-operator-gmsec:7.4.1
#
# Three cases, each: connect + disconnect (registers the subscription), publish
# 42 messages while disconnected, reconnect and count what arrives.
#   durable              expect 42, in order
#   durable + restart    broker restarted while disconnected, expect 42
#   not durable          expect 0 (the old behavior)
set -euo pipefail

IMAGE="${1:-openc3-operator-gmsec:7.4.1}"
BROKER_IMAGE="${BROKER_IMAGE:-apache/activemq-classic:5.18.6}"
PLUGIN_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
NET=gmsec-durable-e2e
BROKER=gmsec-durable-e2e-activemq
EXPECTED=42 # publish_demo_tlm.py count=20: 20 HEALTH, 20 POWER, 2 EVENT

cleanup() { docker rm -f "$BROKER" >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup
docker network create "$NET" >/dev/null

wait_for_broker() {
  for _ in $(seq 60); do
    docker logs --since "$1" "$BROKER" 2>&1 | grep -q "Apache ActiveMQ .* started" && return
    sleep 1
  done
  echo "ActiveMQ did not start" >&2
  exit 1
}

echo "Starting ActiveMQ ($BROKER_IMAGE)..."
since=$(date +%s)
docker run -d --name "$BROKER" --network-alias activemq --network "$NET" "$BROKER_IMAGE" >/dev/null
wait_for_broker "$since"

run() {
  docker run --rm --network "$NET" -v "$PLUGIN_DIR:/plugin:ro" \
    -e PYTHONPATH=/opt/GMSEC_API/bin:/opt/GMSEC_API/bin/lib/GMSECAPI4:/opt/GMSEC_API/bin/lib/GMSECAPI5 \
    --entrypoint /openc3/python/.venv/bin/python "$IMAGE" -X faulthandler "$@"
}

# Prints the number of packets received on reconnect
check() {
  local name="$1" restart="$2" log
  log="$(mktemp)"
  run /plugin/test/e2e/durable_reader.py "$name" >"$log" 2>&1
  grep -q "^RECEIVED 0$" "$log" || { echo "first connect received data: $log" >&2; exit 1; }
  run /plugin/tools/publish_demo_tlm.py mw-id=activemq39 server=tcp://activemq:61616 rate=50 count=20 >/dev/null
  if [ "$restart" = restart ]; then
    since=$(date +%s)
    docker restart "$BROKER" >/dev/null
    wait_for_broker "$since"
  fi
  run /plugin/test/e2e/durable_reader.py "$name" >"$log" 2>&1
  if ! grep -q "^CONNECTED True" "$log"; then
    echo "reconnect failed: $log" >&2
    exit 1
  fi
  grep "^HEALTH_SEQ" "$log" >&2
  grep "^RECEIVED" "$log" | awk '{print $2}'
  rm -f "$log"
}

status=0
result() {
  if [ "$2" = "$3" ]; then echo "PASS  $1: $2 of $EXPECTED"; else echo "FAIL  $1: got $2, expected $3"; status=1; fi
}

result "durable" "$(check e2e-durable none-restart)" "$EXPECTED"
result "durable, broker restarted" "$(check e2e-restart restart)" "$EXPECTED"
result "not durable" "$(check none none-restart)" 0
exit "$status"
