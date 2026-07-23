#!/usr/bin/env python3
"""Stamp existing SQLite artifacts with the configured data versions.

Fresh builds receive these keys from their producers. This command exists for
an intentional release-only restamp of already-built artifacts. Default mode
is dry-run; pass ``--apply`` to write.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import data_version as dv


ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"


def target_databases(explicit: list[Path] | None) -> list[Path]:
    return explicit or sorted(OUTPUT_DIR.glob("*.sqlite"))


def stamp(path: Path, version: dv.DataVersion, apply: bool) -> bool:
    metadata = dv.read_metadata(path)
    expected = dv.expected_metadata(version, metadata.get("source"))
    changes = {
        key: value
        for key, value in expected.items()
        if metadata.get(key) != value
    }
    if not changes:
        print(f"[OK] {path}: already current")
        return False

    detail = ", ".join(
        f"{key}: {metadata.get(key)!r} -> {value!r}"
        for key, value in changes.items()
    )
    print(f"[{'APPLY' if apply else 'DRY-RUN'}] {path}: {detail}")
    if not apply:
        return True

    connection = sqlite3.connect(path)
    try:
        with connection:
            connection.executemany(
                "INSERT INTO metadata(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                changes.items(),
            )
    finally:
        connection.close()
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        action="append",
        dest="databases",
        help="SQLite artifact to stamp; repeat as needed. Defaults to output/*.sqlite.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=dv.DEFAULT_CONFIG,
        help="Data-version YAML path.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write metadata. Without this flag the command is read-only.",
    )
    args = parser.parse_args()

    version = dv.load(args.config)
    databases = target_databases(args.databases)
    if not databases:
        print("No SQLite artifacts found.")
        return 1
    for database in databases:
        if not database.is_file():
            raise FileNotFoundError(database)
        stamp(database, version, args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
