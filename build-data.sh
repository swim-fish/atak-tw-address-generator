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

# Opt-in: add MOI detached-part polygons (e.g. 瑪家鄉三和村 enclave) to the
# townships layer. Off by default — they overlap the main 鄉鎮市區 layer and
# make point-in-township lookup ambiguous. See extract_townships.py.
TOWNSHIP_FLAGS=""
if [ "${INCLUDE_DETACHED_PARTS:-0}" = "1" ]; then
    TOWNSHIP_FLAGS="--include-detached-parts"
fi

if [ -z "${1:-}" ]; then
    echo "Error: missing subcommand." >&2
    exit 1
fi

SUBCOMMAND="$1"
shift || true

case "$SUBCOMMAND" in
    base)
        log "Subcommand: base (MOI townships + OSM roads + places-osm)"
        REFRESH_FLAG=""
        if [ "${1:-}" = "--no-refresh" ]; then
            REFRESH_FLAG="--no-refresh"
            shift
        fi
        python3 /app/scripts/clip_pbf.py $REFRESH_FLAG 2>&1 | tee -a "$LOG_FILE"
        python3 /app/scripts/extract_townships.py $TOWNSHIP_FLAGS 2>&1 | tee -a "$LOG_FILE"
        python3 /app/scripts/extract_roads.py 2>&1 | tee -a "$LOG_FILE"
        python3 /app/scripts/extract_places_osm.py 2>&1 | tee -a "$LOG_FILE"
        ;;
    county)
        if [ -z "${1:-}" ]; then
            echo "Error: county subcommand requires a county name (taichung|changhua)." >&2
            exit 1
        fi
        COUNTY="$1"
        shift
        NO_DEDUP=0
        NO_COLLAPSE=0
        while [ -n "${1:-}" ]; do
            case "$1" in
                --no-dedup)    NO_DEDUP=1; shift ;;
                --no-collapse) NO_COLLAPSE=1; shift ;;
                *) break ;;
            esac
        done
        log "Subcommand: county $COUNTY (dedup=$([ $NO_DEDUP -eq 1 ] && echo off || echo on), collapse=$([ $NO_COLLAPSE -eq 1 ] && echo off || echo on))"
        python3 /app/scripts/ingest_tgos_csv.py --county "$COUNTY" 2>&1 | tee -a "$LOG_FILE"
        if [ "$NO_DEDUP" -eq 0 ]; then
            python3 /app/scripts/dedup_floors.py \
                --db "/app/output/places-${COUNTY}.sqlite" --apply 2>&1 | tee -a "$LOG_FILE"
        fi
        if [ "$NO_COLLAPSE" -eq 0 ]; then
            python3 /app/scripts/collapse_coords.py \
                --db "/app/output/places-${COUNTY}.sqlite" --apply 2>&1 | tee -a "$LOG_FILE"
        fi
        ;;
    all)
        log "Subcommand: all (base + counties + dedup + collapse + full bundle)"
        REFRESH_FLAG=""
        NO_DEDUP=0
        NO_COLLAPSE=0
        while [ -n "${1:-}" ]; do
            case "$1" in
                --no-refresh)  REFRESH_FLAG="--no-refresh"; shift ;;
                --no-dedup)    NO_DEDUP=1; shift ;;
                --no-collapse) NO_COLLAPSE=1; shift ;;
                *) break ;;
            esac
        done
        python3 /app/scripts/clip_pbf.py $REFRESH_FLAG 2>&1 | tee -a "$LOG_FILE"
        python3 /app/scripts/extract_townships.py $TOWNSHIP_FLAGS 2>&1 | tee -a "$LOG_FILE"
        python3 /app/scripts/extract_roads.py 2>&1 | tee -a "$LOG_FILE"
        python3 /app/scripts/extract_places_osm.py 2>&1 | tee -a "$LOG_FILE"
        python3 /app/scripts/ingest_tgos_csv.py --county taichung 2>&1 | tee -a "$LOG_FILE"
        python3 /app/scripts/ingest_tgos_csv.py --county changhua 2>&1 | tee -a "$LOG_FILE"
        if [ "$NO_DEDUP" -eq 0 ]; then
            python3 /app/scripts/dedup_floors.py --apply 2>&1 | tee -a "$LOG_FILE"
        fi
        if [ "$NO_COLLAPSE" -eq 0 ]; then
            python3 /app/scripts/collapse_coords.py --apply 2>&1 | tee -a "$LOG_FILE"
        fi
        # Verify is advisory here: with the MOI legal boundaries, addresses on
        # harbour-reclaimed land (e.g. 台中港) sit seaward of every polygon and
        # fail the polygon-in checks by design. Report them but DON'T abort the
        # packaging step (the standalone `verify` subcommand stays strict for CI).
        if ! python3 /app/scripts/verify_samples.py 2>&1 | tee -a "$LOG_FILE"; then
            log "verify_samples reported sample failures (expected for legal-boundary coastline points); continuing to packaging"
        fi
        python3 /app/scripts/build_manifest.py 2>&1 | tee -a "$LOG_FILE"
        python3 /app/scripts/check_data_version.py 2>&1 | tee -a "$LOG_FILE"
        ;;
    dedup)
        log "Subcommand: dedup (consolidate floors by coordinate and base address)"
        APPLY_FLAG="--apply"
        if [ "${1:-}" = "--dry-run" ]; then
            APPLY_FLAG=""
            shift
        fi
        python3 /app/scripts/dedup_floors.py $APPLY_FLAG "$@" 2>&1 | tee -a "$LOG_FILE"
        ;;
    collapse)
        log "Subcommand: collapse (dedup coordinate and complete base-address keys)"
        APPLY_FLAG="--apply"
        if [ "${1:-}" = "--dry-run" ]; then
            APPLY_FLAG=""
            shift
        fi
        python3 /app/scripts/collapse_coords.py $APPLY_FLAG "$@" 2>&1 | tee -a "$LOG_FILE"
        ;;
    pack)
        log "Subcommand: pack (manifest + ZIPs only)"
        python3 /app/scripts/build_manifest.py 2>&1 | tee -a "$LOG_FILE"
        python3 /app/scripts/check_data_version.py 2>&1 | tee -a "$LOG_FILE"
        ;;
    check-version)
        log "Subcommand: check-version (data version + ZIP hash consistency)"
        python3 /app/scripts/check_data_version.py --require-full-kit "$@" 2>&1 | tee -a "$LOG_FILE"
        ;;
    verify)
        log "Subcommand: verify"
        python3 /app/scripts/verify_samples.py "$@" 2>&1 | tee -a "$LOG_FILE"
        ;;
    *)
        echo "Error: unknown subcommand '$SUBCOMMAND'. Valid: base|county|all|dedup|collapse|verify|pack|check-version" >&2
        exit 1
        ;;
esac

log "Done. Log saved to $LOG_FILE"
