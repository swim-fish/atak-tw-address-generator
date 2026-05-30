"""Schema v2 -> v3 migration: add `area` to the places_fts index in place.

The shipped per-county sqlite files were built before `area` was an indexed
FTS column, so empty-street addresses (located by a named 巷/莊/新村 in 地區,
e.g. 十甲巷, 介壽新村) could not be found by their locality name. This script
upgrades an already-built `places-*.sqlite` without re-running the full Docker
pipeline:

  1. DROP places_fts (and its shadow tables)
  2. CREATE it again WITH `area` (matching ingest_tgos_csv.SCHEMA_SQL v3)
  3. rebuild the FTS index
  4. set metadata.schema_version = '3'
  5. VACUUM to keep the file tidy

It is idempotent: a file already at v3 with `area` in the index is skipped.
A fresh `./run.sh county <name>` produces the same result; this is only the
no-rebuild path for existing artifacts.

Usage:
    python3 scripts/migrate_fts_add_area.py                 # both counties in output/
    python3 scripts/migrate_fts_add_area.py PATH [PATH...]  # explicit files
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
TARGET_VERSION = "3"

# Must match ingest_tgos_csv.SCHEMA_SQL exactly (v3 column order).
FTS_DDL = """
CREATE VIRTUAL TABLE places_fts USING fts5(
    name, display_name, display_name_halfwidth, street, area, township,
    content='places',
    content_rowid='id',
    tokenize='unicode61'
);
"""


def fts_has_area(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='places_fts'"
    ).fetchone()
    return bool(row) and " area," in row[0]


def current_version(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT value FROM metadata WHERE key='schema_version'"
    ).fetchone()
    return row[0] if row else "?"


def self_check(conn: sqlite3.Connection) -> str:
    """Pick one empty-street row and confirm its area name is now matchable."""
    row = conn.execute(
        "SELECT area FROM places "
        "WHERE street IS NULL AND area IS NOT NULL LIMIT 1"
    ).fetchone()
    if not row:
        return "no empty-street row to probe (ok)"
    area = row[0].replace('"', '""')
    hits = conn.execute(
        "SELECT count(*) FROM places_fts WHERE places_fts MATCH ?",
        (f'"{area}"',),
    ).fetchone()[0]
    status = "PASS" if hits >= 1 else "FAIL"
    return f"area '{row[0]}' phrase-match hits={hits} [{status}]"


def migrate(path: Path) -> bool:
    if not path.exists():
        print(f"[skip] {path} not found")
        return False
    conn = sqlite3.connect(str(path))
    try:
        ver = current_version(conn)
        if fts_has_area(conn) and ver == TARGET_VERSION:
            print(f"[skip] {path.name} already at v{ver} with area in FTS")
            return False

        n = conn.execute("SELECT count(*) FROM places").fetchone()[0]
        print(f"[{path.name}] v{ver} -> v{TARGET_VERSION}, {n:,} rows")
        conn.execute("PRAGMA cache_size=-400000")  # ~400 MB for the rebuild

        t0 = time.time()
        conn.execute("DROP TABLE IF EXISTS places_fts")
        conn.executescript(FTS_DDL)
        conn.execute("INSERT INTO places_fts(places_fts) VALUES('rebuild')")
        conn.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
            (TARGET_VERSION,),
        )
        conn.commit()
        print(f"[{path.name}] FTS rebuilt in {time.time()-t0:.1f}s — {self_check(conn)}")

        t1 = time.time()
        conn.execute("VACUUM")
        conn.commit()
        size_mb = path.stat().st_size / (1 << 20)
        print(f"[{path.name}] VACUUM {time.time()-t1:.1f}s — file now {size_mb:.1f} MiB")
        return True
    finally:
        conn.close()


def main() -> int:
    args = sys.argv[1:]
    if args:
        targets = [Path(a) for a in args]
    else:
        targets = sorted(OUTPUT_DIR.glob("places-*.sqlite"))
    if not targets:
        print("No places-*.sqlite found.", file=sys.stderr)
        return 1
    changed = 0
    for p in targets:
        if migrate(p):
            changed += 1
    print(f"\nDone — {changed} file(s) migrated to schema v{TARGET_VERSION}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
