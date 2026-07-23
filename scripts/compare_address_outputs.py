#!/usr/bin/env python3
"""Compare baseline and candidate address artifacts.

The report focuses on the two TGOS county databases and records file-level
hashes for every root-level SQLite/ZIP/manifest artifact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path


COUNTY_FILES = ("places-taichung.sqlite", "places-changhua.sqlite")

BASE_KEY = """
lat, lon, district_code,
COALESCE(village, ''),
COALESCE(street, area, ''),
COALESCE(lane, ''),
COALESCE(alley, ''),
number
"""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def scalar(conn: sqlite3.Connection, sql: str) -> int | str:
    return conn.execute(sql).fetchone()[0]


def inspect_db(path: Path) -> dict:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        metadata = dict(conn.execute("SELECT key,value FROM metadata"))
        return {
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
            "quick_check": scalar(conn, "PRAGMA quick_check"),
            "rows": scalar(conn, "SELECT COUNT(*) FROM places"),
            "unique_coordinates": scalar(
                conn,
                "SELECT COUNT(*) FROM (SELECT 1 FROM places GROUP BY lat,lon)",
            ),
            "coordinates_with_multiple_addresses": scalar(
                conn,
                "SELECT COUNT(*) FROM ("
                " SELECT 1 FROM places GROUP BY lat,lon HAVING COUNT(*) > 1"
                ")",
            ),
            "max_addresses_at_coordinate": scalar(
                conn,
                "SELECT COALESCE(MAX(n),0) FROM ("
                " SELECT COUNT(*) AS n FROM places GROUP BY lat,lon"
                ")",
            ),
            "floor_rows": scalar(
                conn,
                "SELECT COUNT(*) FROM places "
                "WHERE INSTR(number, '號') > 0 "
                "  AND (INSTR(number, '樓') > INSTR(number, '號') "
                "    OR INSTR(number, '層') > INSTR(number, '號'))",
            ),
            "duplicate_base_address_groups": scalar(
                conn,
                f"SELECT COUNT(*) FROM ("
                f" SELECT 1 FROM places GROUP BY {BASE_KEY} HAVING COUNT(*) > 1"
                f")",
            ),
            "rtree_rows": scalar(conn, "SELECT COUNT(*) FROM places_rtree"),
            "fts_rows": scalar(conn, "SELECT COUNT(*) FROM places_fts"),
            "metadata": metadata,
        }
    finally:
        conn.close()


def logical_diff(baseline: Path, candidate: Path) -> dict:
    conn = sqlite3.connect(f"file:{candidate.resolve().as_posix()}?mode=ro", uri=True)
    try:
        conn.execute(
            "ATTACH DATABASE ? AS baseline",
            (f"file:{baseline.resolve().as_posix()}?mode=ro",),
        )
        coordinate_pairs_added = scalar(
            conn,
            "SELECT COUNT(*) FROM ("
            " SELECT lat,lon,display_name FROM main.places"
            " EXCEPT"
            " SELECT lat,lon,display_name FROM baseline.places"
            ")",
        )
        coordinate_pairs_removed = scalar(
            conn,
            "SELECT COUNT(*) FROM ("
            " SELECT lat,lon,display_name FROM baseline.places"
            " EXCEPT"
            " SELECT lat,lon,display_name FROM main.places"
            ")",
        )
        coordinate_pairs_unchanged = scalar(
            conn,
            "SELECT COUNT(*) FROM ("
            " SELECT lat,lon,display_name FROM main.places"
            " INTERSECT"
            " SELECT lat,lon,display_name FROM baseline.places"
            ")",
        )
        display_names_added = scalar(
            conn,
            "SELECT COUNT(*) FROM ("
            " SELECT display_name FROM main.places"
            " EXCEPT"
            " SELECT display_name FROM baseline.places"
            ")",
        )
        display_names_removed = scalar(
            conn,
            "SELECT COUNT(*) FROM ("
            " SELECT display_name FROM baseline.places"
            " EXCEPT"
            " SELECT display_name FROM main.places"
            ")",
        )
        display_names_unchanged = scalar(
            conn,
            "SELECT COUNT(*) FROM ("
            " SELECT display_name FROM main.places"
            " INTERSECT"
            " SELECT display_name FROM baseline.places"
            ")",
        )
        coordinate_delta = conn.execute(
            "SELECT COUNT(*), "
            "       COALESCE(MAX(ABS(n.lat - o.lat)), 0), "
            "       COALESCE(MAX(ABS(n.lon - o.lon)), 0) "
            "FROM main.places n "
            "JOIN baseline.places o ON o.id = n.id "
            "WHERE o.display_name = n.display_name "
            "  AND (o.lat <> n.lat OR o.lon <> n.lon)"
        ).fetchone()
        return {
            "coordinate_address_pairs_added": coordinate_pairs_added,
            "coordinate_address_pairs_removed": coordinate_pairs_removed,
            "coordinate_address_pairs_unchanged": coordinate_pairs_unchanged,
            "display_names_added": display_names_added,
            "display_names_removed": display_names_removed,
            "display_names_unchanged": display_names_unchanged,
            "same_id_name_coordinate_changes": coordinate_delta[0],
            "max_latitude_delta_degrees": coordinate_delta[1],
            "max_longitude_delta_degrees": coordinate_delta[2],
        }
    finally:
        conn.close()


def root_artifacts(directory: Path) -> dict:
    result = {}
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.suffix in {".sqlite", ".zip", ".txt"}:
            result[path.name] = {
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
    return result


def markdown(report: dict) -> str:
    lines = [
        "# Baseline vs Candidate Address Output Comparison",
        "",
        f"- Baseline: `{report['baseline']}`",
        f"- Candidate: `{report['candidate']}`",
        "",
    ]
    for name, values in report["counties"].items():
        old = values["baseline"]
        new = values["candidate"]
        diff = values["logical_diff"]
        lines.extend([
            f"## {name}",
            "",
            "| Metric | Baseline | Candidate | Delta |",
            "|---|---:|---:|---:|",
        ])
        for key in (
            "bytes", "rows", "unique_coordinates",
            "coordinates_with_multiple_addresses",
            "max_addresses_at_coordinate", "floor_rows",
            "duplicate_base_address_groups", "rtree_rows", "fts_rows",
        ):
            lines.append(
                f"| `{key}` | {old[key]} | {new[key]} | {new[key] - old[key]} |"
            )
        lines.extend([
            "",
            f"- Baseline SHA-256: `{old['sha256']}`",
            f"- Candidate SHA-256: `{new['sha256']}`",
            f"- Candidate quick check: `{new['quick_check']}`",
            f"- Synthesized base addresses: "
            f"`{new['metadata'].get('deduped_synthesized', '0')}`",
            f"- Display names added: `{diff['display_names_added']}`",
            f"- Display names removed: `{diff['display_names_removed']}`",
            f"- Display names unchanged: `{diff['display_names_unchanged']}`",
            f"- Exact coordinate/address pairs added: "
            f"`{diff['coordinate_address_pairs_added']}`",
            f"- Exact coordinate/address pairs removed: "
            f"`{diff['coordinate_address_pairs_removed']}`",
            f"- Exact coordinate/address pairs unchanged: "
            f"`{diff['coordinate_address_pairs_unchanged']}`",
            f"- Same-id/name rows with coordinate float changes: "
            f"`{diff['same_id_name_coordinate_changes']}`",
            f"- Maximum latitude/longitude delta (degrees): "
            f"`{diff['max_latitude_delta_degrees']:.12g}` / "
            f"`{diff['max_longitude_delta_degrees']:.12g}`",
            "",
        ])
    old_artifacts = report["artifacts"]["baseline"]
    new_artifacts = report["artifacts"]["candidate"]
    lines.extend([
        "## Root artifact comparison",
        "",
        "| Artifact | Baseline bytes | Candidate bytes | Delta | SHA-256 changed |",
        "|---|---:|---:|---:|:---:|",
    ])
    for name in sorted(set(old_artifacts) | set(new_artifacts)):
        old = old_artifacts.get(name)
        new = new_artifacts.get(name)
        if old and new:
            lines.append(
                f"| `{name}` | {old['bytes']} | {new['bytes']} | "
                f"{new['bytes'] - old['bytes']} | "
                f"{'yes' if old['sha256'] != new['sha256'] else 'no'} |"
            )
        elif old:
            lines.append(
                f"| `{name}` | {old['bytes']} | — | — | removed |"
            )
        else:
            lines.append(
                f"| `{name}` | — | {new['bytes']} | — | added |"
            )
    lines.extend([
        "",
        "## Acceptance summary",
        "",
    ])
    for name, values in report["counties"].items():
        new = values["candidate"]
        ok = (
            new["quick_check"] == "ok"
            and new["rows"] == new["rtree_rows"] == new["fts_rows"]
            and new["floor_rows"] == 0
            and new["duplicate_base_address_groups"] == 0
        )
        lines.append(f"- {name}: {'PASS' if ok else 'FAIL'}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    args = parser.parse_args()

    report = {
        "baseline": str(args.baseline.resolve()),
        "candidate": str(args.candidate.resolve()),
        "artifacts": {
            "baseline": root_artifacts(args.baseline),
            "candidate": root_artifacts(args.candidate),
        },
        "counties": {},
    }
    for name in COUNTY_FILES:
        baseline_db = args.baseline / name
        candidate_db = args.candidate / name
        report["counties"][name] = {
            "baseline": inspect_db(baseline_db),
            "candidate": inspect_db(candidate_db),
            "logical_diff": logical_diff(baseline_db, candidate_db),
        }

    args.report_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.report_dir / "address-output-comparison.json"
    md_path = args.report_dir / "address-output-comparison.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(markdown(report), encoding="utf-8")
    print(md_path)
    print(json_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
