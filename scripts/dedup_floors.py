"""Remove floor-level duplicates from places-*.sqlite (conservative).

Strategy (conservative — keep all distinct house-numbers at a coord):
  - Within each (lat, lon) group:
      * If any row's ``number`` is ground-floor (does NOT contain 樓/層),
        KEEP all such ground-floor rows, REMOVE only the 樓/層 rows.
      * If every row in the group is a 樓/層 row, KEEP the lowest id
        and remove the rest.
  - Singleton coords (group of 1) are never touched.

This is conservative on purpose: TGOS sometimes pins multiple distinct
house-numbers to the same building-centre coordinate (e.g. 大誠街 5-3-2
號 / 5-5-5 號 / 5-8-8 號 all share one point). Those are distinct
addresses, not floors — collapsing them to one row would lose searchable
geocoding.

Outputs:
  - Always writes the to-be-removed id list to a CSV in ``output/logs/``.
  - Default mode is DRY-RUN (no DB writes). Pass ``--apply`` to actually
    delete rows, prune FTS5 + R*Tree, refresh metadata, and VACUUM.

After ``--apply`` succeeds, re-run ``scripts/build_manifest.py`` to
repackage the ZIPs and manifests.

Usage:
    python3 scripts/dedup_floors.py                       # dry-run all
    python3 scripts/dedup_floors.py --db output/places-taichung.sqlite
    python3 scripts/dedup_floors.py --apply               # actually delete
"""
from __future__ import annotations

import argparse
import csv
import datetime
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
LOG_DIR = OUTPUT_DIR / "logs"


# ---------------------------------------------------------------------------
# Removal SQL
# ---------------------------------------------------------------------------
#
# A row is marked for removal when:
#   - it shares its (lat, lon) with at least one other row
#     (i.e. group_size > 1), AND
#   - it is a 樓/層 row, AND
#   - the group still contains at least one ground-floor row even after
#     this row is removed.
#
# Equivalently: per (lat, lon) group with both ground-floor AND
# 樓/層 rows, drop every 樓/層 row. Groups consisting entirely of
# 樓/層 rows fall back to "keep lowest id", removing the rest.
#
# ``rank_by_id`` lets us pick "keep one" within the all-floor case.
_RANK_CTE = """
WITH grp AS (
    SELECT
        id, lat, lon, number, display_name,
        CASE WHEN number LIKE '%樓%' OR number LIKE '%層%' THEN 1 ELSE 0 END AS is_floor,
        COUNT(*)        OVER (PARTITION BY lat, lon) AS group_size,
        SUM(CASE WHEN number NOT LIKE '%樓%' AND number NOT LIKE '%層%'
                 THEN 1 ELSE 0 END)
                         OVER (PARTITION BY lat, lon) AS ground_count,
        ROW_NUMBER()     OVER (PARTITION BY lat, lon ORDER BY id) AS rn_by_id
    FROM places
),
ranked AS (
    SELECT id, lat, lon, number, display_name,
           CASE
               -- Singleton group: keep.
               WHEN group_size = 1                       THEN 0
               -- Has any ground-floor row: drop every floor row.
               WHEN ground_count >= 1 AND is_floor = 1   THEN 1
               -- All-floor group: keep the lowest id only.
               WHEN ground_count  = 0 AND rn_by_id > 1   THEN 1
               -- Ground-floor row in any group: keep.
               ELSE 0
           END AS to_drop
    FROM grp
),
-- For the CSV report: pick one representative kept row per (lat, lon)
-- to print alongside each removed row (lowest kept id).
kept_rep AS (
    SELECT lat, lon, MIN(id) AS kept_id
    FROM ranked WHERE to_drop = 0
    GROUP BY lat, lon
)
"""


def _export_removal_csv(conn: sqlite3.Connection, csv_path: Path) -> int:
    """Write the to-be-removed id list to CSV. Returns the row count."""
    cur = conn.cursor()
    # For each removed row, also note the kept id for the same coord, so
    # the operator can inspect "row 123 (六樓之5) collapsed into kept id 12 (一樓)".
    cur.execute(
        _RANK_CTE +
        "SELECT r.id, r.lat, r.lon, r.number, r.display_name, "
        "       k.kept_id, kp.display_name AS kept_display_name "
        "FROM ranked r "
        "LEFT JOIN kept_rep k ON k.lat = r.lat AND k.lon = r.lon "
        "LEFT JOIN places   kp ON kp.id = k.kept_id "
        "WHERE r.to_drop = 1 "
        "ORDER BY r.lat, r.lon, r.id"
    )
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["removed_id", "lat", "lon", "number", "display_name",
                    "kept_id", "kept_display_name"])
        for row in cur:
            w.writerow(row)
            n += 1
    return n


def _summary(conn: sqlite3.Connection) -> dict:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM places")
    total = cur.fetchone()[0]
    cur.execute(
        _RANK_CTE + "SELECT COUNT(*) FROM ranked WHERE to_drop = 1"
    )
    to_remove = cur.fetchone()[0]
    return {"total": total, "to_remove": to_remove,
            "remaining": total - to_remove}


# ---------------------------------------------------------------------------
# Apply path — mutate a copy in /tmp, then move back.
# ---------------------------------------------------------------------------

def _apply(db_path: Path, removed_count: int) -> Path:
    """Run the actual mutation on a tmp copy; return the new file path."""
    # Match ingest_tgos_csv.py: build in tmp dir, then shutil.move back.
    # On Windows the Docker bind-mount issue doesn't apply (we run native
    # Python here), but staging still protects the original until success.
    tmp_dir = Path(tempfile.gettempdir())
    tmp_path = tmp_dir / (db_path.stem + ".dedup.sqlite")
    if tmp_path.exists():
        tmp_path.unlink()
    print(f"  copying → {tmp_path}", flush=True)
    shutil.copy2(db_path, tmp_path)

    conn = sqlite3.connect(tmp_path)
    try:
        conn.executescript(
            "PRAGMA journal_mode = WAL;"
            "PRAGMA synchronous = NORMAL;"
            "PRAGMA temp_store = MEMORY;"
            "PRAGMA cache_size = -200000;"
        )

        print("  deleting redundant rows...", flush=True)
        # Stage ids in a temp table first — referencing the CTE multiple
        # times below (FTS prune, RTree prune, places DELETE) would
        # otherwise re-evaluate the window function each time.
        conn.execute("CREATE TEMP TABLE to_drop (id INTEGER PRIMARY KEY)")
        conn.execute(
            _RANK_CTE +
            "INSERT INTO temp.to_drop (id) SELECT id FROM ranked WHERE to_drop = 1"
        )

        # Clear FTS + RTree entries for dropped rows BEFORE the places
        # DELETE, otherwise the FTS5 external-content table's rowid
        # references go stale.
        print("  pruning FTS5 + R*Tree entries...", flush=True)
        conn.execute(
            "DELETE FROM places_fts WHERE rowid IN (SELECT id FROM temp.to_drop)"
        )
        conn.execute(
            "DELETE FROM places_rtree WHERE id IN (SELECT id FROM temp.to_drop)"
        )

        conn.execute(
            "DELETE FROM places WHERE id IN (SELECT id FROM temp.to_drop)"
        )
        conn.commit()

        # Rebuild FTS index to keep it consistent + compact. We just
        # deleted entries, but an explicit rebuild guarantees the
        # external-content invariants are intact.
        print("  rebuilding FTS5 index...", flush=True)
        conn.execute("INSERT INTO places_fts(places_fts) VALUES('rebuild')")
        conn.commit()

        # Update metadata.
        now = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        cur = conn.execute("SELECT COUNT(*) FROM places")
        new_total = cur.fetchone()[0]
        meta_updates = {
            "deduped_at": now,
            "deduped_removed": str(removed_count),
            "deduped_strategy": "drop-floor-rows-when-ground-floor-exists;keep-lowest-id-otherwise",
            # Refresh the inserted counter so build_manifest's manifest is
            # accurate; preserve the original under deduped_inserted_orig.
        }
        # Preserve the pre-dedup inserted count for traceability.
        row = conn.execute(
            "SELECT value FROM metadata WHERE key = 'inserted'"
        ).fetchone()
        if row:
            meta_updates["deduped_inserted_orig"] = row[0]
        meta_updates["inserted"] = str(new_total)
        for k, v in meta_updates.items():
            conn.execute(
                "INSERT INTO metadata(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, v),
            )
        conn.commit()

        print("  VACUUM + ANALYZE...", flush=True)
        conn.execute("VACUUM")
        conn.execute("ANALYZE")
        conn.commit()
    finally:
        conn.close()

    # Move tmp back over the original.
    print(f"  moving {tmp_path} → {db_path}", flush=True)
    db_path.unlink()
    shutil.move(str(tmp_path), str(db_path))
    return db_path


# ---------------------------------------------------------------------------
# Per-DB orchestrator
# ---------------------------------------------------------------------------

def process(db_path: Path, apply: bool) -> None:
    print(f"\n=== {db_path} ===", flush=True)
    if not db_path.exists():
        print(f"  MISSING — skipping", flush=True)
        return

    conn = sqlite3.connect(db_path)
    try:
        stats = _summary(conn)
        print(f"  total rows                   : {stats['total']:>10,}", flush=True)
        print(f"  rows to remove (dup floors)  : {stats['to_remove']:>10,}  "
              f"({stats['to_remove'] / stats['total'] * 100:.1f}%)", flush=True)
        print(f"  remaining after dedup        : {stats['remaining']:>10,}", flush=True)

        stem = db_path.stem
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        csv_path = LOG_DIR / f"dedup-{stem}-removed-{ts}.csv"
        print(f"  exporting removal list → {csv_path.name}", flush=True)
        n = _export_removal_csv(conn, csv_path)
        assert n == stats["to_remove"], (n, stats["to_remove"])
        print(f"  wrote {n:,} rows to {csv_path}", flush=True)
    finally:
        conn.close()

    if not apply:
        print("  DRY-RUN — no changes written. Pass --apply to delete.", flush=True)
        return

    _apply(db_path, stats["to_remove"])
    print("  apply done. Re-run scripts/build_manifest.py to repackage.",
          flush=True)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--db", type=Path, default=None,
        help="Path to a places-*.sqlite. If omitted, process all in output/.",
    )
    p.add_argument(
        "--apply", action="store_true",
        help="Actually delete rows. Default is dry-run (CSV only).",
    )
    args = p.parse_args()

    if args.db:
        targets = [args.db]
    else:
        targets = sorted(OUTPUT_DIR.glob("places-*.sqlite"))
        targets = [t for t in targets if not t.name.endswith("-osm.sqlite")]

    if not targets:
        print("No places-*.sqlite found.", file=sys.stderr)
        return 1

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    for t in targets:
        process(t, apply=args.apply)

    print("\nDone.", flush=True)
    if not args.apply:
        print("(dry-run; pass --apply to write changes)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
