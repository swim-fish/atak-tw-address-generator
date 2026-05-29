# Address Deduplication Report

The pipeline applies **two reduction stages** to each TGOS `places-*.sqlite`
after ingest:

1. **Floor-level dedup** (`scripts/dedup_floors.py`) — TGOS publishes
   one row per unit in a building (`N樓之M`, `地下一層之K`), all sharing
   the building's centre coordinate. The 2D map cannot distinguish
   floors, so these rows are redundant.
2. **Coord collapse** (`scripts/collapse_coords.py`) — TGOS also pins
   multiple distinct house numbers to one building-centre point
   (e.g. 大誠街 5-X-Y 號 — 41 different addresses at one coord). After
   floors are removed, this stage collapses every remaining same-coord
   group to one row, preferring the shortest `number` (biases toward
   the building's main door, e.g. `８２２號` over `８２２之１００號`).

Together these guarantee `places-*.sqlite` has **at most one row per
coordinate**.

> **Why reduce rather than ship as-is?** Taichung carries ~586k floor
> rows (44 % of the table); leaving them inflates the sqlite file,
> blows out FTS5 token counts, and produces ~40-deep "same hit" lists
> on every reverse-geocode probe. The coord-collapse stage trims
> another ~35k same-coord ground-floor duplicates whose markers would
> stack on the map anyway.

Both stages are wired into `build-data.sh county` and `build-data.sh all`
by default. Pass `--no-dedup` to skip stage 1 (floor) or `--no-collapse`
to skip stage 2 (coord collapse) for raw inspection.

---

## 1. Stage 1 — Floor dedup strategy (conservative)

Per `(lat, lon)` group of rows:

| Group composition | Action |
|---|---|
| 1 row (singleton coord) | Keep. |
| ≥ 1 ground-floor row + ≥ 1 floor row | **Keep all ground-floor rows; remove every floor row.** |
| All rows are floor rows | Keep the lowest `id`; remove the rest. |

A row is "floor" iff `number LIKE '%樓%' OR number LIKE '%層%'`. Examples:

- `１號二樓之５` → floor (drop if any ground-floor sibling exists)
- `１號地下一層之１` → floor
- `１號` → ground-floor (always kept)
- `５之３之２號`, `５之５之５號`, `５之８之８號` (all sharing one coord) →
  all ground-floor, **all kept** (these are distinct addresses, not
  floors of one building)

This is intentionally **conservative**. An earlier, more aggressive
"keep exactly one row per coord" variant collapsed distinct
ground-floor addresses sharing a TGOS building-centre point (e.g. the
five 大誠街 5-X-Y 號 cases at `(24.1444, 120.6778)` — 41 distinct house
numbers, all real, all map to the same point in the source CSV). The
conservative version preserves all of them.

---

## 2. Stage 2 — Coord collapse strategy (lossy)

Per remaining `(lat, lon)` group of rows (after stage 1):

| Group composition | Action |
|---|---|
| 1 row (singleton coord) | Keep. |
| ≥ 2 distinct ground-floor rows at one coord | **Keep the row with the shortest `number`; drop the rest.** Ties broken by lowest `id`. |

Example: `(24.0209929, 120.5908276)` had 70 different 三芬路 360 號 /
360 之 N 號 / 360 之 N 附 1 號 entries — all pinned to one TGOS
building-centre point. Stage 2 keeps `360號` (shortest) and drops the
69 others.

**Trade-off:** lossy by design. Forward FTS5 searches for the dropped
numbers no longer match any row. The kept row's coord still resolves
reverse-geocode probes to roughly the right building. Operators who
need the dropped recall must run with `--no-collapse`.

---

## 3. Effect on the verifier

`scripts/verify_samples.py` samples N/S/E/W extremes from the raw CSV
and looks them up in sqlite by composite key. Floor rows no longer
exist after stage 1, so the verifier **skips CSV rows whose `number`
contains 樓/層**. The same rule covers stage 2 indirectly — stage 2
only fires on coords where stage 1 already kept multiple ground-floor
rows, which is rare in the extreme-cardinal sample windows. If
verifier coverage ever drops below 99 %, see §5 for the tuning rule.

---

## 4. Latest results (2026-05-27)

Full pipeline (ingest → floor-dedup → coord-collapse → repack) run
from CSVs as published in `config/csv_sources.yaml`:

| County | Ingested | After stage 1 | After stage 2 | Total removed |
|---|---:|---:|---:|---:|
| Taichung | 1,316,671 | 766,110 | **731,005** | 585,666 (44.5 %) |
| Changhua |   467,019 | 430,053 | **426,690** |  40,329 (8.6 %) |

Stage-1 removals: floor rows.
Stage-2 removals: same-coord distinct house numbers (kept the shortest).

| County | Stage-1 removed | Stage-2 removed |
|---|---:|---:|
| Taichung | 550,561 | 35,105 |
| Changhua |  36,966 |  3,363 |

Removal lists (one row per removed id, plus the kept-id at the same
coord, suitable for manual review):

- `output/logs/dedup-places-{taichung,changhua}-removed-<UTC>.csv` — stage 1
- `output/logs/collapse-places-{taichung,changhua}-removed-<UTC>.csv` — stage 2

Coverage spot-check on the post-stage-1 sqlite (5,000 CSV rows scanned
per county, floor rows skipped per §3):

| County | Ground rows checked | Hits | Misses |
|---|---:|---:|---:|
| Taichung | 1,926 | 1,926 | 0 |
| Changhua | 4,205 | 4,205 | 0 |

(Stage-2 collapse intentionally removes some of those hits — see §2.)

Post-collapse invariants:

| County | Remaining same-coord groups |
|---|---:|
| Taichung | 0 |
| Changhua | 0 |

ZIP sizes after both stages + repackage:

| Kit | Size |
|---|---:|
| `base.zip` | 50 MiB |
| `places-changhua.zip` | 32 MiB |
| `places-taichung.zip` | 55 MiB |
| `tw-central-full.zip` | 137 MiB |

The metadata table of every reduced `places-*.sqlite` records both
operations:

```
deduped_at                 2026-05-27T14:37:00Z          (stage 1)
deduped_removed            550561                        (Taichung; 36966 for Changhua)
deduped_strategy           drop-floor-rows-when-ground-floor-exists;keep-lowest-id-otherwise
deduped_inserted_orig      1316671                       (pre-dedup count)
collapsed_at               2026-05-27T14:50:14Z          (stage 2)
collapsed_removed          35105                         (Taichung; 3363 for Changhua)
collapsed_strategy         one-row-per-coord;shortest-number;lowest-id
collapsed_inserted_pre     766110                        (post-stage-1, pre-stage-2 count)
inserted                   731005                        (final row count, refreshed)
```

---

## 5. Operator workflow

Both reduction stages are part of the default pipeline. Normal
invocations:

```bash
./run.sh county taichung                       # ingest + floor + collapse
./run.sh county taichung --no-collapse         # ingest + floor only
./run.sh county taichung --no-dedup            # ingest only (raw)
./run.sh all                                   # full pipeline + repack
./run.sh all --no-collapse                     # full pipeline but skip stage 2

./run.sh dedup [--dry-run]                     # stage 1 on existing sqlite
./run.sh collapse [--dry-run]                  # stage 2 on existing sqlite
```

Direct script use (outside Docker):

```bash
python scripts/dedup_floors.py                       # stage 1 dry-run
python scripts/dedup_floors.py --apply               # stage 1 apply
python scripts/collapse_coords.py                    # stage 2 dry-run
python scripts/collapse_coords.py --apply            # stage 2 apply
python scripts/inspect_duplicate_coords.py           # read-only report
```

Removal CSVs go to `output/logs/{dedup,collapse}-*-removed-<UTC>.csv`
in both dry-run and apply modes — review them before approving large
removals.

---

## 6. Tuning notes

If the floor regex needs to broaden (e.g. new TGOS dialect emits a
half-width "F" suffix), update the `LIKE` patterns in both
`scripts/dedup_floors.py` (`_RANK_CTE.is_floor` definition) **and**
`scripts/verify_samples.py` (the matching skip rule in
`load_county_samples`). The two must agree, otherwise the verifier
will sample rows that the dedup step removed and report false
coverage failures.

To keep stage-2 collapses but bias kept rows differently, edit the
`ORDER BY LENGTH(number), id` clause in `scripts/collapse_coords.py`.
Today's rule prefers the shortest number string (closest to "main door"
in TGOS conventions); alternatives like "alphabetical first" or
"row with the most populated optional fields" are one-line changes.
