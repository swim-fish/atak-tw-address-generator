# Base-Address Rebuild and Comparison Plan

## Objective

Replace the current coordinate-only TGOS reduction rules with
base-address-aware consolidation:

1. Derive a base house number when `樓` or `層` appears after the first `號`,
   removing that floor suffix. A marker before `號` can be part of a building
   name such as `合作大樓１之１號` and is not a floor suffix.
2. Group rows by exact coordinate and full base address.
3. Prefer an existing non-floor row for a base address.
4. When a base address has only floor rows, rewrite the lowest-id row into a
   synthetic base-address row and remove the other floor rows.
5. Keep different base addresses even when TGOS assigns them the same
   coordinate.
6. Remove only duplicate rows that resolve to the same coordinate and base
   address.

Example:

```text
３１號 + ３２號二樓 -> ３１號 + ３２號
```

## Safety and baseline

- Do not rebuild until the current SQLite, ZIP, manifest, and verification
  artifacts have been copied to `output/comparison/baseline/`.
- Record SHA-256, byte size, metadata, row counts, FTS5 counts, RTree counts,
  coordinate cardinality, and floor-row counts for the baseline.
- Build the candidate from the same TGOS CSVs, MOI boundary release, and
  cached OSM input.
- Store machine-readable and Markdown comparison results under
  `output/comparison/reports/`.
- Do not remove the baseline after a successful rebuild.

## Implementation

### 1. Base-number parsing

Add shared helpers that:

- classify `number` values with `樓` or `層` after the first `號` as floor
  rows;
- retain the prefix through the first `號` as `base_number`;
- leave non-floor rows unchanged;
- preserve and report ambiguous markers that are not suffixes after `號`.

Required examples:

```text
３２號二樓         -> ３２號
３２號三樓之１     -> ３２號
３２號地下一層     -> ３２號
３２之５號二樓     -> ３２之５號
臨１１２之４號三樓 -> 臨１１２之４號
```

### 2. Floor consolidation

Update `scripts/dedup_floors.py` to partition by:

```text
lat, lon, district_code, village, street, area, lane, alley, base_number
```

For each partition:

- keep the lowest-id non-floor row when one exists;
- otherwise keep the lowest-id floor row and rewrite it to the base address;
- remove every other row in the partition;
- rebuild `number`, `name`, `display_name`, and `display_name_halfwidth` on a
  synthesized row;
- emit separate removal and synthesis audit CSVs.

### 3. Duplicate base-address consolidation

Update `scripts/collapse_coords.py` so it no longer enforces one row per
coordinate. It must keep one row per exact coordinate and full address key,
while preserving different addresses at the same point.

### 4. Verification

Add unit tests for parsing, selection, synthesis, and same-coordinate
different-address preservation.

Verify every candidate database with:

- `PRAGMA quick_check`;
- `COUNT(places) == COUNT(places_rtree)`;
- no remaining automatically parseable floor rows;
- no duplicate exact coordinate plus full base-address keys;
- FTS5 lookup for synthesized base addresses;
- coordinate differences measured explicitly; any rebuild-only floating-point
  delta must be quantified and shown to be negligible;
- the existing cardinal-extreme sample verifier.

## Rebuild

1. Rebuild `places-taichung.sqlite` from the existing Taichung CSV.
2. Rebuild `places-changhua.sqlite` from the existing Changhua CSV.
3. Re-run verification.
4. Repackage all ZIP kits and manifests.
5. Run the schema-version consistency gate.

## Comparison report

Compare baseline and candidate for each county:

- SQLite and ZIP byte sizes and SHA-256;
- total rows and unique coordinates;
- coordinates with more than one address;
- maximum addresses at one coordinate;
- remaining floor rows;
- synthesized base-address rows;
- exact duplicate coordinate-plus-address keys;
- added, removed, changed, and unchanged logical addresses;
- FTS5 and RTree consistency.

Expected candidate row counts from the pre-change audit:

| County | Baseline | Expected candidate | Expected increase |
|---|---:|---:|---:|
| Taichung | 731,005 | 766,952 | 35,947 |
| Changhua | 426,690 | 429,584 | 2,894 |

Expected synthesized base addresses after the suffix-position correction:

| County | Expected |
|---|---:|
| Taichung | 10,956 |
| Changhua | 1,753 |

The earlier pre-change estimate for Taichung was 11,212. Inspection showed
that 256 of those rows contain a proper-name marker before `號` (for example,
`合作大樓１之１號`) rather than a floor suffix, so they are retained
unchanged and reported separately.

## Acceptance criteria

- Baseline artifacts remain available and hash-verifiable.
- Different base addresses at the same coordinate are retained.
- The same base address at the same coordinate is represented once.
- Floor-only groups produce searchable base-address rows.
- SQLite, FTS5, and RTree checks pass.
- Sample verification passes or every exception is explicitly documented.
- Comparison reports explain all material row-count and file-size changes.

## Execution status

- [x] Frozen the previous SQLite, ZIP, manifest, and verification artifacts.
- [x] Implemented and tested suffix parsing and base-address synthesis.
- [x] Rebuilt Taichung and Changhua from the configured source CSVs.
- [x] Rebuilt FTS5 and RTree indexes and repackaged all release kits.
- [x] Passed address-rule, coordinate-transform, schema, and sample checks.
- [x] Generated machine-readable and Markdown baseline comparisons.
