#!/usr/bin/env bash
# Build the Flow Automator's Vue tool into tools/flowautomator: with local pnpm when there is
# one, otherwise in COSMOS's openc3-node container. Then package the plugin with either
#   rake build VERSION=X.Y.Z                          (local Ruby)
#   /path/to/cosmos/openc3.sh cli rake build VERSION=X.Y.Z
# The container image defaults to the one COSMOS uses (OPENC3_REGISTRY, OPENC3_NAMESPACE,
# OPENC3_TAG from the environment), or set OPENC3_NODE_IMAGE.
set -euo pipefail
cd "$(dirname "$0")"

if command -v pnpm >/dev/null 2>&1 && command -v node >/dev/null 2>&1; then
  pnpm install --frozen-lockfile --ignore-scripts
  pnpm run build
else
  IMAGE=${OPENC3_NODE_IMAGE:-${OPENC3_REGISTRY:-docker.io}/${OPENC3_NAMESPACE:-openc3inc}/openc3-node:${OPENC3_TAG:-latest}}
  echo "No local pnpm: building in $IMAGE"
  # The image is Alpine (musl): its node_modules can't be shared with a host install, so it gets its
  # own in a docker volume mounted over node_modules. CI=true lets pnpm set that up without a prompt.
  VOLUME=${OPENC3_NODE_MODULES_VOLUME:-openc3-cosmos-flowautomator-node-modules}
  docker volume create "$VOLUME" >/dev/null
  docker run --rm --user root -v "$VOLUME:/modules" --entrypoint chown "$IMAGE" "$(id -u):$(id -g)" /modules
  # npm_config_store_dir: keep pnpm's package cache in the container, not in this folder
  docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -e CI=true -e npm_config_store_dir=/tmp/pnpm-store \
    -v "$PWD:/openc3/local:z" -v "$VOLUME:/openc3/local/node_modules" -w /openc3/local \
    --entrypoint sh "$IMAGE" -c 'pnpm install --frozen-lockfile --ignore-scripts && pnpm run build'
fi
echo "Built tools/flowautomator. Package it with: rake build VERSION=X.Y.Z (or openc3.sh cli rake build VERSION=X.Y.Z)"
