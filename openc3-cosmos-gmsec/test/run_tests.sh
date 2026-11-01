#!/usr/bin/env bash
# Run this plugin's tests against the COSMOS Python library.
#
#   ./test/run_tests.sh              # run all tests
#   ./test/run_tests.sh -k oidc      # pass extra args straight to pytest
#
# Override autodetection with env vars:
#   COSMOS_PYTHON_DIR=/path/to/cosmos/openc3/python   PYTHON=/path/to/python
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "$0")/.." && pwd)"
COSMOS_PY="${COSMOS_PYTHON_DIR:-$PLUGIN_DIR/../openc3/python}"

if [ ! -d "$COSMOS_PY" ]; then
  echo "Could not find the COSMOS python dir at '$COSMOS_PY'." >&2
  echo "Set COSMOS_PYTHON_DIR to <cosmos-repo>/openc3/python and retry." >&2
  exit 1
fi
cd "$COSMOS_PY"

# Pick an interpreter: explicit PYTHON, else uv's venv, else `uv run`.
if [ -n "${PYTHON:-}" ]; then
  PY=("$PYTHON")
elif [ -x ".venv/bin/python" ]; then
  PY=(".venv/bin/python")
elif command -v uv >/dev/null 2>&1; then
  PY=(uv run python)
else
  echo "No Python found. Run 'uv sync' in $COSMOS_PY, or set PYTHON." >&2
  exit 1
fi

# NixOS: numpy's C extension needs libstdc++ which isn't on the default loader
# path. Prepend a gcc-lib from the nix store if one exists (keeping any existing
# LD_LIBRARY_PATH). Harmless on non-nix systems, where the glob matches nothing.
libstdcxx="$(ls /nix/store/*gcc*-lib/lib/libstdc++.so.6 2>/dev/null | head -1 || true)"
if [ -n "$libstdcxx" ]; then
  export LD_LIBRARY_PATH="$(dirname "$libstdcxx")${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

echo "Running plugin tests with: ${PY[*]}  (cwd: $COSMOS_PY)"
COSMOS_PLUGIN_DIR="$PLUGIN_DIR" exec "${PY[@]}" -m pytest "$PLUGIN_DIR/test" -v "$@"
