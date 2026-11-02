#!/usr/bin/env bash
# Live end-to-end check of the GMSEC image against a real ActiveMQ broker:
#   ActiveMQ broker <- publish_demo_tlm.py (real GMSEC)  -> GmsecInterface -> COSMOS decode
# No COSMOS install needed; everything runs in throwaway containers.
#
#   ./test/e2e/run_activemq_e2e.sh [image]     # default image: openc3-operator-gmsec:7.4.1
#   PUB_IMAGE=<other image> ./test/e2e/run_activemq_e2e.sh <image>
#                                              # publish with another image, e.g. GMSEC 4 to 5
set -euo pipefail

IMAGE="${1:-openc3-operator-gmsec:7.4.1}"
PUB_IMAGE="${PUB_IMAGE:-$IMAGE}"
BROKER_IMAGE="${BROKER_IMAGE:-apache/activemq-classic:5.18.6}"
PLUGIN_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
NET=gmsec-e2e
LOG="$(mktemp)"

cleanup() { docker rm -f gmsec-e2e-activemq >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup
docker network create "$NET" >/dev/null

echo "Starting ActiveMQ ($BROKER_IMAGE)..."
docker run -d --rm --name gmsec-e2e-activemq --network-alias activemq --network "$NET" "$BROKER_IMAGE" >/dev/null
for _ in $(seq 60); do
  docker logs gmsec-e2e-activemq 2>&1 | grep -q "Apache ActiveMQ .* started" && break
  sleep 1
done

run() {
  local image="$IMAGE"
  if [ "$1" = "--pub" ]; then image="$PUB_IMAGE"; shift; fi
  docker run --rm --network "$NET" -v "$PLUGIN_DIR:/plugin:ro" \
    -e PYTHONPATH=/opt/GMSEC_API/bin:/opt/GMSEC_API/bin/lib/GMSECAPI4:/opt/GMSEC_API/bin/lib/GMSECAPI5 \
    --entrypoint /openc3/python/.venv/bin/python "$image" -X faulthandler "$@"
}

echo "Starting GmsecInterface reader..."
run /plugin/test/e2e/read_tlm.py >"$LOG" 2>&1 &
reader=$!
sleep 6

echo "Publishing demo telemetry..."
run --pub /plugin/tools/publish_demo_tlm.py mw-id=activemq39 server=tcp://activemq:61616 rate=10 count=15 >/dev/null

status=0
wait "$reader" || status=$?
# The reader has no Redis, so COSMOS logs connection noise; show only results
grep -E "^(CONNECTED|HEALTH|POWER|EVENT|SEEN)|Fatal Python|Segmentation" "$LOG" || true
if [ "$status" -ne 0 ] || ! grep -q "^SEEN \['EVENT', 'HEALTH', 'POWER'\]" "$LOG"; then
  echo "FAILED (reader exit $status), full log: $LOG"
  exit 1
fi
echo "PASSED"
rm -f "$LOG"
