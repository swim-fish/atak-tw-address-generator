"""Inspect duplicate (lat, lon) coordinates in a places-*.sqlite.

The 2D map can't distinguish floors, so rows that share an identical
coordinate (typically "N樓之M") are redundant for routing/marker purposes.

This script is *read-only* — it prints a report. Removal happens in a
separate step once the strategy is approved.

Usage:
    python3 scripts/inspect_duplicate_coords.py [--db PATH] [--top N] [--csv OUT.csv]

If --db is omitted, all output/places-*.sqlite files are scanned.
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from collections import Counter
from pathlib import Path

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


# ---------------------------------------------------------------------------
# Per-DB analysis
# ---------------------------------------------------------------------------

def _bucket(n: int) -> str:
    if n == 2:
        return "2"
    if n <= 5:
        return "3-5"
    if n <= 10:
        return "6-10"
    if n <= 50:
        return "11-50"
    if n <= 200:
        return "51-200"
    return "200+"


_BUCKET_ORDER = ["2", "3-5", "6-10", "11-50", "51-200", "200+"]


def inspect(db_path: Path, top: int, csv_out: Path | None) -> None:
    print(f"\n=== {db_path} ===", flush=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM places")
    total = cur.fetchone()[0]
    print(f"  total rows                     : {total:>10,}", flush=True)

    # Materialise a temp table of duplicate groups so subsequent
    # queries are cheap. There's no index on (lat, lon) in the shipped
    # schema, so a single sequential scan is the best we can do — we
    # do it once here and reuse.
    print("  scanning duplicate groups...", flush=True)
    cur.execute(
        "CREATE TEMP TABLE dup_groups AS "
        "SELECT lat, lon, COUNT(*) AS c, "
        "       SUM(CASE WHEN number LIKE '%樓%' THEN 1 ELSE 0 END) AS floor_c "
        "FROM places GROUP BY lat, lon HAVING c > 1"
    )
    cur.execute("CREATE INDEX dup_groups_latlon ON dup_groups(lat, lon)")

    cur.execute("SELECT COUNT(*), COALESCE(SUM(c), 0), COALESCE(SUM(floor_c), 0) "
                "FROM dup_groups")
    dup_groups, rows_in_dups, floor_rows = cur.fetchone()
    redundant = rows_in_dups - dup_groups  # keep 1 per group, drop the rest

    print(f"  duplicate coord groups (>=2)   : {dup_groups:>10,}", flush=True)
    print(f"  rows in duplicate groups       : {rows_in_dups:>10,}", flush=True)
    print(f"  redundant rows (keep 1/group)  : {redundant:>10,}  "
          f"({redundant / total * 100:.1f}% of total)", flush=True)

    # Distribution by group size.
    cur.execute("SELECT c FROM dup_groups")
    dist = Counter(_bucket(row[0]) for row in cur)
    print("\n  duplicate group-size distribution:", flush=True)
    for b in _BUCKET_ORDER:
        if dist[b]:
            print(f"    {b:>7} dup/coord : {dist[b]:>7,} groups", flush=True)

    print(f"\n  rows in dup groups containing '樓' in number: {floor_rows:,} "
          f"({floor_rows / rows_in_dups * 100:.1f}% of dup rows)", flush=True)

    # Top-N largest groups, with samples.
    print(f"\n  top {top} largest duplicate groups:", flush=True)
    cur.execute("SELECT lat, lon, c FROM dup_groups ORDER BY c DESC LIMIT ?", (top,))
    top_rows = cur.fetchall()
    for i, g in enumerate(top_rows, 1):
        lat, lon, c = g["lat"], g["lon"], g["c"]
        cur.execute(
            "SELECT display_name FROM places "
            "WHERE lat = ? AND lon = ? ORDER BY id LIMIT 3",
            (lat, lon),
        )
        samples = cur.fetchall()
        print(f"  [{i:>2}] ({lat:.7f}, {lon:.7f})  ×{c}", flush=True)
        for s in samples:
            print(f"        - {s['display_name']}", flush=True)
        if c > 3:
            print(f"        ... +{c - 3} more", flush=True)

    # Optional CSV export of every duplicate group (one row per group).
    # Use a single JOIN against the (already indexed) dup_groups temp table
    # instead of N point queries.
    if csv_out:
        csv_out.parent.mkdir(parents=True, exist_ok=True)
        print(f"\n  writing per-group CSV → {csv_out} ...", flush=True)
        # Pick a deterministic sample per group: lowest id.
        cur.execute(
            "SELECT d.lat, d.lon, d.c, ("
            "  SELECT display_name FROM places p "
            "  WHERE p.lat = d.lat AND p.lon = d.lon ORDER BY id LIMIT 1"
            ") AS sample FROM dup_groups d ORDER BY d.c DESC"
        )
        with open(csv_out, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["lat", "lon", "count", "sample_display_name"])
            for row in cur:
                w.writerow([row["lat"], row["lon"], row["c"], row["sample"]])
        print("  CSV done.", flush=True)

    conn.close()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--db", type=Path, default=None,
        help="Path to a places-*.sqlite. If omitted, scan all in output/.",
    )
    p.add_argument(
        "--top", type=int, default=10,
        help="Show this many largest duplicate groups per DB (default: 10).",
    )
    p.add_argument(
        "--csv", type=Path, default=None,
        help="Optional path to write a per-group CSV report.",
    )
    args = p.parse_args()

    if args.db:
        targets = [args.db]
    else:
        targets = sorted(OUTPUT_DIR.glob("places-*.sqlite"))
        # Skip the bundled places-osm.sqlite (different schema/use).
        targets = [t for t in targets if not t.name.endswith("-osm.sqlite")]

    if not targets:
        print("No places-*.sqlite found.", file=sys.stderr)
        return 1

    for t in targets:
        if not t.exists():
            print(f"WARN: missing {t}", file=sys.stderr)
            continue
        inspect(t, args.top, args.csv if len(targets) == 1 else None)

    if args.csv and len(targets) != 1:
        print("\nNOTE: --csv is only written when --db points to one file.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
