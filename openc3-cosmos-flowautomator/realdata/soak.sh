#!/usr/bin/env bash
# Real-pass soak (docs/REAL-DATA-TESTING.md step 2), run in containers.
#
#   COSMOS_PASSWORD=... realdata/soak.sh setup   # create the soak in the local COSMOS, store a session token
#   realdata/soak.sh start                       # start the recorder container (restarts with docker)
#   realdata/soak.sh status                      # recorder state and the latest events
#   realdata/soak.sh report                      # fetch SatNOGS data and score the soak so far
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
  stop) docker rm -f "$NAME" ;;
  clear)
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    run realdata/soak_setup.py --clear
    ;;
  *) sed -n '2,10p' "$0"; exit 1 ;;
esac
