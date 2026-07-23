#!/bin/bash
# ==============================================================================
# ATAK TW Address Data Generator - Host Runner
#
# Builds the Docker image (if missing) and dispatches build-data.sh subcommands.
#
# Usage:
#   ./run.sh base                       # build base sqlite (townships + roads + places-osm)
#   ./run.sh county <taichung|changhua> # build + reduce one county's places sqlite
#   ./run.sh all                        # base + both counties + verify + package (end to end)
#   ./run.sh pack                       # package existing output sqlite into ZIP kits + manifests
#   ./run.sh verify                     # rerun strict verification on existing output
#   ./run.sh check-version              # verify release data versions + hashes
#
# Advanced (normally run automatically inside county/all):
#   ./run.sh dedup    [--dry-run] [args]  # stage 1: consolidate floor suffixes by base address
#   ./run.sh collapse [--dry-run] [args]  # stage 2: remove duplicate coordinate/base-address keys
#
# Subcommand flags (forwarded to the container):
#   base|all    --no-refresh              skip the Geofabrik Last-Modified check; use cached PBF
#   county|all  --no-dedup --no-collapse  skip reduction stage 1 / stage 2
#
# NOTE: `base` and `county` build sqlite only. Run `pack` (or use `all`) to
# produce the base.zip / places-*.zip / tw-central-full.zip kits.
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
  ./run.sh pack
  ./run.sh verify
  ./run.sh check-version

Advanced: dedup | collapse        (see header comment / docs)

`base` and `county` build sqlite only — run `pack` (or `all`) for the ZIP kits.

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

# --- Build image if missing OR stale ---
# Rebuild when any build input is newer than the image — otherwise an edit to
# scripts/config silently runs the OLD image and produces stale output (e.g. a
# schema bump that never reaches the container). Docker layer cache keeps an
# unchanged-source rebuild to a few seconds.
NEEDS_BUILD=0
BUILD_INPUTS=(Dockerfile requirements.txt build-data.sh scripts config)
if [[ -z "$(docker images -q ${IMAGE_NAME}:${IMAGE_TAG} 2>/dev/null)" ]]; then
    echo "[run.sh] Image ${IMAGE_NAME}:${IMAGE_TAG} not found; building..."
    NEEDS_BUILD=1
else
    IMG_CREATED="$(docker inspect -f '{{.Created}}' ${IMAGE_NAME}:${IMAGE_TAG} 2>/dev/null)"
    if [[ -n "$IMG_CREATED" ]] && \
       find "${BUILD_INPUTS[@]}" -newermt "$IMG_CREATED" -print -quit 2>/dev/null | grep -q .; then
        echo "[run.sh] Build inputs changed since image was built; rebuilding..."
        NEEDS_BUILD=1
    fi
fi
if [[ "$NEEDS_BUILD" == "1" ]]; then
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
