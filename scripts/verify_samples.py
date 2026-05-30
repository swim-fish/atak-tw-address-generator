"""Verification harness for the TGOS-derived places SQLite files.

Per the plan, for each county that has a TGOS source CSV we draw 200
anchor samples — 50 rows from each cardinal extreme (north / south / east
/ west) of the bounding box — and run a set of integrity checks against
the generated SQLite database.

Iteration 1 (this commit) covers four checks:
  1. coverage          row recoverable by (district_code+village+street+lane+alley+number)
  2. coord_match       sqlite (lat, lon) within tolerance of CSV ground truth
  4. display_name      compose round-trips to the expected fullwidth + halfwidth
  5. fts5_search       FTS5 query on display_name_halfwidth returns the row

Iteration 2 will add checks 3 (polygon-in townships) and 6 (reverse
township lookup) once the OSM-derived townships.sqlite exists (step 7).

Usage (inside the container, dispatched by build-data.sh):
    python3 scripts/verify_samples.py
    python3 scripts/verify_samples.py --county taichung
"""
from __future__ import annotations

import argparse
import csv
import math
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import shapely.wkb
from shapely.geometry import Point
import yaml

import coord_transform as ct
import normalize_address as na
from ingest_tgos_csv import load_dirty_set, _DIRTY_KEY_FIELDS

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

# Tolerances
COORD_TOL_M = 1.0     # WGS84 vs CSV (Taichung exact, Changhua reproject)

DIRECTIONS = ("north", "south", "east", "west")


@dataclass
class Sample:
    """One anchor row, with ground-truth lat/lon and source fields."""
    raw: dict
    district_code: str
    village: str
    neighbor: str
    street: str
    area: str
    lane: str
    alley: str
    number: str
    lat: float
    lon: float


@dataclass
class CheckResult:
    """Aggregated counts per (county, direction, check)."""
    passed: int = 0
    failed: int = 0
    info: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)
    """List of (description, detail) for each failed sample."""

    def add(self, ok, detail: str = "") -> None:
        # ok is True (pass), False (fail), or None (info — a known
        # boundary exception that should not count against the build).
        if ok is None:
            self.info += 1
        elif ok:
            self.passed += 1
        else:
            self.failed += 1
            self.failures.append(("", detail))


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------

def load_county_samples(county_id: str, n: int = 50) -> dict[str, list[Sample]]:
    """Return {direction: [Sample x n]} for one county."""
    config = yaml.safe_load((CONFIG_DIR / "csv_sources.yaml").read_text("utf-8"))
    src = config[county_id]
    csv_path = Path(__file__).resolve().parent.parent / src["path"]
    cols = src["columns"]
    is_wgs84 = src["crs"] == "EPSG:4326"

    # Mirror the ingest-side dirty exclusion so we don't sample rows that
    # the operational pipeline deliberately drops.
    dirty_set = load_dirty_set("tgos", county_id, src.get("data_date", ""))

    samples: list[Sample] = []
    with open(csv_path, encoding=src["encoding"], newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                if is_wgs84:
                    lon = float(row[cols["lon"]])
                    lat = float(row[cols["lat"]])
                else:
                    x = float(row[cols["x"]])
                    y = float(row[cols["y"]])
                    lon, lat = ct.twd97_to_wgs84(x, y)
            except (KeyError, ValueError):
                continue
            number = row.get(cols["number"], "").strip()
            if not number:
                continue  # skip CSV-empty 號 rows
            # Skip rows that the dedup_floors stage removes (same-coord
            # floor variants). The ingested sqlite only retains one row per
            # coord, preferring numbers without 樓/層 — so sampling those
            # would produce spurious coverage failures.
            if "樓" in number or "層" in number:
                continue
            district_code = row[cols["district_code"]].strip()
            village = row.get(cols["village"], "").strip()
            neighbor = row.get(cols["neighbor"], "").strip()
            street = row.get(cols["street"], "").strip()
            lane = row.get(cols["lane"], "").strip()
            alley = row.get(cols["alley"], "").strip()
            if dirty_set:
                key = (district_code, village, neighbor, street, lane, alley, number)
                if key in dirty_set:
                    continue  # row excluded by config/dirty_data.yaml
            samples.append(Sample(
                raw=row,
                district_code=district_code,
                village=village,
                neighbor=neighbor,
                street=street,
                area=row.get(cols["area"], "").strip(),
                lane=lane,
                alley=alley,
                number=number,
                lat=lat,
                lon=lon,
            ))

    by_lat = sorted(samples, key=lambda s: s.lat)
    by_lon = sorted(samples, key=lambda s: s.lon)
    return {
        "south": by_lat[:n],
        "north": by_lat[-n:],
        "west":  by_lon[:n],
        "east":  by_lon[-n:],
    }


# ---------------------------------------------------------------------------
# Per-sample checks
# ---------------------------------------------------------------------------

LOOKUP_SQL = """
SELECT id, lat, lon, display_name, display_name_halfwidth, township, county
FROM places
WHERE district_code = ?
  AND COALESCE(village, '')  = ?
  AND COALESCE(neighbor, '') = ?
  AND COALESCE(street, '')   = ?
  AND COALESCE(area, '')     = ?
  AND COALESCE(lane, '')     = ?
  AND COALESCE(alley, '')    = ?
  AND COALESCE(number, '')   = ?
"""

# Even with neighbor + area in the key, a small number of TGOS rows
# legitimately share all eight key fields (typically coastal lots in
# 大城鄉). For those we pick the closest-coord match — the verification
# question is "did our row survive ingestion", not "are these duplicates
# weird".


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1 = math.radians(lat1); p2 = math.radians(lat2)
    a = (math.sin(math.radians(lat2 - lat1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Township polygon helpers (iteration 2)
# ---------------------------------------------------------------------------

def _load_township_index(townships_db: Path) -> tuple[dict[tuple[int, str], list], bool]:
    """Build ({(admin_level, name_zh): [(min_lat,max_lat,min_lon,max_lon, geom,
    county_zh)]}, has_county_zh).

    Pre-loading is fast — at most a few hundred polygons. ``county_zh`` is
    populated only when the MOI-sourced schema provides it (NULL/absent on
    the legacy OSM-derived layout); ``has_county_zh`` tells callers whether
    the county_match check can run.
    """
    conn = sqlite3.connect(str(townships_db))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(townships)").fetchall()}
    has_county = "county_zh" in cols
    county_sql = "t.county_zh" if has_county else "NULL AS county_zh"
    index: dict[tuple[int, str], list] = defaultdict(list)
    rows = conn.execute(
        "SELECT t.admin_level, t.name_zh, t.geometry_wkb,"
        "       r.min_lat, r.max_lat, r.min_lon, r.max_lon, " + county_sql +
        " FROM townships t JOIN townships_rtree r ON r.id = t.id"
    ).fetchall()
    conn.close()
    for lvl, name, wkb, mn_lat, mx_lat, mn_lon, mx_lon, county in rows:
        geom = shapely.wkb.loads(wkb)
        index[(lvl, name)].append((mn_lat, mx_lat, mn_lon, mx_lon, geom, county))
    return index, has_county


def _point_in_township(
    townships_idx: dict, level: int, name: str, lat: float, lon: float,
) -> bool:
    polys = townships_idx.get((level, name))
    if not polys:
        return False
    pt = Point(lon, lat)
    for mn_lat, mx_lat, mn_lon, mx_lon, geom, _county in polys:
        if mn_lat <= lat <= mx_lat and mn_lon <= lon <= mx_lon:
            if geom.covers(pt):
                return True
    return False


def _reverse_township_lookup(
    townships_idx: dict, lat: float, lon: float, level: int,
) -> str | None:
    pt = Point(lon, lat)
    for (lvl, name), polys in townships_idx.items():
        if lvl != level:
            continue
        for mn_lat, mx_lat, mn_lon, mx_lon, geom, _county in polys:
            if mn_lat <= lat <= mx_lat and mn_lon <= lon <= mx_lon:
                if geom.covers(pt):
                    return name
    return None


def _reverse_county_lookup(
    townships_idx: dict, lat: float, lon: float,
) -> str | None:
    """Return the ``county_zh`` of the level-7/8 township containing the point.

    Uses the inline county MOI ships on every 鄉鎮市區 row — a single
    polygon hit yields the county, no separate level-4 query needed.
    """
    pt = Point(lon, lat)
    for (lvl, _name), polys in townships_idx.items():
        if lvl not in (7, 8):
            continue
        for mn_lat, mx_lat, mn_lon, mx_lon, geom, county in polys:
            if mn_lat <= lat <= mx_lat and mn_lon <= lon <= mx_lon:
                if geom.covers(pt):
                    return county
    return None


def load_boundary_exceptions(boundary_release: str | None) -> list[dict]:
    """Township-boundary exceptions for this MOI release (INFO, not failures).

    See config/boundary_exceptions.yaml. Returns [] when the file is absent
    or has no entry for ``boundary_release`` (e.g. the legacy OSM layout,
    whose metadata has no boundary_release).
    """
    if not boundary_release:
        return []
    path = CONFIG_DIR / "boundary_exceptions.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text("utf-8")) or {}
    return data.get(boundary_release, []) or []


def _sample_boundary_exception(s: Sample, rules: list[dict]) -> dict | None:
    """Return the first matching exception rule, else None.

    Match semantics for each key under a rule's `match`:
      * ``street_prefix`` — list; sample.street must START WITH one of them
        (so 「南堤路」 also matches 「南堤路二段」).
      * any field whose value is a list — membership test.
      * otherwise — equal-string.
    A field name of ``street_prefix`` is checked against ``s.street``.
    Omitted fields aren't compared; all listed fields must match.
    """
    for rule in rules:
        m = rule.get("match", {})
        ok = True
        for key, want in m.items():
            if key == "street_prefix":
                prefixes = want if isinstance(want, (list, tuple)) else [want]
                if not any(s.street.startswith(p) for p in prefixes):
                    ok = False
                    break
                continue
            got = getattr(s, key, None)
            if isinstance(want, (list, tuple)):
                if got not in want:
                    ok = False
                    break
            elif got != want:
                ok = False
                break
        if ok:
            return rule
    return None


def check_sample(
    conn: sqlite3.Connection,
    s: Sample,
    code_table: dict,
    townships_idx: dict | None = None,
    has_county: bool = False,
    boundary_rules: list[dict] | None = None,
) -> dict[str, tuple[bool, str]]:
    """Run the iteration-1 checks against one sample. Returns {check: (ok, detail)}."""
    results: dict[str, tuple[bool, str]] = {}

    cur = conn.execute(LOOKUP_SQL, (
        s.district_code, s.village, s.neighbor, s.street, s.area,
        s.lane, s.alley, s.number,
    ))
    matches = cur.fetchall()

    if not matches:
        miss = (f"no row for {s.district_code}/{s.village}/{s.neighbor}/"
                f"{s.street}/{s.area}/{s.lane}/{s.alley}/{s.number}")
        results["coverage"] = (False, miss)
        results["coord_match"] = (False, "no match")
        results["display_name"] = (False, "no match")
        results["fts5_search"] = (False, "no match")
        # Iteration-2 checks also have to be populated so callers can
        # iterate `active_checks` without KeyError.
        if townships_idx is not None:
            results["polygon_in"] = (False, "no match")
            results["reverse_township"] = (False, "no match")
            if has_county:
                results["county_match"] = (False, "no match")
        return results

    results["coverage"] = (True, "")
    # When multiple rows share the full key, pick the closest to expected coord.
    best = min(matches, key=lambda m: _haversine_m(s.lat, s.lon, m[1], m[2]))
    db_id, db_lat, db_lon, db_disp, db_disp_hw, db_township, db_county = best

    # 2. coord match
    err = _haversine_m(s.lat, s.lon, db_lat, db_lon)
    ok2 = err <= COORD_TOL_M
    results["coord_match"] = (ok2, f"err={err:.2f}m" if not ok2 else "")

    # 4. display_name compose round-trip
    mapping = code_table.get(s.district_code, {})
    parts = na.AddressParts(
        county=mapping.get("county", ""),
        district=mapping.get("district", ""),
        village=s.village, street=s.street, area=s.area,
        lane=s.lane, alley=s.alley, number=s.number,
    )
    want_fw = na.compose_display_name(parts, halfwidth=False)
    want_hw = na.compose_display_name(parts, halfwidth=True)
    ok4 = (db_disp == want_fw) and (db_disp_hw == want_hw)
    detail4 = ""
    if not ok4:
        detail4 = f"db={db_disp!r}/{db_disp_hw!r}; want={want_fw!r}/{want_hw!r}"
    results["display_name"] = (ok4, detail4)

    # 5. FTS5 search by halfwidth display name (use phrase-style quoted query).
    # FTS5 unicode61 tokenizer treats CJK chars as individual tokens.
    safe = want_hw.replace('"', '""')
    cur = conn.execute(
        "SELECT count(*) FROM places_fts WHERE places_fts MATCH ?",
        (f'"{safe}"',),
    )
    hits = cur.fetchone()[0]
    ok5 = hits >= 1
    results["fts5_search"] = (ok5, f"hits={hits}" if not ok5 else "")

    # 3 + 6: polygon-in + reverse-township (iteration 2 — needs townships.sqlite)
    if townships_idx is not None:
        # Known boundary exception (e.g. 台中港 reclaimed land outside the
        # legal polygon): downgrade the township checks to INFO (ok=None) so
        # they neither pass-mask nor fail the build. See boundary_exceptions.yaml.
        exc = _sample_boundary_exception(s, boundary_rules or [])
        # An exception only absorbs *failures* (a point that is genuinely
        # inside the polygon still counts as a pass): ok=None (INFO) iff the
        # check would otherwise fail AND the sample matches an exception rule.
        info_detail = (f"INFO boundary exception: {exc['reason'].split('.')[0]}"
                       if exc else "")

        expected_township = mapping.get("district", "")
        ok3 = _point_in_township(townships_idx, 8, expected_township, s.lat, s.lon)
        # 直轄市的區 are admin_level=7; 縣轄鄉鎮市 are admin_level=8. Try both.
        if not ok3:
            ok3 = _point_in_township(townships_idx, 7, expected_township, s.lat, s.lon)
        results["polygon_in"] = (
            (None if (exc and not ok3) else ok3),
            info_detail if (exc and not ok3) else
            (f"({s.lat:.5f},{s.lon:.5f}) not in {expected_township}" if not ok3 else ""),
        )

        # Reverse: which township polygon contains this point?
        rev = (_reverse_township_lookup(townships_idx, s.lat, s.lon, 8)
               or _reverse_township_lookup(townships_idx, s.lat, s.lon, 7))
        ok6 = rev == expected_township
        results["reverse_township"] = (
            (None if (exc and not ok6) else ok6),
            info_detail if (exc and not ok6) else
            (f"got {rev!r} want {expected_township!r}" if not ok6 else ""),
        )

        # 7: county_match — the MOI inline county_zh of the containing
        # township equals the expected 縣市 (only when the schema carries it).
        if has_county:
            expected_county = mapping.get("county", "")
            got_county = _reverse_county_lookup(townships_idx, s.lat, s.lon)
            ok7 = got_county == expected_county
            results["county_match"] = (
                (None if (exc and not ok7) else ok7),
                info_detail if (exc and not ok7) else
                (f"got {got_county!r} want {expected_county!r}" if not ok7 else ""),
            )

    return results


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

CHECKS_ITER1 = ("coverage", "coord_match", "display_name", "fts5_search")
CHECKS_ITER2 = CHECKS_ITER1 + ("polygon_in", "reverse_township")


def verify_county(county_id: str, n: int = 50, *, townships_db: Path | None = None) -> int:
    """Run all checks for one county. Returns count of failed samples."""
    config = yaml.safe_load((CONFIG_DIR / "csv_sources.yaml").read_text("utf-8"))
    code_table = yaml.safe_load((CONFIG_DIR / "moi_district_codes.yaml").read_text("utf-8"))
    src = config[county_id]
    db_path = OUTPUT_DIR / src["output_sqlite"]

    print(f"\n=== Verify {county_id} ({src['county_name']}) ===")
    print(f"  source CSV : {src['path']}")
    print(f"  sqlite     : {db_path}")
    if not db_path.exists():
        print(f"  SKIP: {db_path} not found yet (run ./run.sh county {county_id} first)")
        return -1

    townships_idx = None
    has_county = False
    boundary_rules: list[dict] = []
    active_checks = CHECKS_ITER1
    if townships_db and townships_db.exists():
        print(f"  townships  : {townships_db}")
        townships_idx, has_county = _load_township_index(townships_db)
        active_checks = CHECKS_ITER2 + (("county_match",) if has_county else ())
        tconn = sqlite3.connect(str(townships_db))
        row = tconn.execute(
            "SELECT value FROM metadata WHERE key='boundary_release'"
        ).fetchone()
        tconn.close()
        boundary_rules = load_boundary_exceptions(row[0] if row else None)
        if boundary_rules:
            print(f"  boundary exceptions: {len(boundary_rules)} rule(s) for "
                  f"release {row[0]} → matching township checks reported as INFO")

    sampled = load_county_samples(county_id, n=n)
    counts: dict[str, dict[str, CheckResult]] = defaultdict(lambda: defaultdict(CheckResult))

    conn = sqlite3.connect(str(db_path))
    try:
        for direction in DIRECTIONS:
            for s in sampled[direction]:
                res = check_sample(conn, s, code_table, townships_idx, has_county,
                                   boundary_rules)
                for ck, (ok, detail) in res.items():
                    counts[ck][direction].add(ok, detail=detail)
    finally:
        conn.close()

    # Pretty-print table. Cells show passed/(passed+failed); a trailing
    # "+Ni" flags INFO downgrades (known boundary exceptions, not failures).
    headers = [f"{county_id[:3].title()}-{d[0].upper()}" for d in DIRECTIONS]
    print(f"\n  {'check':<18s}" + "".join(f"{h:>12s}" for h in headers))
    total_failed = 0
    total_info = 0
    for ck in active_checks:
        cells = []
        for d in DIRECTIONS:
            r = counts[ck][d]
            cell = f"{r.passed}/{r.passed + r.failed}"
            if r.info:
                cell += f"+{r.info}i"
            cells.append(cell)
            total_failed += r.failed
            total_info += r.info
        print(f"  {ck:<18s}" + "".join(f"{c:>12s}" for c in cells))

    # Dump failure details
    verif_dir = OUTPUT_DIR / "verification"
    verif_dir.mkdir(exist_ok=True)
    failure_csv = verif_dir / f"{county_id}-failures.csv"
    with open(failure_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["check", "direction", "district_code", "village", "neighbor",
                    "street", "area", "lane", "alley", "number", "lat", "lon", "detail"])
        for ck in active_checks:
            for d in DIRECTIONS:
                samples_in_dir = sampled[d]
                conn = sqlite3.connect(str(db_path))
                for s in samples_in_dir:
                    sr = check_sample(conn, s, code_table, townships_idx, has_county,
                                      boundary_rules)
                    ok, detail = sr[ck]
                    if ok is False:   # genuine failure only — not INFO (None)
                        w.writerow([ck, d, s.district_code, s.village, s.neighbor,
                                    s.street, s.area, s.lane, s.alley, s.number,
                                    s.lat, s.lon, detail])
                conn.close()

    if total_info:
        print(f"  ℹ  {total_info} township check(s) downgraded to INFO "
              f"(known boundary exceptions; see config/boundary_exceptions.yaml)")
    if total_failed > 0:
        print(f"  → failure detail: {failure_csv}")
    else:
        print(f"  ✅ all non-INFO checks passed")
    return total_failed


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--county", help="If omitted, verify all counties in csv_sources.yaml")
    p.add_argument("-n", type=int, default=50, help="Samples per direction (default 50)")
    p.add_argument("--no-townships", action="store_true",
                   help="Skip iteration-2 polygon checks even if townships.sqlite exists")
    args = p.parse_args()

    townships_db = None
    if not args.no_townships:
        candidate = OUTPUT_DIR / "townships.sqlite"
        if candidate.exists():
            townships_db = candidate

    if args.county:
        rc = verify_county(args.county, n=args.n, townships_db=townships_db)
    else:
        config = yaml.safe_load((CONFIG_DIR / "csv_sources.yaml").read_text("utf-8"))
        total = 0
        for county_id in config:
            r = verify_county(county_id, n=args.n, townships_db=townships_db)
            if r > 0:
                total += r
        rc = total
    return 0 if rc <= 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
