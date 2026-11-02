#!/usr/bin/env bash
# Builds GMSEC versions of COSMOS images. Each keeps its stock name and gets a
# -gmsec<version> tag, so any Dockerfile that builds FROM
# ${OPENC3_REGISTRY}/${OPENC3_NAMESPACE}/<image>:${OPENC3_TAG} (COSMOS's own,
# COSMOS Enterprise's, yours) can use it as a base:
#
#   <OUT_REGISTRY>/<OUT_NAMESPACE>/<image>:<OPENC3_TAG>-gmsec<GMSEC_VERSION>
#
#   ./build_images.sh                          # openc3-base and openc3-operator
#   ./build_images.sh openc3-base openc3-operator openc3-cosmos-script-runner-api
#   OUT_REGISTRY=registry.example.com OUT_NAMESPACE=mission PUSH=1 ./build_images.sh
#
# Images must be openc3-base or built from it. GMSEC is compiled once, in
# openc3-base, and the build cache reuses it for every image after the first.
#
# Environment (defaults in brackets):
#   OPENC3_REGISTRY, OPENC3_NAMESPACE  stock images to start from [docker.io, openc3inc]
#   OPENC3_TAG                         COSMOS version [7.4.1]
#   GMSEC_VERSION                      4.9.1 or a 5.x release [4.9.1]
#   OUT_REGISTRY, OUT_NAMESPACE        where the new tags go [localhost, openc3-gmsec]
#   PUSH                               1 = docker push each image after building
set -euo pipefail

OPENC3_REGISTRY="${OPENC3_REGISTRY:-docker.io}"
OPENC3_NAMESPACE="${OPENC3_NAMESPACE:-openc3inc}"
OPENC3_TAG="${OPENC3_TAG:-7.4.1}"
GMSEC_VERSION="${GMSEC_VERSION:-4.9.1}"
OUT_REGISTRY="${OUT_REGISTRY:-localhost}"
OUT_NAMESPACE="${OUT_NAMESPACE:-openc3-gmsec}"
OUT_TAG="${OPENC3_TAG}-gmsec${GMSEC_VERSION}"
DIR="$(cd "$(dirname "$0")" && pwd)"

if [ $# -eq 0 ]; then
  set -- openc3-base openc3-operator
fi

built=()
for image in "$@"; do
  tag="${OUT_REGISTRY}/${OUT_NAMESPACE}/${image}:${OUT_TAG}"
  echo "=== ${tag}"
  docker build -t "$tag" \
    --build-arg OPENC3_REGISTRY="$OPENC3_REGISTRY" \
    --build-arg OPENC3_NAMESPACE="$OPENC3_NAMESPACE" \
    --build-arg OPENC3_TAG="$OPENC3_TAG" \
    --build-arg OPENC3_IMAGE="$image" \
    --build-arg GMSEC_VERSION="$GMSEC_VERSION" \
    "$DIR"
  if [ "${PUSH:-}" = 1 ]; then
    docker push "$tag"
  fi
  built+=("$tag")
done

echo
echo "Built:"
printf '  %s\n' "${built[@]}"
echo
echo "To build another image FROM these, pass:"
echo "  --build-arg OPENC3_REGISTRY=${OUT_REGISTRY} --build-arg OPENC3_NAMESPACE=${OUT_NAMESPACE} --build-arg OPENC3_TAG=${OUT_TAG}"
