# ATAK TW Address Data Contract

| Field | Value |
|---|---|
| **Contract version** | `2` (this document) |
| **Producer** | `atak_vns_offline_routing/atak-tw-address-generator` |
| **Consumers** | ATAK plugins reading offline TW address data — primarily `atak_tw_power_plugin` |
| **Status** | Stable; bump `schema_version` on incompatible changes |

This document is the **shared interface** between the data generator and any
plugin that consumes its output. Generator changes that affect any field
listed here MUST be accompanied by a version bump and a CHANGELOG entry.

---

## 1. Versioning policy

The integer `schema_version` lives in the `metadata` table of every
sqlite produced. Plugins MUST read it before assuming any structure:

```sql
SELECT value FROM metadata WHERE key = 'schema_version';
```

| Version | Released | Change |
|---|---|---|
| `1` | 2026-05-24 | Initial layout: `places`, `places_fts`, `townships`, `townships_rtree`, `roads`, `roads_rtree` |
| `2` | 2026-05-24 | **Adds `places_rtree`** for fast nearest-address lookup |

### Forward / backward compatibility rules

- **Additive changes** (new optional table, new optional column) → minor
  version bump (`2` → `2.1`) [reserved; we use integers for now]
- **Schema-breaking changes** (rename, type change, removed column) →
  major bump (`2` → `3`)
- **Plugins MUST** branch on `schema_version` rather than assuming the
  latest. The reverse-geocode tier 3 example below shows the v1/v2 path
  selection.

---

## 2. File set and deployment layout

The generator produces four ZIPs; each one unpacks (flat, no extra
directories) into:

```
/sdcard/atak/tools/twcoord/data/
```

| File | Source ZIP | Mandatory | Purpose |
|---|---|---|---|
| `townships.sqlite` | `base.zip` | **yes** | Admin polygons for reverse geocoding |
| `roads.sqlite` | `base.zip` | **yes** | Named roads for reverse geocoding |
| `places-osm.sqlite` | `base.zip` | **yes** | OSM landmarks + non-TGOS addr |
| `places-taichung.sqlite` | `places-taichung.zip` | optional | Taichung TGOS addr (1.3M rows) |
| `places-changhua.sqlite` | `places-changhua.zip` | optional | Changhua TGOS addr (467K rows) |
| `timestamp.<region>` | each ZIP | yes (per ZIP) | Data-date sidecar (string, no newline-stripping required) |
| `*.manifest.txt` | each ZIP | informational | Provenance (NOT used at runtime) |

Plugins MUST behave gracefully when any `places-*.sqlite` is absent —
that simply means the county was not installed. A missing
`townships.sqlite` or `roads.sqlite` degrades reverse geocoding;
plugins SHOULD detect this and surface a clear status to the user.

Discovery pattern (Java pseudocode):

```java
File dir = new File("/sdcard/atak/tools/twcoord/data/");
for (File f : dir.listFiles((d, name) -> name.matches("places-.*\\.sqlite"))) {
    openAndIndex(f);   // mount each county sqlite independently
}
```

---

## 3. SQLite schemas

### 3.1 `places-*.sqlite` (v2)

Same schema for `places-taichung.sqlite`, `places-changhua.sqlite`,
`places-osm.sqlite`. The `source` column distinguishes provenance.

```sql
CREATE TABLE places (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,             -- 'tgos' | 'osm'
    osm_id INTEGER,                   -- non-null when source='osm'
    lat REAL NOT NULL,                -- WGS84 latitude
    lon REAL NOT NULL,                -- WGS84 longitude
    name TEXT,                        -- compact "街+號" form, halfwidth
    display_name TEXT NOT NULL,       -- full "縣+鄉鎮+村里+街+巷+弄+號", fullwidth
    display_name_halfwidth TEXT NOT NULL,  -- halfwidth digits, "之"→"-"
    district_code TEXT,               -- MOI 7- or 8-digit; null for OSM landmarks
    county TEXT,                      -- e.g. "台中市" (normalised, never "臺中市")
    township TEXT,                    -- e.g. "北屯區" (level-7) or "鹿港鎮" (level-8)
    village TEXT,                     -- 村里 (TGOS) or null
    neighbor TEXT,                    -- 鄰 (numeric, kept as TEXT)
    street TEXT,                      -- 街、路段
    area TEXT,                        -- 地區 (used when street empty, e.g. 大城鄉)
    lane TEXT,                        -- 巷
    alley TEXT,                       -- 弄
    number TEXT,                      -- 號 (preserved fullwidth)
    place_type TEXT                   -- OSM only: 'city'|'town'|'village'|'hamlet'|...
);

CREATE INDEX idx_places_district ON places(district_code);
CREATE INDEX idx_places_lookup ON places(
    district_code, village, neighbor, street, area, lane, alley, number
);
CREATE INDEX idx_places_county  ON places(county);    -- places-osm only

CREATE VIRTUAL TABLE places_fts USING fts5(
    name, display_name, display_name_halfwidth, street, township,
    content='places',
    content_rowid='id',
    tokenize='unicode61'              -- CJK = per-character tokens
);

-- *** NEW in v2: spatial index for nearest-address lookup ***
CREATE VIRTUAL TABLE places_rtree USING rtree(
    id,                               -- matches places.id
    min_lat, max_lat,                 -- equal (places are points)
    min_lon, max_lon                  -- equal (places are points)
);

CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

Required `metadata` keys:

| Key | Example | Notes |
|---|---|---|
| `schema_version` | `'2'` | Plugins MUST read this |
| `source` | `'tgos'` or `'osm'` | Provenance of the whole file |
| `county` | `'台中市'` | TGOS only |
| `data_date` | `'115-01'` | TGOS only; 民國年-月 |
| `csv_sha256` | hex | TGOS only |
| `inserted` | `'731005'` | row count *(refreshed after each reduction stage; equals the actual row count in `places`)* |
| `skipped_dirty` | `'1'` | TGOS only; rows excluded per `dirty_data.yaml` (see [`dirty-data-report.md`](./dirty-data-report.md)) |
| `deduped_at` | `'2026-05-27T14:37:00Z'` | TGOS only; UTC timestamp of stage 1 (floor dedup) |
| `deduped_removed` | `'550561'` | TGOS only; floor rows removed in stage 1 (see [`dedup-floors-report.md`](./dedup-floors-report.md)) |
| `deduped_strategy` | `'drop-floor-rows-when-ground-floor-exists;keep-lowest-id-otherwise'` | TGOS only; stage-1 rule tag |
| `deduped_inserted_orig` | `'1316671'` | TGOS only; pre-stage-1 row count |
| `collapsed_at` | `'2026-05-27T14:50:14Z'` | TGOS only; UTC timestamp of stage 2 (same-coord collapse) |
| `collapsed_removed` | `'35105'` | TGOS only; same-coord duplicates removed in stage 2 |
| `collapsed_strategy` | `'one-row-per-coord;shortest-number;lowest-id'` | TGOS only; stage-2 rule tag |
| `collapsed_inserted_pre` | `'766110'` | TGOS only; post-stage-1, pre-stage-2 row count |
| `region` | `'tw-central'` | OSM only |
| `bbox` | `'120.20,23.55,121.45,24.75'` | OSM only |

### 3.2 `townships.sqlite`

Multi-level admin polygons. Plugins resolve "what 鄉鎮市區 / 縣市 am I
in?" via R*Tree bbox prefilter + WKB polygon-in test.

```sql
CREATE TABLE townships (
    id INTEGER PRIMARY KEY,
    osm_id INTEGER NOT NULL,
    admin_level INTEGER NOT NULL,     -- 4=縣市, 7=直轄市區, 8=縣轄鄉鎮市
    name_zh TEXT NOT NULL,            -- normalised 「臺」→「台」
    name_en TEXT,
    geometry_wkb BLOB NOT NULL        -- multipolygon, WGS84
);

CREATE INDEX idx_townships_level ON townships(admin_level);
CREATE INDEX idx_townships_name  ON townships(name_zh);

CREATE VIRTUAL TABLE townships_rtree USING rtree(
    id, min_lat, max_lat, min_lon, max_lon
);

CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
```

Metadata: `schema_version`, `source='osm-clipped'`, `region`, `bbox`,
`inserted_level4`, `inserted_level7`, `inserted_level8`.

### 3.3 `roads.sqlite`

Named highways as LineStrings.

```sql
CREATE TABLE roads (
    id INTEGER PRIMARY KEY,
    osm_id INTEGER NOT NULL,
    name_zh TEXT NOT NULL,            -- normalised
    name_en TEXT,
    highway TEXT NOT NULL,            -- motorway|trunk|primary|...|service
    geometry_wkb BLOB NOT NULL        -- LineString, WGS84
);

CREATE INDEX idx_roads_name    ON roads(name_zh);
CREATE INDEX idx_roads_highway ON roads(highway);

CREATE VIRTUAL TABLE roads_rtree USING rtree(
    id, min_lat, max_lat, min_lon, max_lon
);

CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
```

---

## 4. Conventions

### 4.1 Text encoding & glyph normalisation

- All TEXT columns are UTF-8.
- The character `臺` (used in OSM for 臺中市/臺北市/臺南市/臺東縣/臺西…)
  is normalised to `台` everywhere it appears as part of a place name.
  TGOS / MOI already use `台`. Plugins MAY assume any name read from
  these sqlite files uses `台`.
- This normalisation applies to `name`, `name_zh`, `display_name`,
  `township`, `county`, `street`. It does NOT apply to OSM tags
  inside any other context, since these sqlite files don't ship raw
  OSM tags.

### 4.2 Digit width

TGOS source CSVs use fullwidth Han glyphs for digits (`２之３之２號`).
We preserve them in `display_name`, and store a halfwidth + hyphenated
variant in `display_name_halfwidth` (`2-3-2號`) for FTS5 search input
that a user types in plain ASCII digits.

Plugins SHOULD use `display_name` for UI rendering and
`display_name_halfwidth` (or `name`) for FTS5 query matching.

### 4.3 `source` enum

| Value | Meaning |
|---|---|
| `'tgos'` | MOI Taiwan Geographic One Stop CSV — authoritative |
| `'osm'` | OpenStreetMap — crowdsourced; addresses are sparse outside the OSM Taiwan editor community's focus regions |

### 4.4 `district_code` semantics

The MOI 鄉鎮市區代碼:

- **7 digits** for 直轄市 (e.g. `6601100` = 台中市大甲區)
- **8 digits** for 縣轄 (e.g. `10007020` = 彰化縣鹿港鎮)

Plugins SHOULD treat it as an opaque string key. Mapping to county /
township text is already done at ingest time; the values are in the
`county` and `township` columns of the same row.

### 4.5 Coordinate system

All `lat` / `lon` columns and all WKB geometries are in **WGS84
(EPSG:4326)**. TGOS Changhua's TWD97 coordinates are reprojected at
ingest time via `pyproj` (EPSG:3826 → EPSG:4326), with round-trip
stability < 1 m.

---

## 5. Reference query patterns

These are the canonical implementations the generator validates against
during `verify_samples.py`. Plugins MAY use equivalent native APIs.

### 5.1 Tier 1 — Reverse geocode to township (rev = county + 鄉鎮市區)

Available with **base.zip alone**; does NOT need any `places-*.sqlite`.

```sql
-- 1a) township (level 8 縣轄, then level 7 直轄市區)
SELECT t.name_zh
FROM townships t JOIN townships_rtree r ON r.id = t.id
WHERE t.admin_level = ?            -- try 8, then 7
  AND r.min_lat <= :lat AND :lat <= r.max_lat
  AND r.min_lon <= :lon AND :lon <= r.max_lon;
-- Then in app code: load geometry_wkb, parse, check polygon.covers(point)

-- 1b) county (level 4)
-- Same query with admin_level = 4
```

App-side parsing of `geometry_wkb` requires a WGS84 WKB parser (e.g.
`org.locationtech.jts.io.WKBReader` on Android).

### 5.2 Tier 2 — Reverse geocode to nearest road name

```sql
SELECT roads.name_zh, roads.highway, roads.geometry_wkb
FROM roads JOIN roads_rtree r ON roads.id = r.id
WHERE r.min_lat <= :lat + 0.01 AND :lat - 0.01 <= r.max_lat
  AND r.min_lon <= :lon + 0.01 AND :lon - 0.01 <= r.max_lon;
-- App: parse each WKB LineString, project the point, pick minimum distance.
-- 0.01° ≈ 1 km — adjust the search radius to taste.
```

### 5.3 Tier 3 — Reverse geocode to nearest house number (v2 R*Tree path)

```sql
SELECT p.display_name, p.display_name_halfwidth, p.lat, p.lon
FROM places p JOIN places_rtree r ON p.id = r.id
WHERE r.min_lat <= :lat + 0.005 AND :lat - 0.005 <= r.max_lat
  AND r.min_lon <= :lon + 0.005 AND :lon - 0.005 <= r.max_lon;
-- App: haversine over the few hundred candidate rows, pick min.
-- 0.005° ≈ 500 m bbox; typical urban hit count < 500.
```

Expected runtime on Taichung (1.3M rows): **< 200 ms** including parsing.

### 5.4 Tier 3 — schema_version=1 fallback

If `schema_version` reads `'1'`, the file has no `places_rtree`. Plugins
that need tier 3 should restrict by `district_code` (using tier 1's
result) and sequential-scan:

```sql
-- First do tier 1, then:
SELECT display_name, lat, lon FROM places WHERE district_code = ?;
-- Sequential haversine in app code. ~50-200k rows per district.
-- Typical 1-10 second response time.
```

### 5.5 Forward search (text → coordinate)

```sql
-- Sanitise the user input (strip FTS5 punctuation: " * ( ) - : ' \).
-- Then phrase-quote:
SELECT p.id, p.display_name, p.lat, p.lon
FROM places p
WHERE p.id IN (
    SELECT rowid FROM places_fts WHERE places_fts MATCH ?
)
LIMIT 50;
-- Pass query as e.g. '"鹿港中山路"'. The unicode61 tokenizer treats CJK
-- characters as individual tokens, so phrase queries match contiguously.
```

For numeric address fragments (e.g. user typed `100號`), prefer matching
against `display_name_halfwidth` and quote the entire phrase.

### 5.6 Composite reverse-geocode (recommended for "show full address")

The plugin pipes tier 1 → tier 2 → tier 3 in a single query flow:

1. Tier 1 returns `(county, township)`.
2. Tier 2 returns `(road_name, distance_m)`.
3. Tier 3 returns `(display_name, distance_m)`.
4. The plugin presents `display_name` if tier-3 distance is < 50 m,
   otherwise falls back to `"<county><township> <road_name> 附近"`.

---

## 6. Manifest file format (informational)

Each ZIP ships with a sidecar `*.manifest.txt`. Plugins SHOULD NOT
parse this at runtime — it is for operator-side auditing. Keys:

```
Kit:              base
Region:           tw-central
Bbox:             120.20,23.55,121.45,24.75
Generated at:     2026-05-24T13:45:00Z
ZIP size:         42.4 MiB
ZIP SHA-256:      <hex>
Contents:
  <label>
    file:    <basename>
    size:    <MiB>
    sha256:  <hex>
    meta.<key>: <value>
```

---

## 7. CHANGELOG

### `2` — 2026-05-27 (additive metadata)

- TGOS `places-*.sqlite` files now ship with **two reduction stages**
  applied before packaging:
  - Stage 1: floor-level rows (`number LIKE '%樓%' OR '%層%'`) that
    share a coord with a ground-floor row are removed.
  - Stage 2: each remaining same-coord group is collapsed to one row
    (kept row = shortest `number`, ties by lowest `id`).
  - Net invariant: at most one row per `(lat, lon)`.
- Schema **unchanged**.
- New informational `metadata` keys on TGOS files:
  - `deduped_at`, `deduped_removed`, `deduped_strategy`,
    `deduped_inserted_orig` (stage 1)
  - `collapsed_at`, `collapsed_removed`, `collapsed_strategy`,
    `collapsed_inserted_pre` (stage 2)
  - See [`dedup-floors-report.md`](./dedup-floors-report.md) for the
    rule and current counts.
- `metadata.inserted` is refreshed after each stage so it always equals
  `COUNT(*) FROM places`. Plugins that already trust `inserted` need
  no change.

### `2` — 2026-05-24

- Add `places_rtree USING rtree(id, min_lat, max_lat, min_lon, max_lon)`
  to all `places-*.sqlite` files
- Bump `metadata.schema_version` from `'1'` to `'2'`
- Plugin-side tier-3 reverse geocode shifts from sequential scan
  (1-30 s) to R*Tree bbox + haversine refine (< 200 ms)
- Add `metadata.skipped_dirty` to TGOS sqlite files — count of rows
  excluded per `dirty_data.yaml`. See [`dirty-data-report.md`](./dirty-data-report.md).
  Schema unchanged; key is informational.

### `1` — 2026-05-24

- Initial release: `places`, `places_fts`, `townships`,
  `townships_rtree`, `roads`, `roads_rtree`
