"""Reference reverse-geocode walk-through over the generated SQLite kits.

Demonstrates the three-tier lookup an ATAK plugin can implement:

  Tier 1 — Township       : (lat, lon) → "台中市北屯區"
  Tier 2 — Nearest road   : (lat, lon) → "中山路三段"
  Tier 3 — Nearest house  : (lat, lon) → "台中市北屯區大誠街39巷2-3-2號"

Tier 1+2 use the R*Tree indexes already in townships.sqlite / roads.sqlite.
Tier 3 currently sequential-scans places-*.sqlite filtered to the township
returned by tier 1 — fast enough for one-off lookups (~50 ms typical).

Usage (inside container, or natively if you have the deps):
    python3 scripts/reverse_geocode.py 24.1454 120.6786
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
import time
from pathlib import Path

import shapely.wkb
from shapely.geometry import Point
from shapely.ops import nearest_points

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin(math.radians(lat2 - lat1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Tier 1: township polygon-in
# ---------------------------------------------------------------------------

def lookup_township(
    townships_db: Path, lat: float, lon: float, snap_tolerance_m: float = 0.0,
) -> dict:
    """Return {county, district, approx} via admin polygon containment.

    With the MOI-sourced schema, each 鄉鎮市區 row carries its parent 縣市
    inline (``county_zh``), so a single level-7/8 hit yields both halves and
    the level-4 query is skipped. The legacy OSM schema (no ``county_zh``)
    still falls back to the level-4 polygon-in for the county.

    ``snap_tolerance_m`` > 0 enables a coastline tolerance: if no polygon
    covers the point (e.g. an address on harbour-reclaimed land that sits
    seaward of the legal MOI boundary), snap to the nearest township polygon
    within that many metres and set ``approx=True``.
    """
    conn = sqlite3.connect(str(townships_db))
    has_county = any(
        r[1] == "county_zh"
        for r in conn.execute("PRAGMA table_info(townships)").fetchall()
    )
    county_col = "t.county_zh" if has_county else "NULL"
    pt = Point(lon, lat)
    out = {"county": None, "district": None, "approx": False}

    # Try level 8 (縣轄鄉鎮市) then level 7 (直轄市區) → district (+ county).
    for lvl in (8, 7):
        rows = conn.execute(
            f"SELECT t.name_zh, {county_col}, t.geometry_wkb "
            "FROM townships t JOIN townships_rtree r ON r.id = t.id "
            "WHERE t.admin_level = ? "
            "  AND r.min_lat <= ? AND ? <= r.max_lat "
            "  AND r.min_lon <= ? AND ? <= r.max_lon",
            (lvl, lat, lat, lon, lon),
        ).fetchall()
        for name, county, wkb in rows:
            if shapely.wkb.loads(wkb).covers(pt):
                out["district"] = name
                out["county"] = county
                break
        if out["district"]:
            break

    # County: from the level-4 polygon when not already supplied inline.
    if out["county"] is None:
        rows = conn.execute(
            "SELECT t.name_zh, t.geometry_wkb "
            "FROM townships t JOIN townships_rtree r ON r.id = t.id "
            "WHERE t.admin_level = 4 "
            "  AND r.min_lat <= ? AND ? <= r.max_lat "
            "  AND r.min_lon <= ? AND ? <= r.max_lon",
            (lat, lat, lon, lon),
        ).fetchall()
        for name, wkb in rows:
            if shapely.wkb.loads(wkb).covers(pt):
                out["county"] = name
                break

    # Coastline tolerance: snap to the nearest township within N metres.
    if snap_tolerance_m > 0 and out["district"] is None:
        deg = snap_tolerance_m / 111_000.0  # ~metres → degrees latitude
        best_d, best = float("inf"), None
        rows = conn.execute(
            f"SELECT t.name_zh, {county_col}, t.geometry_wkb "
            "FROM townships t JOIN townships_rtree r ON r.id = t.id "
            "WHERE t.admin_level IN (7, 8) "
            "  AND r.min_lat <= ? AND ? <= r.max_lat "
            "  AND r.min_lon <= ? AND ? <= r.max_lon",
            (lat + deg, lat - deg, lon + deg, lon - deg),
        ).fetchall()
        for name, county, wkb in rows:
            geom = shapely.wkb.loads(wkb)
            nearest = nearest_points(geom, pt)[0]
            d = _haversine_m(lat, lon, nearest.y, nearest.x)
            if d < best_d:
                best_d, best = d, (name, county)
        if best and best_d <= snap_tolerance_m:
            out["district"], snapped_county = best[0], best[1]
            out["approx"] = True
            if out["county"] is None:
                out["county"] = snapped_county

    conn.close()
    return out


# ---------------------------------------------------------------------------
# Tier 2: nearest road via R*Tree + LineString distance
# ---------------------------------------------------------------------------

def lookup_nearest_road(
    roads_db: Path, lat: float, lon: float, search_radius_deg: float = 0.01,
) -> dict:
    """Find the nearest named road within ~1 km (deg ≈ 111 km * 0.01)."""
    conn = sqlite3.connect(str(roads_db))
    rows = conn.execute(
        "SELECT roads.name_zh, roads.highway, roads.geometry_wkb "
        "FROM roads JOIN roads_rtree ON roads.id = roads_rtree.id "
        "WHERE roads_rtree.min_lat <= ? AND ? <= roads_rtree.max_lat "
        "  AND roads_rtree.min_lon <= ? AND ? <= roads_rtree.max_lon",
        (lat + search_radius_deg, lat - search_radius_deg,
         lon + search_radius_deg, lon - search_radius_deg),
    ).fetchall()
    conn.close()

    if not rows:
        return {"name": None, "distance_m": None}

    pt = Point(lon, lat)
    best = None
    best_d = float("inf")
    for name, hwy, wkb in rows:
        line = shapely.wkb.loads(wkb)
        # shapely distance is in degrees; project to nearest point and
        # convert via haversine for a real metric.
        nearest = line.interpolate(line.project(pt))
        d = _haversine_m(lat, lon, nearest.y, nearest.x)
        if d < best_d:
            best_d, best = d, (name, hwy)
    return {"name": best[0], "highway": best[1], "distance_m": best_d}


# ---------------------------------------------------------------------------
# Tier 3: nearest address point (sequential scan after township filter)
# ---------------------------------------------------------------------------

def _schema_version(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()
        return row[0] if row else "1"
    except sqlite3.OperationalError:
        return "1"


def lookup_nearest_address(
    sqlite_path: Path, lat: float, lon: float, township: str | None,
    bbox_deg: float = 0.001,
) -> dict | None:
    """Find the closest address row in the given sqlite.

    v2 path (places_rtree present): bbox prefilter via R*Tree, then
    haversine over a few hundred candidates. Sub-200ms for 1.3M rows.

    v1 fallback (no R*Tree): scan rows restricted by township (still
    works, just slow). Plugins SHOULD prefer v2 — see
    docs/data-contract.md §5.3.
    """
    if not sqlite_path.exists():
        return None
    conn = sqlite3.connect(str(sqlite_path))
    version = _schema_version(conn)

    rows: list[tuple]
    if version == "2":
        # v2: R*Tree bbox query
        rows = conn.execute(
            "SELECT p.display_name, p.display_name_halfwidth, p.lat, p.lon "
            "FROM places p JOIN places_rtree r ON r.id = p.id "
            "WHERE r.min_lat <= ? AND ? <= r.max_lat "
            "  AND r.min_lon <= ? AND ? <= r.max_lon",
            (lat + bbox_deg, lat - bbox_deg,
             lon + bbox_deg, lon - bbox_deg),
        ).fetchall()
        # Iterative widening — only the dense urban first hit avoids the
        # expansion. 0.001 → 0.005 → 0.02 covers ~100m → ~2km windows.
        widen = 5
        while not rows and widen <= 25:
            rows = conn.execute(
                "SELECT p.display_name, p.display_name_halfwidth, p.lat, p.lon "
                "FROM places p JOIN places_rtree r ON r.id = p.id "
                "WHERE r.min_lat <= ? AND ? <= r.max_lat "
                "  AND r.min_lon <= ? AND ? <= r.max_lon",
                (lat + bbox_deg * widen, lat - bbox_deg * widen,
                 lon + bbox_deg * widen, lon - bbox_deg * widen),
            ).fetchall()
            widen *= 5
    else:
        # v1 fallback
        sql = ("SELECT display_name, display_name_halfwidth, lat, lon "
               "FROM places")
        params: list = []
        if township:
            sql += " WHERE township = ?"
            params.append(township)
        rows = conn.execute(sql, params).fetchall()

    conn.close()
    if not rows:
        return None

    best = None
    best_d = float("inf")
    for display, display_hw, plat, plon in rows:
        d = _haversine_m(lat, lon, plat, plon)
        if d < best_d:
            best_d, best = d, (display, display_hw)
    return {
        "display_name": best[0],
        "display_name_halfwidth": best[1],
        "distance_m": best_d,
        "schema_version": version,
        "candidates_scanned": len(rows),
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("lat", type=float)
    p.add_argument("lon", type=float)
    p.add_argument("--no-tier3", action="store_true",
                   help="Skip the slow nearest-address scan")
    p.add_argument("--snap-m", type=float, default=0.0,
                   help="Coastline tolerance (metres): if no township covers "
                        "the point, snap to the nearest one within this range "
                        "(0 = off). Useful for harbour-reclaimed addresses.")
    args = p.parse_args()

    townships_db = OUTPUT_DIR / "townships.sqlite"
    roads_db = OUTPUT_DIR / "roads.sqlite"

    print(f"\nReverse geocode → ({args.lat}, {args.lon})\n")

    # Tier 1
    t0 = time.time()
    t = lookup_township(townships_db, args.lat, args.lon, snap_tolerance_m=args.snap_m)
    approx = " (approx, snapped)" if t.get("approx") else ""
    print(f"Tier 1 (township):  {t['county']} {t['district']}{approx}    "
          f"({(time.time() - t0)*1000:.1f} ms)")

    # Tier 2
    t0 = time.time()
    r = lookup_nearest_road(roads_db, args.lat, args.lon)
    if r["name"]:
        print(f"Tier 2 (road):      {r['name']} ({r['highway']}), "
              f"{r['distance_m']:.1f} m away    "
              f"({(time.time() - t0)*1000:.1f} ms)")
    else:
        print(f"Tier 2 (road):      no named road within 1km")

    # Tier 3
    if args.no_tier3:
        return 0
    # Decide which county sqlite to scan (based on tier 1 result)
    county_to_db = {
        "台中市": "places-taichung.sqlite",
        "彰化縣": "places-changhua.sqlite",
    }
    db_file = county_to_db.get(t["county"], "places-osm.sqlite")
    db_path = OUTPUT_DIR / db_file
    t0 = time.time()
    a = lookup_nearest_address(db_path, args.lat, args.lon, t["district"])
    if a:
        print(f"Tier 3 (address):   {a['display_name']}    "
              f"({a['distance_m']:.1f} m away, "
              f"{(time.time() - t0)*1000:.1f} ms, "
              f"v{a['schema_version']}, scanned {a['candidates_scanned']} from {db_file})")
        print(f"                    halfwidth: {a['display_name_halfwidth']}")
    else:
        print(f"Tier 3 (address):   no address rows in {db_file}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
