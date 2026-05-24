# Tech stack

## Container

| Component | Version | Note |
|---|---|---|
| Base image | `python:3.11.8-slim-bookworm` | Pinned to a specific patch; rebuild only on intentional bump |
| `osmium-tool` (apt) | bookworm default | Used by `clip_pbf.py` for bbox extraction |
| `libgeos-c1v5` (apt) | bookworm default | Required by `shapely` |
| `libproj25` (apt) | bookworm default | Required by `pyproj` |
| `libexpat1` (apt) | bookworm default | Required by `osmium` Python bindings |
| `zip` / `unzip` (apt) | bookworm default | Used by `build_manifest.py` |

## Python deps

Pinned versions in `requirements.txt`, all ≥ 30 days old on PyPI as of
2026-05-24:

| Package | Version | PyPI upload | Purpose |
|---|---|---|---|
| `osmium`   | 4.3.1  | 2026-04-02 | PBF reader with built-in WKB geometry assembly |
| `shapely`  | 2.1.2  | 2025-09-24 | WKB parsing, polygon-in test |
| `pyproj`   | 3.7.2  | 2025-08-14 | TWD97 (EPSG:3826) ↔ WGS84 (EPSG:4326) |
| `PyYAML`   | 6.0.3  | 2025-09-25 | Config files |
| `tqdm`     | 4.67.3 | 2026-02-03 | Progress bar for 1.3M-row CSV ingest |

### Hash verification

By default, `pip install` runs without `--require-hashes` so the build
works with any compatible wheel hash from the cooldown window. To
enforce hash verification:

```bash
# 1. install pip-tools and generate hashes (host or any disposable VM)
pip install pip-tools
echo -e "osmium==4.3.1\nshapely==2.1.2\npyproj==3.7.2\nPyYAML==6.0.3\ntqdm==4.67.3" \
  > requirements.in
pip-compile --generate-hashes requirements.in

# 2. commit the resulting requirements.txt and rebuild with the strict flag
docker build --build-arg PIP_HASHES=1 -t atak-tw-address-generator:dev .
```

The `pip-compile` output replaces the existing `requirements.txt`; review
the diff carefully before committing.

## Storage formats

| File | Schema | Notes |
|---|---|---|
| `places-<county>.sqlite` | `places` + `places_fts` (FTS5 unicode61) + `metadata` | Per-county TGOS data |
| `places-osm.sqlite` | Same as above (`source='osm'`) | OSM landmarks + non-TGOS-county addr |
| `townships.sqlite` | `townships` + `townships_rtree` + `metadata` | admin_level 4/7/8 polygons (WKB) |
| `roads.sqlite` | `roads` + `roads_rtree` + `metadata` | Named highways (WKB LineString) |

All sqlite files use `PRAGMA journal_mode=WAL` during ingest; the WAL is
checkpointed on close. VACUUM is intentionally **not** run inside the
container because the Docker-for-Windows bind mount sporadically fails
with "disk I/O error" on VACUUM. Output ZIPs include the
non-vacuumed sqlite (~10 % size overhead — compresses away in the ZIP).

## OSM coordinate reference

The Geofabrik PBF stores node coordinates in WGS84. `osmium.geom.
WKBFactory` produces hex WKB in the same SRID. Our coordinate-transform
layer is only invoked for TGOS Changhua data, never for OSM.
