"""Collapse same-coordinate house-number groups to a single row.

Runs *after* ``dedup_floors.py``. The floor-level dedup removes 樓/層
rows that share a coord with a ground-floor row, but TGOS still pins
multiple **distinct** house numbers to the same building-centre point
(e.g. 中清路一段 822 之 100…159 號 — 160 ground-floor addresses all at
one coord). The 2D map cannot place separate markers there, so we
collapse each remaining same-coord group to one row.

Strategy (per ``(lat, lon)`` group):
  1. Prefer the row with the shortest ``number`` string — biases toward
     a building's main door (e.g. ``８２２號`` beats ``８２２之１００號``).
  2. Tiebreak by lowest ``id`` for determinism.

This is lossy by design: forward FTS5 searches like
``中清路一段 822 之 105 號`` will no longer match any row in the group.
Operators who need that recall must skip this stage (``--no-collapse``
in build-data.sh).

Outputs:
  - Removal list CSV at ``output/logs/collapse-<stem>-removed-<UTC>.csv``
  - Default mode is DRY-RUN; pass ``--apply`` to actually delete.

After ``--apply`` succeeds, re-run ``scripts/build_manifest.py`` to
repackage the ZIPs and manifests.

Usage:
    python3 scripts/collapse_coords.py                    # dry-run all
    python3 scripts/collapse_coords.py --db output/places-taichung.sqlite
    python3 scripts/collapse_coords.py --apply
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


# A row is kept iff it is the lexicographically first by
# (LENGTH(number), id) within its (lat, lon) group; all others are dropped.
_RANK_CTE = """
WITH ranked AS (
    SELECT
        id, lat, lon, number, display_name,
        ROW_NUMBER() OVER (
            PARTITION BY lat, lon
            ORDER BY LENGTH(number), id
        ) AS rn,
        COUNT(*)    OVER (PARTITION BY lat, lon) AS group_size
    FROM places
),
kept_rep AS (
    SELECT lat, lon, id AS kept_id, display_name AS kept_display_name
    FROM ranked WHERE rn = 1
)
"""


def _export_removal_csv(conn: sqlite3.Connection, csv_path: Path) -> int:
    cur = conn.cursor()
    cur.execute(
        _RANK_CTE +
        "SELECT r.id, r.lat, r.lon, r.number, r.display_name, "
        "       k.kept_id, k.kept_display_name "
        "FROM ranked r "
        "LEFT JOIN kept_rep k ON k.lat = r.lat AND k.lon = r.lon "
        "WHERE r.rn > 1 "
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
    cur.execute(_RANK_CTE + "SELECT COUNT(*) FROM ranked WHERE rn > 1")
    to_remove = cur.fetchone()[0]
    return {"total": total, "to_remove": to_remove,
            "remaining": total - to_remove}


def _apply(db_path: Path, removed_count: int) -> Path:
    tmp_dir = Path(tempfile.gettempdir())
    tmp_path = tmp_dir / (db_path.stem + ".collapse.sqlite")
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

        print("  staging removal id set...", flush=True)
        conn.execute("CREATE TEMP TABLE to_drop (id INTEGER PRIMARY KEY)")
        conn.execute(
            _RANK_CTE +
            "INSERT INTO temp.to_drop (id) SELECT id FROM ranked WHERE rn > 1"
        )

        print("  pruning FTS5 + R*Tree entries...", flush=True)
        conn.execute(
            "DELETE FROM places_fts WHERE rowid IN (SELECT id FROM temp.to_drop)"
        )
        conn.execute(
            "DELETE FROM places_rtree WHERE id IN (SELECT id FROM temp.to_drop)"
        )

        print("  deleting rows from places...", flush=True)
        conn.execute(
            "DELETE FROM places WHERE id IN (SELECT id FROM temp.to_drop)"
        )
        conn.commit()

        print("  rebuilding FTS5 index...", flush=True)
        conn.execute("INSERT INTO places_fts(places_fts) VALUES('rebuild')")
        conn.commit()

        now = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        cur = conn.execute("SELECT COUNT(*) FROM places")
        new_total = cur.fetchone()[0]
        meta_updates = {
            "collapsed_at": now,
            "collapsed_removed": str(removed_count),
            "collapsed_strategy": "one-row-per-coord;shortest-number;lowest-id",
        }
        # Preserve the pre-collapse count for traceability.
        row = conn.execute(
            "SELECT value FROM metadata WHERE key = 'inserted'"
        ).fetchone()
        if row:
            meta_updates["collapsed_inserted_pre"] = row[0]
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

    print(f"  moving {tmp_path} → {db_path}", flush=True)
    db_path.unlink()
    shutil.move(str(tmp_path), str(db_path))
    return db_path


def process(db_path: Path, apply: bool) -> None:
    print(f"\n=== {db_path} ===", flush=True)
    if not db_path.exists():
        print("  MISSING — skipping", flush=True)
        return

    conn = sqlite3.connect(db_path)
    try:
        stats = _summary(conn)
        print(f"  total rows                   : {stats['total']:>10,}", flush=True)
        print(f"  rows to remove (coord-collapse): {stats['to_remove']:>10,}  "
              f"({stats['to_remove'] / stats['total'] * 100:.1f}%)", flush=True)
        print(f"  remaining after collapse     : {stats['remaining']:>10,}", flush=True)

        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        csv_path = LOG_DIR / f"collapse-{db_path.stem}-removed-{ts}.csv"
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


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, default=None,
                   help="Path to a places-*.sqlite. If omitted, process all in output/.")
    p.add_argument("--apply", action="store_true",
                   help="Actually delete rows. Default is dry-run (CSV only).")
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
