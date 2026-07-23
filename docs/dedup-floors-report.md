# Address Reduction Report

The pipeline applies two loss-reduction stages to each TGOS
`places-*.sqlite` after ingest. The current policy preserves different
addresses that share a building-centre coordinate.

## 1. Stage 1 — Base-address floor consolidation

`scripts/dedup_floors.py` treats a row as floor-level only when `樓` or `層`
occurs after the first `號`. It removes that suffix to derive the base number
and groups rows by:

```text
(lat, lon, county, township, village, neighborhood, road, section,
 lane, alley, sub_alley, base_number, area)
```

For each group:

| Composition | Action |
|---|---|
| An explicit non-floor/base row exists | Keep its lowest `id`; remove floor rows and duplicate base rows. |
| Every row has a floor suffix | Rewrite the lowest-id row to the base number; remove the rest. |
| `樓`/`層` occurs before `號` or no `號` exists | Preserve the row as-is and report it as a non-suffix marker. |

Examples:

- `３２號二樓` → base number `３２號`
- `３２號地下一層之１` → base number `３２號`
- `３１號` and `３２號二樓` at the same coordinate remain two addresses;
  the latter becomes `３２號`
- `合作大樓１之１號` remains unchanged

## 2. Stage 2 — Exact base-address deduplication

`scripts/collapse_coords.py` runs after stage 1. It groups rows by the same
exact coordinate and complete base-address key and keeps the lowest `id`.
It does not collapse different house numbers merely because TGOS assigned
them the same coordinate.

The final invariants are:

- no floor suffix remains after `號`;
- no duplicate exact coordinate-plus-complete-base-address key remains;
- different base addresses at the same coordinate are retained.

## 3. Results — 2026-07-23

| County | Ingested | Stage 1 removed | Synthesized bases | Stage 2 removed | Final |
|---|---:|---:|---:|---:|---:|
| Taichung | 1,316,671 | 549,472 | 10,956 | 247 | 766,952 |
| Changhua | 467,019 | 36,617 | 1,753 | 818 | 429,584 |

Taichung also retains 256 rows containing a non-suffix `樓`/`層` marker;
Changhua has none.

The comparison with the frozen previous release is generated at
`output/comparison/reports/address-output-comparison.md`.

## 4. Metadata

Reduced TGOS databases record:

```text
deduped_at
deduped_removed
deduped_synthesized
deduped_non_suffix_floor_markers
deduped_strategy
deduped_inserted_orig
collapsed_at
collapsed_removed
collapsed_strategy
collapsed_inserted_pre
inserted
```

## 5. Operator workflow

Both stages run by default:

```bash
./run.sh county taichung
./run.sh county changhua
./run.sh all

./run.sh dedup [--dry-run]
./run.sh collapse [--dry-run]
```

The apply modes write audit CSVs under `output/logs/` for both removed rows
and synthesized base addresses.
