#!/usr/bin/env bash
# Real-pass soak (docs/REAL-DATA-TESTING.md step 2), run in containers.
#
#   COSMOS_PASSWORD=... realdata/soak.sh setup   # create the soak in the local COSMOS, store a session token
#   realdata/soak.sh start                       # start the recorder container (restarts with docker)
#   realdata/soak.sh status                      # recorder state and the latest events
#   realdata/soak.sh report                      # fetch SatNOGS data and score the soak so far
#   realdata/soak.sh replay [--speed 1]          # step 3c: replay recorded real passes, check the post-pass flows
#   realdata/soak.sh cfs-faults                  # step 4: staged passes with faults against cFS (needs openc3-cosmos-cfs)
#   realdata/soak.sh install <gem>               # upgrade the plugin mid-soak; the restart is left out of the scores
#   realdata/soak.sh stop                        # stop the recorder
#   COSMOS_PASSWORD=... realdata/soak.sh clear   # stop the recorder and remove the soak from COSMOS
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE=${SOAK_IMAGE:-fa-pytest}  # python:3.12-slim + pytest sgp4 numpy requests (docs/DEVELOPMENT.md)
NAME=fa-soak-recorder

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "Building $IMAGE (python:3.12-slim + sgp4 numpy requests)"
  printf 'FROM python:3.12-slim\nRUN pip install --no-cache-dir pytest sgp4 numpy requests ruff\n' | docker build -t "$IMAGE" -
fi

run() {
  docker run --rm --network host -u "$(id -u):$(id -g)" -e HOME=/tmp \
    -e COSMOS_PASSWORD -e COSMOS_URL -e SATNOGS_TOKEN -v "$PWD:/p" -w /p "$IMAGE" python "$@"
}

case "${1:-}" in
  setup) mkdir -p realdata/cache/soak && run realdata/soak_setup.py ;;
  start)
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker run -d --name "$NAME" --restart unless-stopped --network host -u "$(id -u):$(id -g)" -e HOME=/tmp \
      -e COSMOS_URL -v "$PWD:/p" -w /p "$IMAGE" python -u realdata/soak_recorder.py
    ;;
  status)
    docker ps -a --filter "name=$NAME" --format '{{.Names}}: {{.Status}}'
    docker logs --tail 5 "$NAME" 2>&1 || true
    tail -n 3 realdata/cache/soak/events-*.jsonl 2>/dev/null | cut -c1-200 || true
    ;;
  report) run realdata/soak_report.py "${@:2}" ;;
  replay) run realdata/replay_test.py "${@:2}" ;;
  cfs-faults) run realdata/cfs_faults.py "${@:2}" ;;
  install)
    # Install a Flow Automator gem without reloading the demo (setup_demo.sh would put the demo rule back on
    # the soak stations), and record the engine restart as a maintenance window the report leaves out
    GEM=${2:?usage: soak.sh install <gem>}
    CMD_API=${CMD_API_CONTAINER:-cosmos-openc3-cosmos-cmd-tlm-api-1}
    START=$(date +%s)
    SINCE=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    docker cp "$GEM" "$CMD_API:/tmp/$(basename "$GEM")" >/dev/null
    docker exec "$CMD_API" ruby /openc3/bin/openc3cli load "/tmp/$(basename "$GEM")" DEFAULT 2>&1 \
      | grep -q "Command succeeded" || { echo "Loading $GEM failed"; exit 1; }
    echo "Loaded $GEM; waiting for the new engine"
    for _ in $(seq 1 300); do
      # No "| grep -q" here: under pipefail its early exit fails the pipeline, so this never matched
      LOGS=$(docker logs --since "$SINCE" cosmos-openc3-operator-1 2>&1 || true)
      [[ "$LOGS" == *"$(basename "$GEM")"* ]] && break
      sleep 1
    done
    until curl -sf "${COSMOS_URL:-http://localhost:2900}/flow-automator/api/health" >/dev/null; do sleep 2; done
    sleep 30 # the planner re-predicts and the scheduler catches up
    run realdata/soak_report.py --maintenance "$START" "$(date +%s)" "installed $(basename "$GEM")"
    ;;
  stop) docker rm -f "$NAME" ;;
  clear)
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    run realdata/soak_setup.py --clear
    ;;
  *) sed -n '2,13p' "$0"; exit 1 ;;
esac
