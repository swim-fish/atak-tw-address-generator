#!/usr/bin/env python3
"""Unit tests for release-facing data-version helpers."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

import check_data_version as check
import data_version as dv
import stamp_data_version as stamp


class DataVersionTest(unittest.TestCase):
    def test_load_valid_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "version.yaml"
            path.write_text(
                'data_version: "2026.07.23.1"\n'
                'address_policy_version: "2"\n',
                encoding="utf-8",
            )
            version = dv.load(path)
            self.assertEqual(version.data_version, "2026.07.23.1")
            self.assertEqual(version.address_policy_version, "2")

    def test_rejects_invalid_calver(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "version.yaml"
            path.write_text(
                'data_version: "v1.2.3"\n'
                'address_policy_version: "2"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "YYYY.MM.DD.REVISION"):
                dv.load(path)

    def test_expected_metadata_depends_on_source(self) -> None:
        version = dv.DataVersion("2026.07.23.1", "2")
        self.assertEqual(
            dv.expected_metadata(version, "tgos"),
            {
                "data_version": "2026.07.23.1",
                "address_policy_version": "2",
            },
        )
        self.assertEqual(
            dv.expected_metadata(version, "osm-clipped"),
            {"data_version": "2026.07.23.1"},
        )

    def test_stamp_existing_tgos_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "places.sqlite"
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT)"
                )
                connection.execute(
                    "INSERT INTO metadata VALUES('source', 'tgos')"
                )
                connection.commit()
            finally:
                connection.close()

            version = dv.DataVersion("2026.07.23.1", "2")
            self.assertTrue(stamp.stamp(path, version, apply=True))
            metadata = dv.read_metadata(path)
            self.assertEqual(metadata["data_version"], "2026.07.23.1")
            self.assertEqual(metadata["address_policy_version"], "2")
            self.assertFalse(dv.mismatches(metadata, version))

    def test_zip_check_accepts_crlf_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kit.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "timestamp.data-version", "2026.07.23.1\r\n"
                )
                archive.writestr(
                    "timestamp.address-policy-version", "2\r\n"
                )
            version = dv.DataVersion("2026.07.23.1", "2")
            self.assertEqual(check.check_zip(path, version), [])


if __name__ == "__main__":
    unittest.main()
