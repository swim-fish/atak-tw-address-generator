"""Extract OSM-derived places into ``places-osm.sqlite``.

Two categories of entries are kept:

1. **Landmarks** — nodes with ``place=city|town|village|hamlet|suburb|
   neighbourhood`` (geographic features that are not buildings).
   Kept *everywhere* inside the region bbox.

2. **Address points** — nodes with ``addr:housenumber`` set.
   Kept *only outside Taichung + Changhua*, because TGOS already gives us
   complete official coverage for those counties. We resolve "inside" via
   point-in-polygon against the admin_level=4 polygons in
   ``townships.sqlite``.

The output schema matches ``places-<county>.sqlite`` so the plugin can
``glob places-*.sqlite`` and treat them uniformly.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import osmium
import shapely.wkb
from shapely.geometry import Point
import yaml

import normalize_address as na

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

NAME_NORMALISE = str.maketrans({"臺": "台"})

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA temp_store = MEMORY;

-- Same schema as places-<county>.sqlite for plugin uniformity.
CREATE TABLE places (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    osm_id INTEGER,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    name TEXT,
    display_name TEXT NOT NULL,
    display_name_halfwidth TEXT NOT NULL,
    district_code TEXT,
    county TEXT,
    township TEXT,
    village TEXT,
    neighbor TEXT,
    street TEXT,
    area TEXT,
    lane TEXT,
    alley TEXT,
    number TEXT,
    place_type TEXT
);

CREATE INDEX idx_places_county ON places(county);

CREATE VIRTUAL TABLE places_fts USING fts5(
    name, display_name, display_name_halfwidth, street, township,
    content='places',
    content_rowid='id',
    tokenize='unicode61'
);

CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def name_zh(tags: dict) -> str | None:
    for k in ("name:zh", "name:zh-Hant", "name"):
        v = tags.get(k)
        if v:
            return v.translate(NAME_NORMALISE)
    return None


def load_excluded_polygons(townships_db: Path, exclude_names: set[str]):
    """Load admin_level=4 polygons matching exclude_names (e.g. 台中市/彰化縣).

    Returns a list of (min_lat, max_lat, min_lon, max_lon, geometry) for
    O(log n) bbox prefilter before the more expensive shapely covers().
    """
    if not townships_db.exists():
        print(f"WARN: townships.sqlite missing; no TGOS-area exclusion applied",
              file=sys.stderr)
        return []
    conn = sqlite3.connect(str(townships_db))
    rows = conn.execute(
        "SELECT t.name_zh, t.geometry_wkb,"
        "       r.min_lat, r.max_lat, r.min_lon, r.max_lon"
        " FROM townships t JOIN townships_rtree r ON r.id = t.id"
        " WHERE t.admin_level = 4 AND t.name_zh IN (" +
        ",".join(["?"] * len(exclude_names)) + ")",
        list(exclude_names),
    ).fetchall()
    conn.close()
    return [(mn_lat, mx_lat, mn_lon, mx_lon, shapely.wkb.loads(wkb), name)
            for name, wkb, mn_lat, mx_lat, mn_lon, mx_lon in rows]


def point_in_excluded(excluded, lat: float, lon: float) -> bool:
    pt = Point(lon, lat)
    for mn_lat, mx_lat, mn_lon, mx_lon, geom, _ in excluded:
        if mn_lat <= lat <= mx_lat and mn_lon <= lon <= mx_lon:
            if geom.covers(pt):
                return True
    return False


PLACE_VALUES = {"city", "town", "village", "hamlet", "suburb", "neighbourhood"}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--region", default="tw-central")
    args = p.parse_args()

    region_cfg = yaml.safe_load((CONFIG_DIR / "regions.yaml").read_text("utf-8"))[args.region]
    bbox = region_cfg["bbox"]
    pbf = CACHE_DIR / f"{args.region}.osm.pbf"
    if not pbf.exists():
        print(f"ERROR: clipped PBF missing: {pbf}", file=sys.stderr)
        return 2

    # Read region config to figure out which county polygons we should
    # exclude addr:* points within (TGOS coverage areas).
    csv_sources = yaml.safe_load((CONFIG_DIR / "csv_sources.yaml").read_text("utf-8"))
    exclude_county_names = {
        csv_sources[cid]["county_name"].translate(NAME_NORMALISE)
        for cid in region_cfg.get("tgos_counties", [])
    }
    townships_db = OUTPUT_DIR / "townships.sqlite"
    excluded = load_excluded_polygons(townships_db, exclude_county_names)
    print(f"[places-osm] excluding addr:* inside: {exclude_county_names}")
    print(f"[places-osm]   loaded {len(excluded)} admin_level=4 polygons")

    out = OUTPUT_DIR / "places-osm.sqlite"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    conn = sqlite3.connect(str(out))
    conn.executescript(SCHEMA_SQL)

    landmarks = 0
    addrs_kept = 0
    addrs_excluded = 0

    t0 = time.time()
    print(f"[places-osm] reading {pbf}")
    fp = osmium.FileProcessor(str(pbf))
    for obj in fp:
        if not obj.is_node():
            continue
        tags = dict(obj.tags)
        lat, lon = obj.location.lat, obj.location.lon

        # Category 1: place=* landmarks
        place_type = tags.get("place")
        if place_type in PLACE_VALUES:
            name = name_zh(tags)
            if not name:
                continue
            display = name
            conn.execute(
                "INSERT INTO places (source, osm_id, lat, lon, name,"
                " display_name, display_name_halfwidth, place_type)"
                " VALUES ('osm', ?, ?, ?, ?, ?, ?, ?)",
                (obj.id, lat, lon, name, display, display, place_type),
            )
            landmarks += 1
            continue

        # Category 2: addr:* points (only outside TGOS-covered counties)
        housenumber = tags.get("addr:housenumber")
        if housenumber:
            if point_in_excluded(excluded, lat, lon):
                addrs_excluded += 1
                continue
            street = tags.get("addr:street", "")
            city = (tags.get("addr:city") or "").translate(NAME_NORMALISE)
            sub = (tags.get("addr:district") or tags.get("addr:suburb") or "")
            display = city + sub + street + housenumber
            display_hw = na.to_halfwidth(display).replace("之", "-")
            short = na.to_halfwidth(street + housenumber).replace("之", "-") or display_hw
            conn.execute(
                "INSERT INTO places (source, osm_id, lat, lon, name,"
                " display_name, display_name_halfwidth,"
                " county, township, street, number)"
                " VALUES ('osm', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (obj.id, lat, lon, short, display, display_hw,
                 city or None, sub or None, street or None, housenumber),
            )
            addrs_kept += 1
    conn.commit()
    elapsed = time.time() - t0

    print(f"[places-osm] landmarks (place=*):  {landmarks}")
    print(f"[places-osm] addr:* kept (outside TGOS):  {addrs_kept}")
    print(f"[places-osm] addr:* excluded (in Tai/Cha): {addrs_excluded}")
    print(f"[places-osm] elapsed: {elapsed:.1f}s")

    print(f"[places-osm] building FTS5 index...")
    t1 = time.time()
    conn.execute("INSERT INTO places_fts(places_fts) VALUES('rebuild')")
    conn.commit()
    print(f"[places-osm]   FTS5 built in {time.time() - t1:.1f}s")

    meta = {
        "schema_version": "1",
        "source": "osm-clipped",
        "region": args.region,
        "bbox": f"{bbox['west']},{bbox['south']},{bbox['east']},{bbox['north']}",
        "excluded_counties": ",".join(sorted(exclude_county_names)),
        "landmarks": str(landmarks),
        "addrs_kept": str(addrs_kept),
        "addrs_excluded": str(addrs_excluded),
    }
    conn.executemany("INSERT INTO metadata (key, value) VALUES (?, ?)", list(meta.items()))
    conn.commit()
    try:
        conn.execute("ANALYZE")
    except sqlite3.OperationalError as e:
        print(f"[places-osm] ANALYZE warning: {e}")
    conn.close()

    size = out.stat().st_size / (1 << 20)
    print(f"[places-osm] OK — {out} ({size:.1f} MiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
