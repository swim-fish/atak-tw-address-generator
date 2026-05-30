#!/bin/bash
# ==============================================================================
# ATAK TW Address Data Generator - Host Runner
#
# Builds the Docker image (if missing) and dispatches build-data.sh subcommands.
#
# Usage:
#   ./run.sh base                       # produce base.zip only
#   ./run.sh county taichung            # produce places-taichung.zip
#   ./run.sh county changhua            # produce places-changhua.zip
#   ./run.sh all                        # all of the above + tw-central-full.zip
#   ./run.sh verify                     # rerun verification on existing output
#
# Optional env:
#   VNS_MEMORY_GB=N           cap Docker memory (defaults to autodetect, max 8g)
#   PIP_HASHES=1              enforce hash verification on pip install (after
#                             first `pip-compile --generate-hashes` run)
#   INCLUDE_DETACHED_PARTS=1  add MOI detached-part polygons to townships
#                             (e.g. 瑪家鄉三和村 enclave); off by default
#                             because they overlap the main 鄉鎮市區 layer
# ==============================================================================

set -euo pipefail

IMAGE_NAME="atak-tw-address-generator"
IMAGE_TAG="dev"

if [ -z "${1:-}" ]; then
    cat <<'EOF'
Error: missing subcommand.

Usage:
  ./run.sh base
  ./run.sh county <taichung|changhua>
  ./run.sh all
  ./run.sh verify

See ./docs/architecture.md or ../atak-tw-address-manual.md for details.
EOF
    exit 1
fi

SUBCOMMAND="$1"
shift || true

# --- Sanity checks ---
if ! command -v docker >/dev/null 2>&1; then
    echo "Error: docker not on PATH." >&2
    exit 1
fi

mkdir -p ./output ./cache ./input

# --- Build image if missing ---
if [[ -z "$(docker images -q ${IMAGE_NAME}:${IMAGE_TAG} 2>/dev/null)" ]]; then
    echo "[run.sh] Image ${IMAGE_NAME}:${IMAGE_TAG} not found; building..."
    docker build \
        --build-arg PIP_HASHES="${PIP_HASHES:-0}" \
        -t "${IMAGE_NAME}:${IMAGE_TAG}" .
fi

# --- Memory cap ---
MEM_GB="${VNS_MEMORY_GB:-8}"

# --- Run ---
# Security flags mirror ../atak-vns-offline-routing-manual.md §6:
#   --rm                      ephemeral container
#   --cap-drop=ALL            no Linux capabilities required
#   --security-opt=no-new-privileges
# Volume mounts: input (read-only), cache (read-write for PBF), output (read-write).
#
# MSYS_NO_PATHCONV=1 prevents Git-Bash on Windows from rewriting absolute
# container-side paths (e.g. /app/input) into host paths.
export MSYS_NO_PATHCONV=1

exec docker run --rm \
    --cap-drop=ALL \
    --security-opt=no-new-privileges \
    --memory="${MEM_GB}g" \
    -e INCLUDE_DETACHED_PARTS="${INCLUDE_DETACHED_PARTS:-0}" \
    -v "$(pwd)/input:/app/input:ro" \
    -v "$(pwd)/cache:/app/cache" \
    -v "$(pwd)/output:/app/output" \
    -v "$(pwd)/config:/app/config:ro" \
    "${IMAGE_NAME}:${IMAGE_TAG}" \
    "${SUBCOMMAND}" "$@"
