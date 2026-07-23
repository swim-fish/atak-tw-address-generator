"""Consolidate TGOS floor rows into coordinate-aware base addresses.

Rows are grouped by exact coordinate and full base address, not by coordinate
alone. A floor suffix after the first ``號`` is removed to derive the base
number. An existing non-floor row wins; otherwise the lowest-id floor row is
rewritten into a synthetic base-address row. Different base addresses at the
same coordinate are preserved.

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

import base_address as ba

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
LOG_DIR = OUTPUT_DIR / "logs"


# ---------------------------------------------------------------------------
# Removal SQL
# ---------------------------------------------------------------------------
#
# Address identity intentionally excludes ``neighbor`` because it is not part
# of the human-facing address rendered by normalize_address.py. ``district_code``
# supplies county/township identity; street falls back to area exactly as the
# display-name composer does.
_RANK_CTE = """
WITH classified AS (
    SELECT
        p.*,
        CASE
            WHEN INSTR(number, '號') > 0
             AND (INSTR(number, '樓') > INSTR(number, '號')
               OR INSTR(number, '層') > INSTR(number, '號'))
            THEN 1 ELSE 0
        END AS is_floor,
        CASE
            WHEN INSTR(number, '號') > 0
             AND (INSTR(number, '樓') > INSTR(number, '號')
               OR INSTR(number, '層') > INSTR(number, '號'))
            THEN SUBSTR(number, 1, INSTR(number, '號'))
            ELSE number
        END AS base_number,
        CASE
            WHEN INSTR(number, '號') > 0
             AND (INSTR(number, '樓') > INSTR(number, '號')
               OR INSTR(number, '層') > INSTR(number, '號'))
            THEN 1 ELSE 0
        END AS parseable_floor
    FROM places p
),
grp AS (
    SELECT
        classified.*,
        SUM(CASE WHEN is_floor = 0 THEN 1 ELSE 0 END) OVER (
            PARTITION BY lat, lon, district_code,
                         COALESCE(village, ''),
                         COALESCE(street, area, ''),
                         COALESCE(lane, ''), COALESCE(alley, ''),
                         base_number
        ) AS ground_count,
        MIN(CASE WHEN is_floor = 0 THEN id END) OVER (
            PARTITION BY lat, lon, district_code,
                         COALESCE(village, ''),
                         COALESCE(street, area, ''),
                         COALESCE(lane, ''), COALESCE(alley, ''),
                         base_number
        ) AS lowest_ground_id,
        MIN(CASE WHEN parseable_floor = 1 THEN id END) OVER (
            PARTITION BY lat, lon, district_code,
                         COALESCE(village, ''),
                         COALESCE(street, area, ''),
                         COALESCE(lane, ''), COALESCE(alley, ''),
                         base_number
        ) AS lowest_floor_id
    FROM classified
),
ranked AS (
    SELECT
        grp.*,
        COALESCE(lowest_ground_id, lowest_floor_id) AS kept_id,
           CASE
               WHEN parseable_floor = 1 AND ground_count > 0 THEN 1
               WHEN parseable_floor = 1 AND ground_count = 0
                    AND id <> lowest_floor_id THEN 1
               ELSE 0
           END AS to_drop,
           CASE
               WHEN parseable_floor = 1 AND ground_count = 0
                    AND id = lowest_floor_id THEN 1
               ELSE 0
           END AS to_synthesize
    FROM grp
)
"""


def _export_removal_csv(conn: sqlite3.Connection, csv_path: Path) -> int:
    """Write the to-be-removed id list to CSV. Returns the row count."""
    cur = conn.cursor()
    cur.execute(
        _RANK_CTE +
        "SELECT r.id, r.lat, r.lon, r.number, r.base_number, r.display_name, "
        "       r.kept_id, kp.display_name AS kept_display_name, "
        "       CASE WHEN r.ground_count > 0 "
        "            THEN 'matched-existing-base' ELSE 'merged-floor-variants' END "
        "FROM ranked r "
        "LEFT JOIN places kp ON kp.id = r.kept_id "
        "WHERE r.to_drop = 1 "
        "ORDER BY r.lat, r.lon, r.base_number, r.id"
    )
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["removed_id", "lat", "lon", "number", "base_number",
                    "display_name", "kept_id", "kept_display_name", "reason"])
        for row in cur:
            w.writerow(row)
            n += 1
    return n


def _export_synthesis_csv(conn: sqlite3.Connection, csv_path: Path) -> int:
    """Write the rows that will be rewritten into base addresses."""
    cur = conn.execute(
        _RANK_CTE +
        "SELECT id, lat, lon, number, base_number, county, township, village, "
        "       street, area, lane, alley, display_name "
        "FROM ranked r "
        "WHERE to_synthesize = 1 "
        "ORDER BY lat, lon, base_number, id"
    )
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["kept_id", "lat", "lon", "original_number", "base_number",
                    "original_display_name", "new_display_name"])
        for row in cur:
            (row_id, lat, lon, original_number, base_number, county, township,
             village, street, area, lane, alley, original_display) = row
            fields = ba.synthesize_fields(
                county=county, township=township, village=village,
                street=street, area=area, lane=lane, alley=alley,
                number=base_number,
            )
            w.writerow([row_id, lat, lon, original_number, base_number,
                        original_display, fields.display_name])
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
    cur.execute(
        _RANK_CTE + "SELECT COUNT(*) FROM ranked WHERE to_synthesize = 1"
    )
    to_synthesize = cur.fetchone()[0]
    cur.execute(
        _RANK_CTE +
        "SELECT COUNT(*) FROM ranked "
        "WHERE (number LIKE '%樓%' OR number LIKE '%層%') AND is_floor = 0"
    )
    non_suffix_floor_markers = cur.fetchone()[0]
    return {"total": total, "to_remove": to_remove,
            "to_synthesize": to_synthesize,
            "non_suffix_floor_markers": non_suffix_floor_markers,
            "remaining": total - to_remove}


# ---------------------------------------------------------------------------
# Apply path — mutate a copy in /tmp, then move back.
# ---------------------------------------------------------------------------

def _apply(
    db_path: Path,
    removed_count: int,
    synthesized_count: int,
    non_suffix_floor_marker_count: int,
) -> Path:
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
        conn.execute(
            "CREATE TEMP TABLE to_synthesize "
            "(id INTEGER PRIMARY KEY, base_number TEXT NOT NULL)"
        )
        conn.execute(
            _RANK_CTE +
            "INSERT INTO temp.to_synthesize (id, base_number) "
            "SELECT id, base_number FROM ranked WHERE to_synthesize = 1"
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

        print("  synthesizing base-address rows...", flush=True)
        synth_rows = conn.execute(
            "SELECT p.id, s.base_number, p.county, p.township, p.village, "
            "       p.street, p.area, p.lane, p.alley "
            "FROM places p JOIN temp.to_synthesize s ON s.id = p.id "
            "ORDER BY p.id"
        ).fetchall()
        updates = []
        for (row_id, base_number, county, township, village, street,
             area, lane, alley) in synth_rows:
            fields = ba.synthesize_fields(
                county=county, township=township, village=village,
                street=street, area=area, lane=lane, alley=alley,
                number=base_number,
            )
            updates.append((
                fields.number, fields.name, fields.display_name,
                fields.display_name_halfwidth, row_id,
            ))
        conn.executemany(
            "UPDATE places "
            "SET number = ?, name = ?, display_name = ?, "
            "    display_name_halfwidth = ? "
            "WHERE id = ?",
            updates,
        )
        if len(updates) != synthesized_count:
            raise RuntimeError(
                f"synthesized {len(updates)} rows; expected {synthesized_count}"
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
            "deduped_synthesized": str(synthesized_count),
            "deduped_non_suffix_floor_markers": str(
                non_suffix_floor_marker_count
            ),
            "deduped_strategy": (
                "coordinate-and-base-address;"
                "prefer-explicit-base;synthesize-lowest-floor-id"
            ),
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
        print(f"  base rows to synthesize      : {stats['to_synthesize']:>10,}", flush=True)
        print(f"  non-suffix 樓/層 markers     : "
              f"{stats['non_suffix_floor_markers']:>10,}", flush=True)
        print(f"  remaining after dedup        : {stats['remaining']:>10,}", flush=True)

        stem = db_path.stem
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        csv_path = LOG_DIR / f"dedup-{stem}-removed-{ts}.csv"
        print(f"  exporting removal list → {csv_path.name}", flush=True)
        n = _export_removal_csv(conn, csv_path)
        assert n == stats["to_remove"], (n, stats["to_remove"])
        print(f"  wrote {n:,} rows to {csv_path}", flush=True)

        synthesis_path = LOG_DIR / f"synthesize-{stem}-base-{ts}.csv"
        print(f"  exporting synthesis list → {synthesis_path.name}", flush=True)
        n_synth = _export_synthesis_csv(conn, synthesis_path)
        assert n_synth == stats["to_synthesize"], (
            n_synth, stats["to_synthesize"]
        )
        print(f"  wrote {n_synth:,} rows to {synthesis_path}", flush=True)
    finally:
        conn.close()

    if not apply:
        print("  DRY-RUN — no changes written. Pass --apply to delete.", flush=True)
        return

    _apply(
        db_path,
        stats["to_remove"],
        stats["to_synthesize"],
        stats["non_suffix_floor_markers"],
    )
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
