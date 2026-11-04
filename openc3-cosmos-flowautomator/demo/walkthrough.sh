#!/usr/bin/env bash
# Run the captioned demo walkthrough (walkthrough.mjs) for screen recording.
#
#   COSMOS_PASSWORD=... demo/walkthrough.sh                 # drive a Chrome window for OBS
#   COSMOS_PASSWORD=... SPEED=1 demo/walkthrough.sh         # slower (default 0.75; 0.5 is brisk)
#   COSMOS_PASSWORD=... HEADLESS=1 VIDEO_DIR=video demo/walkthrough.sh   # rehearse headless, video in demo/video
#   COSMOS_PASSWORD=... OBS_RECORD=1 demo/walkthrough.sh    # also start/stop the OBS recording
#
# Opens a separate Chrome window (its own throwaway profile, remote debugging on 127.0.0.1:9222)
# unless one is already listening, then drives it from the Playwright container used by the
# browser tests. Start recording in OBS before the countdown ends; don't touch that window.
set -euo pipefail
cd "$(dirname "$0")"
: "${COSMOS_PASSWORD:?set COSMOS_PASSWORD}"
IMAGE=mcr.microsoft.com/playwright:v1.63.0-noble
PORT=${CDP_PORT:-9222}
MODULES=../test/ui/node_modules
[ -d "$MODULES/playwright" ] || (cd ../test/ui && docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD:/work" -w /work "$IMAGE" npm install --no-audit --no-fund)

if [ "${HEADLESS:-0}" != 1 ] && ! curl -s "http://127.0.0.1:$PORT/json/version" >/dev/null; then
  CHROME=${CHROME:-$(command -v google-chrome || command -v chromium || true)}
  [ -n "$CHROME" ] || { echo "No Chrome found: set CHROME=/path/to/chrome"; exit 1; }
  PROFILE=${CHROME_PROFILE:-${XDG_CACHE_HOME:-$HOME/.cache}/flow-automator-walkthrough}
  mkdir -p "$PROFILE"
  "$CHROME" --remote-debugging-port="$PORT" --remote-debugging-address=127.0.0.1 --user-data-dir="$PROFILE" \
    --no-first-run --no-default-browser-check --disable-notifications --window-size=1920,1080 about:blank \
    >/dev/null 2>&1 &
  for _ in $(seq 30); do curl -s "http://127.0.0.1:$PORT/json/version" >/dev/null && break; sleep 0.5; done
fi

# VIDEO_DIR is a folder under demo/ (git-ignored when named video)
CONTAINER_VIDEO_DIR=""
if [ -n "${VIDEO_DIR:-}" ]; then
  mkdir -p "$VIDEO_DIR"
  CONTAINER_VIDEO_DIR="/work/$VIDEO_DIR"
fi
# OBS_RECORD=1: start and stop the OBS recording over obs-websocket (Tools > WebSocket Server
# Settings > Enable). Port and password come from the local OBS config unless OBS_URL/OBS_PASSWORD are set.
OBS_URL=${OBS_URL:-}
if [ "${OBS_RECORD:-0}" = 1 ]; then
  OBS_CONFIG=${OBS_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/obs-studio/plugin_config/obs-websocket/config.json}
  if [ -z "${OBS_PASSWORD:-}" ] && [ -f "$OBS_CONFIG" ]; then
    OBS_PASSWORD=$(sed -n 's/.*"server_password": *"\([^"]*\)".*/\1/p' "$OBS_CONFIG")
  fi
  OBS_PORT=$( [ -f "$OBS_CONFIG" ] && sed -n 's/.*"server_port": *\([0-9]*\).*/\1/p' "$OBS_CONFIG" || true)
  OBS_URL=${OBS_URL:-ws://127.0.0.1:${OBS_PORT:-4455}}
  export OBS_PASSWORD
fi
docker run --rm --network host --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -e COSMOS_PASSWORD -e COSMOS_URL -e SPEED -e HEADLESS -e FULLSCREEN -e VIDEO_DIR="$CONTAINER_VIDEO_DIR" \
  -e OBS_URL="$OBS_URL" -e OBS_PASSWORD \
  -e CDP_URL="http://127.0.0.1:$PORT" \
  -v "$PWD:/work" -v "$(cd "$MODULES" && pwd):/work/node_modules:ro" -w /work \
  "$IMAGE" node walkthrough.mjs
