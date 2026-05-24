"""Extract Taiwan administrative polygons from a clipped OSM PBF.

We pull both ``admin_level=6`` (縣/市) and ``admin_level=8`` (鄉鎮市區) because:

- The plugin needs both granularities for reverse geocoding (a marker
  centroid wants both 「彰化縣」 and 「鹿港鎮」).
- OSM names polygons with just the bare admin name (e.g. ``北屯區``
  with no county prefix); the county membership is inferred at runtime
  from spatial containment with admin_level=6.

Output: ``output/townships.sqlite``
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import osmium
import osmium.geom
import shapely.wkb
import yaml

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA temp_store = MEMORY;

CREATE TABLE townships (
    id INTEGER PRIMARY KEY,
    osm_id INTEGER NOT NULL,
    admin_level INTEGER NOT NULL,
    name_zh TEXT NOT NULL,
    name_en TEXT,
    geometry_wkb BLOB NOT NULL
);

CREATE INDEX idx_townships_level ON townships(admin_level);
CREATE INDEX idx_townships_name ON townships(name_zh);

CREATE VIRTUAL TABLE townships_rtree USING rtree(
    id, min_lat, max_lat, min_lon, max_lon
);

CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Tags we care about for admin polygons.
#   level=4 — 縣 + 直轄市 (台灣 OSM convention; level=6 is unused here)
#   level=7 — 直轄市的區 (e.g. 台中市北屯區)
#   level=8 — 縣轄的 鄉/鎮/市 (e.g. 彰化縣鹿港鎮)
WANT_LEVELS = ("4", "7", "8")

# OSM names use the traditional glyph 「臺」 for 臺中市/臺北市/臺南市/臺東縣;
# TGOS / MOI uses the simplified 「台」. Normalise to match TGOS for
# downstream joins and UI consistency.
NAME_NORMALISE = str.maketrans({"臺": "台"})


def name_zh(tags: dict) -> str | None:
    """Pick the best Chinese name from the OSM tags."""
    for k in ("name:zh", "name:zh-Hant", "name"):
        v = tags.get(k)
        if v:
            return v.translate(NAME_NORMALISE)
    return None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--region", default="tw-central")
    args = p.parse_args()

    region_cfg = yaml.safe_load((CONFIG_DIR / "regions.yaml").read_text("utf-8"))[args.region]
    bbox = region_cfg["bbox"]

    pbf_clipped = CACHE_DIR / f"{args.region}.osm.pbf"
    if not pbf_clipped.exists():
        print(f"ERROR: clipped PBF missing: {pbf_clipped}\n"
              f"       run scripts/clip_pbf.py first", file=sys.stderr)
        return 2
    # County (admin_level=6) polygons span the full county; they cannot be
    # reassembled from a clipped extract, so we walk the full PBF for them
    # and filter by bbox overlap after assembly.
    geofabrik_name = region_cfg["geofabrik_region"].split("/")[-1]
    pbf_full = CACHE_DIR / f"{geofabrik_name}-latest.osm.pbf"
    if not pbf_full.exists():
        print(f"WARN: full PBF missing ({pbf_full}); level=6 will be empty",
              file=sys.stderr)
        pbf_full = None

    out = OUTPUT_DIR / "townships.sqlite"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    conn = sqlite3.connect(str(out))
    conn.executescript(SCHEMA_SQL)

    wkb_factory = osmium.geom.WKBFactory()

    inserted = {4: 0, 7: 0, 8: 0}
    rejected_no_name = 0
    rejected_bad_geom = 0

    def bbox_overlaps(minx, miny, maxx, maxy) -> bool:
        return not (maxx < bbox["west"] or minx > bbox["east"]
                    or maxy < bbox["south"] or miny > bbox["north"])

    def consume(pbf_path: Path, accept_levels: set[str]) -> None:
        nonlocal rejected_no_name, rejected_bad_geom
        print(f"[townships] reading {pbf_path} (levels {sorted(accept_levels)})")
        fp = osmium.FileProcessor(str(pbf_path)).with_areas()
        for obj in fp:
            if not obj.is_area():
                continue
            tags = dict(obj.tags)
            if tags.get("boundary") != "administrative":
                continue
            level = tags.get("admin_level")
            if level not in accept_levels:
                continue
            name = name_zh(tags)
            if not name:
                rejected_no_name += 1
                continue
            try:
                hex_wkb = wkb_factory.create_multipolygon(obj)
                geom_bytes = bytes.fromhex(hex_wkb)
                geom = shapely.wkb.loads(geom_bytes)
                minx, miny, maxx, maxy = geom.bounds
            except Exception:
                rejected_bad_geom += 1
                continue
            if not bbox_overlaps(minx, miny, maxx, maxy):
                continue
            cur = conn.execute(
                "INSERT INTO townships (osm_id, admin_level, name_zh, name_en, geometry_wkb)"
                " VALUES (?, ?, ?, ?, ?)",
                (obj.id, int(level), name, tags.get("name:en"), geom_bytes),
            )
            conn.execute(
                "INSERT INTO townships_rtree (id, min_lat, max_lat, min_lon, max_lon)"
                " VALUES (?, ?, ?, ?, ?)",
                (cur.lastrowid, miny, maxy, minx, maxx),
            )
            inserted[int(level)] += 1
        conn.commit()

    t0 = time.time()
    # level 7 (直轄市的區) and level 8 (縣轄鄉鎮市) from the clipped PBF —
    # both are small enough to assemble correctly within the bbox.
    consume(pbf_clipped, {"7", "8"})
    # level 4 (縣 / 直轄市) cannot be reassembled from the clip because the
    # county polygons span the bbox; walk the full PBF for them and filter.
    if pbf_full is not None:
        consume(pbf_full, {"4"})
    elapsed = time.time() - t0

    print(f"[townships] inserted level=4 (縣市):     {inserted[4]}")
    print(f"[townships] inserted level=7 (直轄市區): {inserted[7]}")
    print(f"[townships] inserted level=8 (鄉鎮市區): {inserted[8]}")
    print(f"[townships] rejected no-name:          {rejected_no_name}")
    print(f"[townships] rejected bad geom:         {rejected_bad_geom}")
    print(f"[townships] elapsed: {elapsed:.1f}s")

    meta = {
        "schema_version": "1",
        "source": "osm-clipped",
        "region": args.region,
        "bbox": f"{bbox['west']},{bbox['south']},{bbox['east']},{bbox['north']}",
        "inserted_level4": str(inserted[4]),
        "inserted_level7": str(inserted[7]),
        "inserted_level8": str(inserted[8]),
    }
    conn.executemany("INSERT INTO metadata (key, value) VALUES (?, ?)", list(meta.items()))
    conn.commit()

    try:
        conn.execute("ANALYZE")
    except sqlite3.OperationalError as e:
        print(f"[townships] ANALYZE warning: {e}")
    conn.close()

    size = out.stat().st_size / (1 << 20)
    print(f"[townships] OK — {out} ({size:.1f} MiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
