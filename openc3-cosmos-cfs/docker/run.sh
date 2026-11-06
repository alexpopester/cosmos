#!/usr/bin/env bash
# Start (or stop) the cFS "spacecraft" and its contact gate next to a local COSMOS.
#
#   docker/run.sh build      # build both images (cFS takes a few minutes)
#   docker/run.sh start      # start them on the COSMOS docker network
#   docker/run.sh stop
#   docker/run.sh status     # the gate's link state and next pass
#   docker/run.sh logs
#
# Settings (environment): NETWORK (cosmos_default), NORAD (33591, NOAA 19), STATION_LAT/LON/ALT_M
# (Fairbanks, Alaska), MIN_ELEVATION (10), GATE_CONTROL_PORT on the host (8089).
set -euo pipefail
cd "$(dirname "$0")"
NETWORK=${NETWORK:-cosmos_default}
CFS_IMAGE=openc3-cfs:0ba1faa
GATE_IMAGE=openc3-cfs-gate:0.1.0
case "${1:-}" in
  build)
    docker build -t "$CFS_IMAGE" cfs
    docker build -t "$GATE_IMAGE" gate
    ;;
  start)
    docker rm -f cfs cfs-gate >/dev/null 2>&1 || true
    # The gate owns the network namespace; cFS joins it, so cFS's telemetry to 127.0.0.1:1235 reaches the gate
    docker run -d --name cfs-gate --network "$NETWORK" --restart unless-stopped \
      -p "127.0.0.1:${GATE_CONTROL_PORT:-8089}:8089" \
      -e NORAD="${NORAD:-33591}" -e STATION_LAT="${STATION_LAT:-64.85}" -e STATION_LON="${STATION_LON:--147.85}" \
      -e STATION_ALT_M="${STATION_ALT_M:-130}" -e MIN_ELEVATION="${MIN_ELEVATION:-10}" "$GATE_IMAGE" >/dev/null
    docker run -d --name cfs --network container:cfs-gate --restart unless-stopped \
      --cap-add CAP_SYS_RESOURCE "$CFS_IMAGE" >/dev/null
    echo "cfs and cfs-gate started on $NETWORK"
    ;;
  stop) docker rm -f cfs cfs-gate ;;
  status) curl -s "http://127.0.0.1:${GATE_CONTROL_PORT:-8089}/status"; echo ;;
  logs) docker logs --tail 30 cfs-gate; docker logs --tail 30 cfs ;;
  *) sed -n '2,12p' "$0"; exit 1 ;;
esac
