"""Build Taiwan administrative polygons from the MOI authoritative boundary
shapefiles (內政部 直轄市/縣市界線 + 鄉鎮市區界線).

Why MOI instead of OSM:

- **Authoritative.** These are the Ministry of the Interior's official
  boundaries (release 1140318), the legal ground truth — not the OSM
  community's crowd-sourced approximation.
- **County membership is inline.** Every 鄉鎮市區 polygon in ``TOWN_MOI``
  already carries its ``COUNTYNAME``. The OSM pipeline had to *infer*
  county membership at runtime via admin_level=4 spatial containment;
  here it is a column read, so the plugin can resolve 「彰化縣鹿港鎮」
  from a single polygon hit (see ``county_zh`` below).

Input  (mounted read-only at /app/input):
    input/moi-boundaries/COUNTY_MOI_1140318.{shp,shx,dbf,prj,cpg}   縣市
    input/moi-boundaries/TOWN_MOI_1140318.{shp,shx,dbf,prj,cpg}     鄉鎮市區
    input/moi-boundaries/Town_Majia_Sanhe.{shp,shx,dbf,...}         屏東縣瑪家鄉三和段飛地

Output: ``output/townships.sqlite``  (schema-compatible with the former
OSM-derived layout; see docs/data-contract.md §3.2).

The shapefiles are in GCS_TWD97[2020] geographic coordinates (lon/lat
degrees). For ATAK / WGS84 reverse-geocoding the datum difference is
sub-metre, so the coordinates are stored verbatim as WGS84 lon/lat —
matching the (lat, lon) the plugin queries with.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

import shapefile  # pyshp
import shapely.geometry
import shapely.wkb
import yaml

INPUT_DIR = Path(__file__).resolve().parent.parent / "input"
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
MOI_DIR = INPUT_DIR / "moi-boundaries"

COUNTY_SHP = MOI_DIR / "COUNTY_MOI_1140318.shp"
TOWN_SHP = MOI_DIR / "TOWN_MOI_1140318.shp"
# Detached-part shapefiles MOI ships separately from the main TOWN layer.
#
# Per 內政部國土測繪中心: 屏東縣瑪家鄉三和村 is an enclave (飛地) NOT
# adjacent to the 瑪家鄉 main body; it sits between 屏東縣鹽埔鄉 / 長治鄉 /
# 內埔鄉, and its extent OVERLAPS the existing 鄉鎮市區界線 because the
# county's administrative-overlap coordination is unfinished. MOI therefore
# supplies it as a standalone polygon (Town_Majia_Sanhe) to mark 三和村's
# current status — it is deliberately NOT merged into TOWN_MOI, which would
# otherwise stop being a clean non-overlapping partition.
#
# We honour that: by default the main TOWN layer stays a deterministic
# partition (every point resolves to exactly one 鄉鎮市區). Pass
# --include-detached-parts to add the enclave as an extra 瑪家鄉 polygon,
# accepting that points in 禮納里/三和村 then match BOTH 瑪家鄉 and their
# host township (order-dependent in a point-in-polygon walk). County-level
# identification is unaffected either way — the enclave is wholly inside
# 屏東縣.
TOWN_EXTRA_SHPS = (MOI_DIR / "Town_Majia_Sanhe.shp",)

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA temp_store = MEMORY;

CREATE TABLE townships (
    id INTEGER PRIMARY KEY,
    moi_code TEXT NOT NULL,           -- MOI COUNTYCODE / TOWNCODE (provenance)
    admin_level INTEGER NOT NULL,     -- 4=縣市, 7=直轄市區, 8=縣轄鄉鎮市
    name_zh TEXT NOT NULL,            -- bare name, normalised 「臺」→「台」
    name_en TEXT,
    county_zh TEXT,                   -- parent 縣市 for level 7/8; NULL for level 4
    geometry_wkb BLOB NOT NULL        -- MultiPolygon, WGS84 lon/lat
);

CREATE INDEX idx_townships_level  ON townships(admin_level);
CREATE INDEX idx_townships_name   ON townships(name_zh);
CREATE INDEX idx_townships_county ON townships(county_zh);

CREATE VIRTUAL TABLE townships_rtree USING rtree(
    id, min_lat, max_lat, min_lon, max_lon
);

CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# MOI shapefiles store 臺中市/臺北市/臺南市/臺東縣 with the traditional glyph
# 「臺」. TGOS CSVs and every downstream join use 「台」. Normalise to 台.
NAME_NORMALISE = str.maketrans({"臺": "台"})

# The six 直轄市. Their sub-divisions are 區 at admin_level=7; every other
# county's sub-divisions are 鄉/鎮/(縣轄)市 at admin_level=8. Names here are
# already 臺→台 normalised.
SPECIAL_MUNICIPALITIES = frozenset(
    {"台北市", "新北市", "桃園市", "台中市", "台南市", "高雄市"}
)


def norm(s: str | None) -> str | None:
    return s.translate(NAME_NORMALISE).strip() if s else s


def to_multipolygon_wkb(geo_interface: dict):
    """Return (wkb_bytes, (minx, miny, maxx, maxy)) as a WGS84 MultiPolygon."""
    geom = shapely.geometry.shape(geo_interface)
    if geom.is_empty:
        raise ValueError("empty geometry")
    if geom.geom_type == "Polygon":
        geom = shapely.geometry.MultiPolygon([geom])
    elif geom.geom_type != "MultiPolygon":
        raise ValueError(f"unexpected geometry type {geom.geom_type}")
    return geom.wkb, geom.bounds


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--region", default="tw-central",
                   help="region key in config/regions.yaml; use 'all' to keep "
                        "every county/township nationwide")
    p.add_argument("--include-detached-parts", action="store_true",
                   help="add MOI detached-part polygons (e.g. 瑪家鄉三和村 "
                        "enclave). Off by default: these overlap the main "
                        "TOWN layer, making township lookup ambiguous.")
    args = p.parse_args()

    regions = yaml.safe_load((CONFIG_DIR / "regions.yaml").read_text("utf-8"))
    if args.region == "all":
        bbox = None
    else:
        bbox = regions[args.region]["bbox"]

    for shp in (COUNTY_SHP, TOWN_SHP):
        if not shp.exists():
            print(f"ERROR: missing MOI shapefile: {shp}\n"
                  f"       expected under {MOI_DIR}", file=sys.stderr)
            return 2

    out = OUTPUT_DIR / "townships.sqlite"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    conn = sqlite3.connect(str(out))
    conn.executescript(SCHEMA_SQL)

    inserted = {4: 0, 7: 0, 8: 0}
    rejected_bbox = 0
    rejected_bad_geom = 0

    def bbox_overlaps(b) -> bool:
        # Keep the *whole* polygon if its bounds overlap the region window,
        # so county/township shapes are never clipped (mirrors the OSM
        # pipeline's overlap-not-clip behaviour).
        if bbox is None:
            return True
        minx, miny, maxx, maxy = b
        return not (maxx < bbox["west"] or minx > bbox["east"]
                    or maxy < bbox["south"] or miny > bbox["north"])

    def insert(level: int, code: str, name_zh: str, name_en: str | None,
               county_zh: str | None, wkb: bytes, b) -> None:
        minx, miny, maxx, maxy = b
        cur = conn.execute(
            "INSERT INTO townships"
            " (moi_code, admin_level, name_zh, name_en, county_zh, geometry_wkb)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (code, level, name_zh, name_en, county_zh, wkb),
        )
        conn.execute(
            "INSERT INTO townships_rtree (id, min_lat, max_lat, min_lon, max_lon)"
            " VALUES (?, ?, ?, ?, ?)",
            (cur.lastrowid, miny, maxy, minx, maxx),
        )
        inserted[level] += 1

    t0 = time.time()

    # --- level 4: 縣市 (COUNTY_MOI) ---
    reader = shapefile.Reader(str(COUNTY_SHP), encoding="utf-8")
    for sr in reader.iterShapeRecords():
        rec = sr.record
        name = norm(rec["COUNTYNAME"])
        try:
            wkb, b = to_multipolygon_wkb(sr.shape.__geo_interface__)
        except Exception:
            rejected_bad_geom += 1
            continue
        if not bbox_overlaps(b):
            rejected_bbox += 1
            continue
        insert(4, str(rec["COUNTYCODE"]).strip(), name, rec["COUNTYENG"],
               None, wkb, b)
    reader.close()

    # --- level 7/8: 鄉鎮市區 (TOWN_MOI [+ detached parts if opted in]) ---
    town_shps = (TOWN_SHP, *TOWN_EXTRA_SHPS) if args.include_detached_parts \
        else (TOWN_SHP,)
    for town_shp in town_shps:
        if not town_shp.exists():
            print(f"WARN: detached-part shapefile missing, skipping: {town_shp}",
                  file=sys.stderr)
            continue
        reader = shapefile.Reader(str(town_shp), encoding="utf-8")
        for sr in reader.iterShapeRecords():
            rec = sr.record
            county = norm(rec["COUNTYNAME"])
            name = norm(rec["TOWNNAME"])
            level = 7 if county in SPECIAL_MUNICIPALITIES else 8
            try:
                wkb, b = to_multipolygon_wkb(sr.shape.__geo_interface__)
            except Exception:
                rejected_bad_geom += 1
                continue
            if not bbox_overlaps(b):
                rejected_bbox += 1
                continue
            insert(level, str(rec["TOWNCODE"]).strip(), name, rec["TOWNENG"],
                   county, wkb, b)
        reader.close()

    conn.commit()
    elapsed = time.time() - t0

    print(f"[townships] inserted level=4 (縣市):     {inserted[4]}")
    print(f"[townships] inserted level=7 (直轄市區): {inserted[7]}")
    print(f"[townships] inserted level=8 (鄉鎮市區): {inserted[8]}")
    print(f"[townships] rejected outside bbox:      {rejected_bbox}")
    print(f"[townships] rejected bad geom:          {rejected_bad_geom}")
    print(f"[townships] elapsed: {elapsed:.1f}s")

    region_label = args.region
    meta = {
        "schema_version": "1",
        "source": "moi-shapefile",
        "boundary_release": "1140318",
        "detached_parts": "included" if args.include_detached_parts else "excluded",
        "region": region_label,
        "bbox": ("nationwide" if bbox is None else
                 f"{bbox['west']},{bbox['south']},{bbox['east']},{bbox['north']}"),
        "inserted_level4": str(inserted[4]),
        "inserted_level7": str(inserted[7]),
        "inserted_level8": str(inserted[8]),
    }
    conn.executemany("INSERT INTO metadata (key, value) VALUES (?, ?)",
                     list(meta.items()))
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
