#!/bin/bash
# ==============================================================================
# ATAK TW Address Data Generator - In-Container Entrypoint
#
# Dispatches subcommands to the relevant Python scripts. Runs inside the
# Docker container (working dir = /app).
# ==============================================================================

set -euo pipefail

LOG_DIR=/app/output/logs
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/build-$(date -u +%Y%m%dT%H%M%SZ).log"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*" | tee -a "$LOG_FILE"; }

if [ -z "${1:-}" ]; then
    echo "Error: missing subcommand." >&2
    exit 1
fi

SUBCOMMAND="$1"
shift || true

case "$SUBCOMMAND" in
    base)
        log "Subcommand: base (OSM-derived townships + roads + places-osm)"
        log "TODO step 6/8 — invoke clip_pbf.py, extract_townships.py, extract_roads.py, extract_places_osm.py"
        ;;
    county)
        if [ -z "${1:-}" ]; then
            echo "Error: county subcommand requires a county name (taichung|changhua)." >&2
            exit 1
        fi
        COUNTY="$1"
        log "Subcommand: county $COUNTY"
        python3 /app/scripts/ingest_tgos_csv.py --county "$COUNTY" 2>&1 | tee -a "$LOG_FILE"
        ;;
    all)
        log "Subcommand: all (base + counties + full bundle)"
        log "TODO orchestrate: base → counties → tw-central-full.zip"
        ;;
    verify)
        log "Subcommand: verify"
        python3 /app/scripts/verify_samples.py "$@" 2>&1 | tee -a "$LOG_FILE"
        ;;
    *)
        echo "Error: unknown subcommand '$SUBCOMMAND'. Valid: base|county|all|verify" >&2
        exit 1
        ;;
esac

log "Done. Log saved to $LOG_FILE"
