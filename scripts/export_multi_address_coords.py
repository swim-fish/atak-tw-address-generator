#!/usr/bin/env python3
"""Export addresses that share an exact coordinate to a UTF-8 CSV.

By default, the script reads the rebuilt Taichung and Changhua databases and
writes one row per address for every coordinate containing two or more rows.
The ``original_address`` column is copied directly from ``places.display_name``.
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASES = (
    ROOT / "output" / "places-taichung.sqlite",
    ROOT / "output" / "places-changhua.sqlite",
)
DEFAULT_OUTPUT = (
    ROOT / "output" / "reports" / "multi-address-coordinates.csv"
)

EXPORT_SQL = """
WITH multi_coordinates AS (
    SELECT lat, lon, COUNT(*) AS address_count
    FROM places
    GROUP BY lat, lon
    HAVING COUNT(*) > 1
)
SELECT
    p.county,
    p.lat,
    p.lon,
    m.address_count,
    p.id,
    p.display_name,
    p.number,
    p.township,
    COALESCE(p.village, ''),
    COALESCE(p.street, ''),
    COALESCE(p.area, ''),
    COALESCE(p.lane, ''),
    COALESCE(p.alley, '')
FROM places AS p
JOIN multi_coordinates AS m
  ON m.lat = p.lat
 AND m.lon = p.lon
ORDER BY p.county, p.lat, p.lon, p.id
"""

HEADER = (
    "county",
    "latitude",
    "longitude",
    "address_count_at_coordinate",
    "source_id",
    "original_address",
    "number",
    "township",
    "village",
    "street",
    "area",
    "lane",
    "alley",
)


def readonly_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def export(databases: list[Path], output_path: Path) -> tuple[int, int]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    row_count = 0
    coordinate_count = 0

    with output_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(HEADER)

        for database in databases:
            if not database.is_file():
                raise FileNotFoundError(f"database not found: {database}")

            with readonly_connection(database) as connection:
                previous_coordinate: tuple[float, float] | None = None
                for row in connection.execute(EXPORT_SQL):
                    coordinate = (row[1], row[2])
                    if coordinate != previous_coordinate:
                        coordinate_count += 1
                        previous_coordinate = coordinate
                    writer.writerow(row)
                    row_count += 1

    return row_count, coordinate_count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        action="append",
        dest="databases",
        help=(
            "Input places SQLite; repeat for multiple files. Defaults to "
            "places-taichung.sqlite and places-changhua.sqlite."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output CSV path (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()

    databases = args.databases or list(DEFAULT_DATABASES)
    rows, coordinates = export(databases, args.output)
    print(f"Exported {rows:,} address rows from {coordinates:,} coordinates")
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
