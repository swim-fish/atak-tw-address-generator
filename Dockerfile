# ==============================================================================
# ATAK TW Address Data Generator - Dockerfile
#
# Description:
# Builds a self-contained environment for converting Taiwan TGOS address CSVs +
# OpenStreetMap PBF data into SQLite databases consumable by ATAK plugins
# operating fully offline.
#
# Companion to ../atak-vns-offline-routing-generator/Dockerfile which handles
# the routing axis. This pipeline handles the address axis.
# ==============================================================================

# Pinned to a patch-level base image so reproducible builds remain identical
# across rebuilds. python:3.11.8-slim-bookworm chosen for:
#   - Debian-based glibc (pyosmium / proj / GEOS wheels available)
#   - Bookworm = stable + security updates active
#   - 3.11.x has stable pyosmium 3.x wheels
FROM python:3.11.8-slim-bookworm

LABEL org.opencontainers.image.title="ATAK TW Address Data Generator"
LABEL org.opencontainers.image.description="Builds offline address SQLite (FTS5 + R*Tree) from Taiwan TGOS CSV and OSM data for ATAK plugin consumption."
LABEL org.opencontainers.image.vendor="TAK Community / Taiwan"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app

# Runtime OS deps:
#   - libexpat1 / libosmium-dev : pyosmium runtime
#   - libgeos-c1v5 : shapely runtime
#   - libproj25 : pyproj runtime
#   - zip / unzip : packaging
#   - ca-certificates : HTTPS to Geofabrik
RUN apt-get update && apt-get install -y --no-install-recommends \
    libexpat1 \
    libgeos-c1v5 \
    libproj25 \
    osmium-tool \
    zip \
    unzip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps from pinned requirements.txt.
# Hash verification (--require-hashes) is opt-in via build arg PIP_HASHES=1 once
# hashes are generated with `pip-compile --generate-hashes`. See docs/tech-stack.md.
ARG PIP_HASHES=0
COPY requirements.txt .
RUN if [ "$PIP_HASHES" = "1" ]; then \
      pip install --no-cache-dir --require-hashes -r requirements.txt; \
    else \
      pip install --no-cache-dir -r requirements.txt; \
    fi

# Copy scripts and config into the image.
COPY scripts/ ./scripts/
COPY config/ ./config/
COPY build-data.sh .
RUN chmod +x build-data.sh scripts/*.py 2>/dev/null || true

# build-data.sh dispatches subcommands (base / county / all / verify).
ENTRYPOINT ["./build-data.sh"]
