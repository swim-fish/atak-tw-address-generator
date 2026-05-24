"""Extract named roads from a clipped OSM PBF.

We pull OSM ways tagged ``highway=*`` with a ``name`` (the highway types
are configured in ``config/tag_filter.yaml``). The geometry is stored as
WKB LineString in WGS84, with bounding box mirrored into a SQLite R*Tree
for fast nearest-road reverse-lookup at runtime.

Output: ``output/roads.sqlite``
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

CREATE TABLE roads (
    id INTEGER PRIMARY KEY,
    osm_id INTEGER NOT NULL,
    name_zh TEXT NOT NULL,
    name_en TEXT,
    highway TEXT NOT NULL,
    geometry_wkb BLOB NOT NULL
);

CREATE INDEX idx_roads_name ON roads(name_zh);
CREATE INDEX idx_roads_highway ON roads(highway);

CREATE VIRTUAL TABLE roads_rtree USING rtree(
    id, min_lat, max_lat, min_lon, max_lon
);

CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# OSM uses 「臺」 for 臺中市 etc.; TGOS / MOI uses 「台」. Keep them aligned.
NAME_NORMALISE = str.maketrans({"臺": "台"})


def name_zh(tags: dict) -> str | None:
    for k in ("name:zh", "name:zh-Hant", "name"):
        v = tags.get(k)
        if v:
            return v.translate(NAME_NORMALISE)
    return None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--region", default="tw-central")
    args = p.parse_args()

    tag_filter = yaml.safe_load((CONFIG_DIR / "tag_filter.yaml").read_text("utf-8"))
    region_cfg = yaml.safe_load((CONFIG_DIR / "regions.yaml").read_text("utf-8"))[args.region]
    highway_set = set(tag_filter["roads"]["highway_values"])

    pbf = CACHE_DIR / f"{args.region}.osm.pbf"
    if not pbf.exists():
        print(f"ERROR: clipped PBF missing: {pbf}\n"
              f"       run scripts/clip_pbf.py first", file=sys.stderr)
        return 2

    out = OUTPUT_DIR / "roads.sqlite"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    conn = sqlite3.connect(str(out))
    conn.executescript(SCHEMA_SQL)

    wkb_factory = osmium.geom.WKBFactory()

    inserted = 0
    rejected_no_name = 0
    rejected_unknown_highway = 0
    rejected_bad_geom = 0
    batch: list[tuple] = []
    rtree_batch: list[tuple] = []

    t0 = time.time()
    print(f"[roads] reading {pbf}")
    # with_locations adds node coordinates so create_linestring() works.
    fp = osmium.FileProcessor(str(pbf)).with_locations()
    for obj in fp:
        if not obj.is_way():
            continue
        tags = dict(obj.tags)
        hwy = tags.get("highway")
        if hwy not in highway_set:
            rejected_unknown_highway += 1
            continue
        name = name_zh(tags)
        if not name:
            rejected_no_name += 1
            continue
        try:
            hex_wkb = wkb_factory.create_linestring(obj)
            geom_bytes = bytes.fromhex(hex_wkb)
            geom = shapely.wkb.loads(geom_bytes)
            minx, miny, maxx, maxy = geom.bounds
        except Exception:
            rejected_bad_geom += 1
            continue
        cur = conn.execute(
            "INSERT INTO roads (osm_id, name_zh, name_en, highway, geometry_wkb)"
            " VALUES (?, ?, ?, ?, ?)",
            (obj.id, name, tags.get("name:en"), hwy, geom_bytes),
        )
        conn.execute(
            "INSERT INTO roads_rtree (id, min_lat, max_lat, min_lon, max_lon)"
            " VALUES (?, ?, ?, ?, ?)",
            (cur.lastrowid, miny, maxy, minx, maxx),
        )
        inserted += 1
    conn.commit()
    elapsed = time.time() - t0

    print(f"[roads] inserted:                 {inserted}")
    print(f"[roads] rejected no-name:         {rejected_no_name}")
    print(f"[roads] rejected unknown highway: {rejected_unknown_highway}")
    print(f"[roads] rejected bad geom:        {rejected_bad_geom}")
    print(f"[roads] elapsed: {elapsed:.1f}s")

    bbox = region_cfg["bbox"]
    meta = {
        "schema_version": "1",
        "source": "osm-clipped",
        "region": args.region,
        "bbox": f"{bbox['west']},{bbox['south']},{bbox['east']},{bbox['north']}",
        "inserted": str(inserted),
        "highway_filter": ",".join(sorted(highway_set)),
    }
    conn.executemany("INSERT INTO metadata (key, value) VALUES (?, ?)", list(meta.items()))
    conn.commit()

    try:
        conn.execute("ANALYZE")
    except sqlite3.OperationalError as e:
        print(f"[roads] ANALYZE warning: {e}")
    conn.close()

    size = out.stat().st_size / (1 << 20)
    print(f"[roads] OK — {out} ({size:.1f} MiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
