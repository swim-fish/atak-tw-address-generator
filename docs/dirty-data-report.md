# Upstream Data Anomaly Report

Catalogues anomalies discovered in the TGOS and OSM source data that
the pipeline excludes during ingestion. The mechanical exclusion list
lives in `config/dirty_data.yaml`; this document gives the **why** and
the **how-to-add**.

> **Why exclude rather than warn?** A row with a coordinate ~25 km from
> its claimed locale will produce nonsense reverse-geocode output and
> may mislead an ATAK operator looking at the map. Excluding is the
> conservative behaviour: lose one row, never present wrong data.

---

## 1. Detection methodology

The verification harness (`scripts/verify_samples.py`) draws 50 samples
each from the north/south/east/west extremes of each TGOS county and
runs six integrity checks (see [`data-contract.md`](./data-contract.md)
§5). Anomalies typically surface as:

| Failure check | What it means |
|---|---|
| `coord_match` | sqlite stored (lat, lon) doesn't match CSV ground truth → projection bug or our ingest dropped data |
| `polygon_in` | (lat, lon) is not inside the township polygon claimed by the row's MOI district code → coordinate is wrong |
| `reverse_township` | Reverse polygon-in returns a different township → same root cause as polygon_in |

Once `polygon_in` / `reverse_township` reports the bad row, the operator
should:

1. Inspect adjacent rows in the same `(district_code, village, street)`
   group to see whether the error is systematic (a whole street is off)
   or single-row (one typo)
2. Confirm by reprojecting the raw coordinate manually and looking at
   the resulting point in OSM or Google Maps
3. Record the row in `config/dirty_data.yaml` with the address key,
   observed coords, and a short reason
4. Re-run `./run.sh county <county>` followed by `./run.sh verify` —
   should now pass

---

## 2. Currently catalogued anomalies

### 2.1 彰化縣田尾鄉新生村富農路一段 190 之 1 號 (TGOS 114-05)

| Field | Value |
|---|---|
| **Source** | TGOS — 彰化縣 |
| **Release** | 民國 114 年 5 月 (`changhua/114-05`) |
| **District code** | `10007210` (彰化縣田尾鄉) |
| **Address** | 新生村 8 鄰 富農路一段 190 之 1 號 |
| **TWD97 stored** | `(217897.28, 2653132.75)` |
| **WGS84 (reprojected)** | `(23.9826°N, 120.6845°E)` |
| **Polygon-in result** | 南投縣草屯鎮 |
| **Expected locale** | 彰化縣田尾鄉 |
| **Coord error** | **~25 km east** of the claimed township |

**Cross-check** with the adjacent row 190 號 (same street, same village,
same neighbour):

| 號 | TWD97 X | TWD97 Y | Locale |
|---|---|---|---|
| 190 號 | 204322.09 | 2642533.80 | 彰化縣田尾鄉 ✓ |
| 190 之 1 號 | **217897.28** | **2653132.75** | 南投縣草屯鎮 ✗ |

Adjacent rows (188 號, 186 號, …) on the same street are all near
(204370, 2642535). Only 190-1 號 is off — this is a **single-row
upstream data-entry typo**, not a systematic projection or boundary
issue.

| Discovered | 2026-05-24 |
|---|---|
| Surfaced by | `verify_samples.py` iteration 2, Changhua east extreme |
| Excluded since | data-contract v2 build, generator commit (this commit) |

---

## 3. Adding a new entry

When the verification report flags a new row:

1. Open `config/dirty_data.yaml`.
2. Locate the right `(source, county_id, data_date)` subkey, or create
   it if this is the first entry for that release.
3. Append an entry. **`match` fields must be unique enough to identify
   exactly one CSV row** — typically `district_code + village +
   neighbor + street + number` is enough; add `lane`/`alley` only if
   needed to disambiguate.
4. Document the *why* in `reason`. Include "Coord ~Nkm <direction> of
   the claimed township" so future readers can sanity-check.
5. Record the raw source coords under `observed` for reproducibility.
6. Re-run `./run.sh county <county>` → `./run.sh verify`. The
   verification table should show one fewer failure.
7. Commit `config/dirty_data.yaml` and the updated section of this
   document together.

---

## 4. What about OSM anomalies?

The pipeline also ingests OSM data via `extract_places_osm.py`
(`places-osm.sqlite`) and `extract_townships.py` / `extract_roads.py`.
OSM data is community-edited and we expect higher noise (untagged
points, polygon misalignment, name typos), but we have not catalogued
specific cases here yet. Add them under the `osm:` top-level key in
`dirty_data.yaml` if/when discovered. The matching logic in
`scripts/extract_places_osm.py` would need to be extended in tandem;
currently exclusion is wired only for TGOS rows.

---

## 5. Future enhancement — automated outlier detection

Right now anomaly discovery is anchored by the 50-N/S/E/W sample sweep.
That catches rows on the geographic extremes, but a systematic scan
would catch interior rows too. A practical implementation:

```
for each TGOS row:
    expected_township = moi_district_codes[row.district_code]["district"]
    actual_township   = polygon_in_lookup(row.lat, row.lon)
    if actual_township != expected_township:
        emit (row, "claimed=%s, actual=%s" % (expected_township, actual_township))
```

Cost: ~5 minutes per million rows once `townships.sqlite` is built. Run
it after `extract_townships.py` and before the per-county ingest, then
auto-stage any new entries into `dirty_data.yaml` for human review.

This is **not** implemented yet — current process is reactive (only
catch rows the 50×4×N sampler happens to draw). The trade-off is
acceptable because TGOS data quality is generally very high and most
single-row typos do not pose operational risk on their own; we add to
the exclusion list as they surface.
