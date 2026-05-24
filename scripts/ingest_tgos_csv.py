"""Ingest a TGOS address CSV into a per-county SQLite database.

Per the plan, each TGOS county produces its own `places-<county>.sqlite` so
that the artefact can be shipped, updated, or omitted independently. The
schema is identical across counties; only the source CSV column mapping
differs (configured in ``config/csv_sources.yaml``).

Usage (run inside the Docker container, dispatched by ``build-data.sh``):

    python3 scripts/ingest_tgos_csv.py --county taichung
    python3 scripts/ingest_tgos_csv.py --county changhua
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import os
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import yaml
from tqdm import tqdm

import coord_transform as ct
import normalize_address as na

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


def load_csv_sources() -> dict:
    with open(CONFIG_DIR / "csv_sources.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_district_codes() -> dict:
    with open(CONFIG_DIR / "moi_district_codes.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


# Field order for the dirty-data composite key. MUST match the call site
# below. Documented in docs/dirty-data-report.md.
_DIRTY_KEY_FIELDS = (
    "district_code", "village", "neighbor",
    "street", "lane", "alley", "number",
)


def load_dirty_set(source: str, county_id: str, data_date: str) -> set[tuple]:
    """Return the set of CSV-row composite keys to exclude.

    Each entry in ``config/dirty_data.yaml`` lists a partial set of
    fields under ``match``; we project to the full key tuple with
    blanks for fields the entry didn't specify. The CSV-row key is
    built with the same convention so a partial match key (e.g. one
    without `lane`) still matches a row where the CSV column was empty.
    """
    path = CONFIG_DIR / "dirty_data.yaml"
    if not path.exists():
        return set()
    cfg = yaml.safe_load(path.read_text("utf-8")) or {}
    entries = (cfg.get(source, {}) or {}).get(county_id, {}) or {}
    entries = entries.get(data_date, []) or []
    out: set[tuple] = set()
    for e in entries:
        m = e.get("match", {}) or {}
        out.add(tuple(str(m.get(k, "")) for k in _DIRTY_KEY_FIELDS))
    return out


def _row_dirty_key(raw) -> tuple:
    """Build the composite key for a CSV row in the same order as
    ``_DIRTY_KEY_FIELDS``."""
    return (
        raw.district_code, raw.village, raw.neighbor,
        raw.street, raw.lane, raw.alley, raw.number,
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA temp_store = MEMORY;
PRAGMA cache_size = -200000;          -- ~200MB

CREATE TABLE places (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,             -- 'tgos' or 'osm'
    osm_id INTEGER,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    name TEXT,                        -- compact "街+號" form for marker labels
    display_name TEXT NOT NULL,       -- full Taiwan-style address (fullwidth)
    display_name_halfwidth TEXT NOT NULL,
    district_code TEXT NOT NULL,
    county TEXT NOT NULL,
    township TEXT NOT NULL,
    village TEXT,
    neighbor TEXT,                    -- 鄰 (numeric in TGOS)
    street TEXT,                      -- 街、路段
    area TEXT,                        -- 地區 (used by coastal 大城鄉 etc. when 街 empty)
    lane TEXT,                        -- 巷
    alley TEXT,                       -- 弄
    number TEXT                       -- 號
);

CREATE INDEX idx_places_district ON places(district_code);
CREATE INDEX idx_places_lookup ON places(
    district_code, village, neighbor, street, area, lane, alley, number
);

CREATE VIRTUAL TABLE places_fts USING fts5(
    name, display_name, display_name_halfwidth, street, township,
    content='places',
    content_rowid='id',
    tokenize='unicode61'
);

-- Spatial index for nearest-address reverse geocoding (data-contract v2).
-- See docs/data-contract.md §5.3.
CREATE VIRTUAL TABLE places_rtree USING rtree(
    id, min_lat, max_lat, min_lon, max_lon
);

CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# See docs/data-contract.md §1. Bump on incompatible schema changes.
SCHEMA_VERSION = "2"


# ---------------------------------------------------------------------------
# Row processing
# ---------------------------------------------------------------------------

@dataclass
class RawRow:
    """Subset of a CSV row needed downstream."""
    district_code: str
    village: str
    neighbor: str
    street: str
    area: str
    lane: str
    alley: str
    number: str
    lon: float
    lat: float


def stream_rows(
    csv_path: Path,
    encoding: str,
    columns: dict,
    *,
    is_wgs84: bool,
    transformer: ct.Transformer | None,
) -> Iterator[RawRow]:
    """Yield :class:`RawRow` objects from the TGOS CSV.

    Coordinate handling:
      - if ``is_wgs84``: read lon/lat from columns[lon]/columns[lat] directly
      - else: read x/y from columns[x]/columns[y] and project via transformer
    """
    with open(csv_path, encoding=encoding, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                if is_wgs84:
                    lon = float(row[columns["lon"]])
                    lat = float(row[columns["lat"]])
                else:
                    x = float(row[columns["x"]])
                    y = float(row[columns["y"]])
                    lon, lat = transformer.transform(x, y)
            except (KeyError, ValueError):
                continue  # skip malformed coord
            yield RawRow(
                district_code=row[columns["district_code"]].strip(),
                village=row.get(columns["village"], "").strip(),
                neighbor=row.get(columns["neighbor"], "").strip(),
                street=row.get(columns["street"], "").strip(),
                area=row.get(columns["area"], "").strip(),
                lane=row.get(columns["lane"], "").strip(),
                alley=row.get(columns["alley"], "").strip(),
                number=row.get(columns["number"], "").strip(),
                lon=lon,
                lat=lat,
            )


def derive_record(raw: RawRow, code_table: dict) -> tuple | None:
    """Turn a :class:`RawRow` into a tuple ready for INSERT, or None to skip.

    Skip conditions:
      - empty 號 (3 rows / 3 rows observed — dirty)
      - district_code unknown in mapping table
    """
    if not raw.number:
        return None
    mapping = code_table.get(raw.district_code)
    if mapping is None:
        return None
    county = mapping["county"]
    township = mapping["district"]

    parts_fw = na.AddressParts(
        county=county,
        district=township,
        village=raw.village,
        street=raw.street,
        area=raw.area,
        lane=raw.lane,
        alley=raw.alley,
        number=raw.number,
    )
    display_name = na.compose_display_name(parts_fw, halfwidth=False)
    display_name_hw = na.compose_display_name(parts_fw, halfwidth=True)

    # Compact "name" field for marker labels: just street+lane+alley+number,
    # halfwidth form. Falls back to area when street is empty (orphan).
    head_for_name = parts_fw.street or parts_fw.area
    name_parts_compact = na.AddressParts(
        county="", district="", village="",
        street=head_for_name, area="",
        lane=parts_fw.lane, alley=parts_fw.alley, number=parts_fw.number,
    )
    short_name = na.compose_display_name(name_parts_compact, halfwidth=True)

    return (
        "tgos",                # source
        None,                   # osm_id
        raw.lat,
        raw.lon,
        short_name,
        display_name,
        display_name_hw,
        raw.district_code,
        county,
        township,
        raw.village or None,
        raw.neighbor or None,
        raw.street or None,
        raw.area or None,
        raw.lane or None,
        raw.alley or None,
        raw.number or None,
    )


# ---------------------------------------------------------------------------
# SQLite I/O
# ---------------------------------------------------------------------------

INSERT_SQL = """
INSERT INTO places (
    source, osm_id, lat, lon, name, display_name, display_name_halfwidth,
    district_code, county, township, village, neighbor, street, area,
    lane, alley, number
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ingest(county_id: str, batch_size: int = 10000) -> int:
    """Ingest one county. Returns the number of inserted rows."""
    config = load_csv_sources()
    code_table = load_district_codes()

    if county_id not in config:
        print(f"ERROR: county '{county_id}' not in csv_sources.yaml", file=sys.stderr)
        return -1

    src = config[county_id]
    csv_path = Path(__file__).resolve().parent.parent / src["path"]
    if not csv_path.exists():
        print(f"ERROR: CSV not found at {csv_path}", file=sys.stderr)
        return -1

    out_path = OUTPUT_DIR / src["output_sqlite"]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    # Build in /tmp (container overlay fs) — Docker-for-Windows bind mounts
    # sporadically fail with "disk I/O error" during long sqlite write
    # workloads. /tmp is single-writer, overlay-backed, and very fast.
    # Copy to the mount at the end.
    tmp_path = Path("/tmp") / src["output_sqlite"]
    if tmp_path.exists():
        tmp_path.unlink()

    is_wgs84 = src["crs"] == "EPSG:4326"
    transformer = None if is_wgs84 else ct._TO_WGS84  # reuse cached transformer

    print(f"[{county_id}] CSV: {csv_path.name}")
    print(f"[{county_id}] CRS: {src['crs']} {'(direct)' if is_wgs84 else '(reprojecting)'}")
    print(f"[{county_id}] Build: {tmp_path} → {out_path}")

    # SHA-256 source before reading (so it's recorded even if interrupted)
    print(f"[{county_id}] Computing source SHA-256...")
    src_sha = sha256_of_file(csv_path)

    conn = sqlite3.connect(tmp_path)
    try:
        conn.executescript(SCHEMA_SQL)

        t0 = time.time()
        inserted = 0
        skipped_no_number = 0
        skipped_unknown_code = 0
        skipped_dirty = 0
        batch: list[tuple] = []

        dirty_set = load_dirty_set("tgos", county_id, src.get("data_date", ""))
        if dirty_set:
            print(f"[{county_id}] Dirty-data exclusion list: {len(dirty_set)} row(s) "
                  f"(see docs/dirty-data-report.md)")

        expected = src.get("expected_rows", 0)
        rows_iter = stream_rows(
            csv_path,
            src["encoding"],
            src["columns"],
            is_wgs84=is_wgs84,
            transformer=transformer,
        )
        progress = tqdm(rows_iter, total=expected, unit="rows", desc=county_id)

        for raw in progress:
            if not raw.number:
                skipped_no_number += 1
                continue
            if raw.district_code not in code_table:
                skipped_unknown_code += 1
                continue
            if dirty_set and _row_dirty_key(raw) in dirty_set:
                skipped_dirty += 1
                continue
            rec = derive_record(raw, code_table)
            if rec is None:
                continue
            batch.append(rec)
            if len(batch) >= batch_size:
                conn.executemany(INSERT_SQL, batch)
                batch.clear()
                inserted += batch_size
        if batch:
            conn.executemany(INSERT_SQL, batch)
            inserted += len(batch)
        conn.commit()
        elapsed = time.time() - t0

        print(f"[{county_id}] Inserted {inserted:,} rows in {elapsed:.1f}s "
              f"({inserted/elapsed:,.0f} rows/s)")
        print(f"[{county_id}] Skipped: empty 號={skipped_no_number}, "
              f"unknown district={skipped_unknown_code}, "
              f"dirty={skipped_dirty}")

        # Build FTS5 index after bulk insert (much faster than per-row triggers)
        print(f"[{county_id}] Building FTS5 index...")
        t1 = time.time()
        conn.execute("INSERT INTO places_fts(places_fts) VALUES('rebuild')")
        conn.commit()
        print(f"[{county_id}] FTS5 built in {time.time()-t1:.1f}s")

        # Populate spatial R*Tree (each row is a point, so min=max).
        # Doing this after the FTS5 rebuild because the rebuild scans
        # the whole table; ordering doesn't matter functionally.
        print(f"[{county_id}] Populating places_rtree...")
        t2 = time.time()
        conn.execute(
            "INSERT INTO places_rtree (id, min_lat, max_lat, min_lon, max_lon)"
            " SELECT id, lat, lat, lon, lon FROM places"
        )
        conn.commit()
        print(f"[{county_id}] R*Tree built in {time.time()-t2:.1f}s")

        # Metadata
        meta = {
            "schema_version": SCHEMA_VERSION,
            "source": "tgos",
            "county": src["county_name"],
            "data_date": src["data_date"],
            "csv_path": src["path"],
            "csv_sha256": src_sha,
            "crs": src["crs"],
            "inserted": str(inserted),
            "skipped_no_number": str(skipped_no_number),
            "skipped_unknown_code": str(skipped_unknown_code),
            "skipped_dirty": str(skipped_dirty),
        }
        conn.executemany(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            list(meta.items()),
        )
        conn.commit()

        # Now in /tmp, VACUUM is safe and shrinks the file.
        print(f"[{county_id}] VACUUM + ANALYZE...")
        try:
            conn.execute("VACUUM")
            conn.execute("ANALYZE")
        except sqlite3.OperationalError as e:
            print(f"[{county_id}] VACUUM/ANALYZE warning: {e}")

        result_inserted = inserted
    finally:
        conn.close()

    # Move the completed sqlite from /tmp to the mounted output volume.
    # Use shutil.move (cross-fs falls back to copy + remove), then verify.
    print(f"[{county_id}] Moving {tmp_path} → {out_path}")
    shutil.move(str(tmp_path), str(out_path))
    size_mb = out_path.stat().st_size / (1 << 20)
    print(f"[{county_id}] DONE — {size_mb:.1f} MB at {out_path}")
    return result_inserted


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--county", required=True, help="County id from csv_sources.yaml")
    args = p.parse_args()
    rc = ingest(args.county)
    return 0 if rc > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
