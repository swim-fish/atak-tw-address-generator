# ATAK TW Address Data Generator

**Builds offline Taiwan address databases (SQLite + FTS5 + R\*Tree) for ATAK plugins — address search and reverse geocoding with no cell service.**

[![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)](LICENSE)
[![Schema](https://img.shields.io/badge/data%20schema-v3-blue?style=flat-square)](docs/data-contract.md)
[![Data version](https://img.shields.io/badge/data%20version-2026.07.23.1-purple?style=flat-square)](docs/data-versioning.md)
[![Python](https://img.shields.io/badge/python-3.11-blue?style=flat-square&logo=python)](Dockerfile)

This pipeline turns three authoritative source streams — Taiwan **TGOS**
address CSVs, the Ministry of the Interior (**MOI**) administrative boundary
shapefiles, and an **OpenStreetMap** PBF extract — into a set of SQLite
databases that an ATAK plugin can read fully offline. It is the **sibling
pipeline** to the upstream [VNS routing
generator](../atak-vns-offline-routing-generator/): where VNS produces
graph-based routing data, this one produces the address axis.

## What it produces

The generator ships four ZIP kits that unpack (flat) into
`/sdcard/atak/tools/twcoord/data/` on the device:

| Kit | Contents | Mandatory | Purpose |
|---|---|---|---|
| `base.zip` | `townships.sqlite`, `roads.sqlite`, `places-osm.sqlite` | **yes** | Reverse geocoding (admin polygons + roads) + OSM landmarks/addresses |
| `places-taichung.zip` | `places-taichung.sqlite` (766,952 rows) | optional | Taichung TGOS addresses |
| `places-changhua.zip` | `places-changhua.sqlite` (429,584 rows) | optional | Changhua TGOS addresses |
| `tw-central-full.zip` | everything in one bundle | convenience | All of the above |

Each ZIP carries a sidecar `*.manifest.txt` with ZIP/file/source SHA-256,
TGOS data date, release-facing data version, address-policy version, OSM
extraction counts, build timestamp, and the region bbox.

## Data release version

The current data-kit release identity is defined once in
[`config/data_version.yaml`](config/data_version.yaml):

```yaml
data_version: "2026.07.23.1"
address_policy_version: "2"
```

`data_version` uses `YYYY.MM.DD.REVISION` CalVer and MUST be bumped for every
published data-kit release, even when the source CSV is unchanged.
`address_policy_version` is bumped only when address normalization or
reduction semantics change. These values do not replace `schema_version`;
they identify data content and processing policy without forcing a plugin
schema migration.

## Data schema — currently **v3**

Every SQLite file stores an integer `schema_version` in its `metadata`
table. Plugins MUST read it before assuming any structure:

```sql
SELECT value FROM metadata WHERE key = 'schema_version';
```

| Version | Released | Change |
|---|---|---|
| `1` | 2026-05-24 | Initial layout: `places`, `places_fts`, `townships`, `townships_rtree`, `roads`, `roads_rtree` |
| `2` | 2026-05-24 | Adds `places_rtree` for fast nearest-address reverse geocoding (< 200 ms) |
| **`3`** | **2026-05-30** | **Adds `area` to `places_fts`** — empty-street addresses (located by a named 巷/莊/新村 in `area`, e.g. 十甲巷, 介壽新村) become searchable by their 地區 locality. **Additive & non-breaking**: existing `street`/`township`/full-string queries are unchanged. |

Fresh builds emit v3 directly; existing v2 artifacts can be upgraded in place
with `scripts/migrate_fts_add_area.py` (no full rebuild). The full
plugin-facing schema, query patterns, and compatibility rules are the
canonical contract in **[`docs/data-contract.md`](docs/data-contract.md)** —
when in doubt, that file is the source of truth.

## Requirements

- **Docker** — the entire toolchain runs inside a pinned
  `python:3.11.8-slim-bookworm` image; nothing else is installed on the host.
- **Source data** placed under `input/` before running (large, read-only,
  git-ignored):
  - TGOS county address CSVs → `input/`
  - MOI 直轄市/縣市界線 + 鄉鎮市區界線 shapefiles (release 1140318) →
    `input/moi-boundaries/`
- **Internet** for the first run only — the OSM PBF is fetched from Geofabrik
  and cached under `cache/`.

## Quick start

The simplest path — build, verify, and package everything end to end:

```bash
./run.sh all
```

Or drive the stages individually:

```bash
# Build the base sqlite layers (townships + roads + places-osm)
./run.sh base

# Build + reduce a per-county TGOS layer
./run.sh county taichung
./run.sh county changhua

# Package the built sqlite into the ZIP kits + manifests
./run.sh pack

# Re-run strict verification against existing output
./run.sh verify

# Release preflight: SQLite metadata, manifests, ZIP sidecars, and hashes
./run.sh check-version
```

> **`base` and `county` build `.sqlite` only.** The `base.zip` /
> `places-*.zip` / `tw-central-full.zip` kits are produced by `pack` (or by
> `all`, which runs the whole chain).

### Subcommands

| Subcommand | Builds | Notes |
|---|---|---|
| `base` | `townships.sqlite`, `roads.sqlite`, `places-osm.sqlite` | Clips the OSM PBF first |
| `county <taichung\|changhua>` | `places-<county>.sqlite` | Ingest + two reduction stages |
| `all` | everything + all ZIPs | `base` + both counties + verify + `pack` |
| `pack` | the ZIP kits + `*.manifest.txt` | Packages whatever `.sqlite` exist in `output/` |
| `verify` | — | Strict sample verification (CI gate) |
| `check-version` | — | Release preflight for data versions, ZIP sidecars, and manifest hashes |
| `dedup` / `collapse` | — | Advanced: the standalone reduction passes, normally invoked inside `county`/`all` |

Flags forwarded to the container:

| Flag | Applies to | Effect |
|---|---|---|
| `--no-refresh` | `base`, `all` | Skip the Geofabrik `Last-Modified` check; use the cached PBF |
| `--no-dedup` | `county`, `all` | Skip reduction stage 1 (floor dedup) |
| `--no-collapse` | `county`, `all` | Skip reduction stage 2 (same-coordinate/base-address dedup) |
| `--dry-run` | `dedup`, `collapse` | Report what would change without writing |

`run.sh` builds the Docker image on first use and runs the container with
hardened flags (`--rm`, `--cap-drop=ALL`, `--security-opt=no-new-privileges`,
input/config mounted read-only). Outputs land in `./output/`.

Optional environment variables:

| Var | Effect |
|---|---|
| `VNS_MEMORY_GB=N` | Cap the container memory (default autodetect, max 8g) |
| `PIP_HASHES=1` | Enforce `--require-hashes` on `pip install` (see [`docs/tech-stack.md`](docs/tech-stack.md)) |
| `INCLUDE_DETACHED_PARTS=1` | Include MOI detached-part polygons (e.g. 瑪家鄉三和村 enclave); off by default |

## Install on the device

1. Copy the relevant ZIP(s) to the Android device.
2. Unpack so the `.sqlite` files land **flat** in
   `/sdcard/atak/tools/twcoord/data/`.
3. Restart ATAK. A plugin that finds `base.zip` data gets reverse geocoding;
   each `places-<county>.sqlite` present adds that county's address search.

Plugins behave gracefully when any `places-*.sqlite` is absent — that simply
means the county was not installed.

## How it works

```
TGOS CSVs ─┐
           ├─ ingest / normalise / reproject ─┐
MOI shp  ──┤                                  ├─ reduce ─ verify ─ package ─ ZIP kits
OSM PBF  ──┘                                  ┘
```

- **TGOS CSVs** — per-county government address points with exact
  coordinates. Changhua is published in TWD97 (EPSG:3826) and reprojected to
  WGS84 (EPSG:4326) via `pyproj`.
- **MOI shapefiles** — the legal ground truth for 縣市 / 鄉鎮市區 polygons,
  read with `pyshp`; each 鄉鎮市區 carries its parent 縣市 inline.
- **OSM PBF** — Geofabrik Taiwan extract, clipped to the region bbox, supplying
  the roads layer and non-TGOS-county landmarks/addresses.
- **Glyph normalisation** — `臺` → `台` everywhere a place name appears, so
  county/township join keys are consistent across all three sources.
- **Address reduction** — two passes (`dedup_floors.py`,
  `collapse_coords.py`) remove floor suffixes and exact duplicate base
  addresses while preserving different house numbers that share a TGOS
  coordinate.
- **Verification** — `verify_samples.py` is the primary quality gate: 200
  anchor samples per TGOS county × 6 checks = ~2,400 assertions per county.

See [`docs/architecture.md`](docs/architecture.md) for the full design, and
the [Operator's Manual](../atak-tw-address-manual.md) for end-to-end runbook
detail. The zh-TW end-user placement note is in
[`../TW-離線地址-使用說明.md`](../TW-離線地址-使用說明.md).

## Documentation

| Doc | What it covers |
|---|---|
| [`docs/data-contract.md`](docs/data-contract.md) | Canonical plugin-facing schema, query patterns, versioning policy |
| [`docs/architecture.md`](docs/architecture.md) | Source streams, per-county design, reduction, verification |
| [`docs/tech-stack.md`](docs/tech-stack.md) | Pinned container + Python dependencies, hash verification |
| [`docs/address-search-guide.md`](docs/address-search-guide.md) | FTS5 query construction, tokenizer behaviour |
| [`../atak-tw-address-manual.md`](../atak-tw-address-manual.md) | Operator's manual (runbook) |

## Disclaimers

This tool only generates **data files**. The actual ATAK plugin that consumes
them is a separate component. Offline address data reflects the source release
dates (TGOS data date, MOI boundary release 1140318, OSM extract) and will not
include changes published after a build.

## License

[MIT](LICENSE) © 2026 Shihyu.
