#!/usr/bin/env python3
"""Unit tests for base-address-aware TGOS reduction."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import base_address as ba
import collapse_coords
import dedup_floors


PLACE_COLUMNS = """
CREATE TABLE places (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'tgos',
    osm_id INTEGER,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    district_code TEXT NOT NULL,
    county TEXT NOT NULL,
    township TEXT NOT NULL,
    village TEXT,
    neighbor TEXT,
    street TEXT,
    area TEXT,
    lane TEXT,
    alley TEXT,
    number TEXT,
    name TEXT,
    display_name TEXT,
    display_name_halfwidth TEXT
)
"""


def insert_place(
    conn: sqlite3.Connection,
    row_id: int,
    lat: float,
    lon: float,
    number: str,
) -> None:
    display = f"台中市中區測試里測試路{number}"
    conn.execute(
        "INSERT INTO places "
        "(id,lat,lon,district_code,county,township,village,street,area,lane,"
        " alley,number,name,display_name,display_name_halfwidth) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            row_id, lat, lon, "6600100", "台中市", "中區", "測試里",
            "測試路", None, None, None, number, f"測試路{number}",
            display, display,
        ),
    )


class BaseAddressHelpersTest(unittest.TestCase):
    def test_base_number_examples(self) -> None:
        self.assertEqual(ba.base_number("３２號二樓"), "３２號")
        self.assertEqual(ba.base_number("３２號三樓之１"), "３２號")
        self.assertEqual(ba.base_number("３２號地下一層"), "３２號")
        self.assertEqual(ba.base_number("３２之５號二樓"), "３２之５號")
        self.assertEqual(ba.base_number("臨１１２之４號三樓"), "臨１１２之４號")
        self.assertEqual(ba.base_number("３２之５號"), "３２之５號")
        self.assertEqual(ba.base_number("合作大樓１之１號"), "合作大樓１之１號")
        self.assertFalse(ba.is_floor_number("合作大樓１之１號"))
        self.assertEqual(ba.base_number("地下二層"), "地下二層")

    def test_synthesized_fields(self) -> None:
        fields = ba.synthesize_fields(
            county="台中市",
            township="中區",
            village="測試里",
            street="測試路",
            area=None,
            lane=None,
            alley=None,
            number="３２號",
        )
        self.assertEqual(fields.number, "３２號")
        self.assertEqual(fields.name, "測試路32號")
        self.assertEqual(fields.display_name, "台中市中區測試里測試路３２號")
        self.assertEqual(
            fields.display_name_halfwidth,
            "台中市中區測試里測試路32號",
        )


class FloorReductionRankingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(PLACE_COLUMNS)

    def tearDown(self) -> None:
        self.conn.close()

    def ranked(self) -> dict[int, tuple[str, int, int]]:
        rows = self.conn.execute(
            dedup_floors._RANK_CTE +
            "SELECT id,base_number,to_drop,to_synthesize FROM ranked ORDER BY id"
        )
        return {row_id: (base, drop, synth) for row_id, base, drop, synth in rows}

    def test_existing_base_wins(self) -> None:
        insert_place(self.conn, 1, 24.0, 120.0, "３２號")
        insert_place(self.conn, 2, 24.0, 120.0, "３２號二樓")
        insert_place(self.conn, 3, 24.0, 120.0, "３２號三樓之１")
        ranked = self.ranked()
        self.assertEqual(ranked[1], ("３２號", 0, 0))
        self.assertEqual(ranked[2], ("３２號", 1, 0))
        self.assertEqual(ranked[3], ("３２號", 1, 0))

    def test_floor_only_group_synthesizes_lowest_id(self) -> None:
        insert_place(self.conn, 4, 24.1, 120.1, "３３號地下一層")
        insert_place(self.conn, 5, 24.1, 120.1, "３３號二樓")
        ranked = self.ranked()
        self.assertEqual(ranked[4], ("３３號", 0, 1))
        self.assertEqual(ranked[5], ("３３號", 1, 0))

    def test_different_base_at_same_coordinate_is_preserved(self) -> None:
        insert_place(self.conn, 6, 24.2, 120.2, "３１號")
        insert_place(self.conn, 7, 24.2, 120.2, "３２號二樓")
        ranked = self.ranked()
        self.assertEqual(ranked[6], ("３１號", 0, 0))
        self.assertEqual(ranked[7], ("３２號", 0, 1))

    def test_unparseable_floor_is_not_modified(self) -> None:
        insert_place(self.conn, 8, 24.3, 120.3, "地下二層")
        ranked = self.ranked()
        self.assertEqual(ranked[8], ("地下二層", 0, 0))


class BaseDuplicateRankingTest(unittest.TestCase):
    def test_same_coordinate_different_base_survives(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(PLACE_COLUMNS)
            insert_place(conn, 1, 24.0, 120.0, "３１號")
            insert_place(conn, 2, 24.0, 120.0, "３２號")
            insert_place(conn, 3, 24.0, 120.0, "３２號")
            rows = conn.execute(
                collapse_coords._RANK_CTE +
                "SELECT id,rn FROM ranked ORDER BY id"
            ).fetchall()
            self.assertEqual(rows, [(1, 1), (2, 1), (3, 2)])
        finally:
            conn.close()


class ApplyIntegrationTest(unittest.TestCase):
    def test_floor_only_base_is_rewritten_and_indexes_stay_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places-smoke.sqlite"
            conn = sqlite3.connect(db_path)
            conn.execute(PLACE_COLUMNS)
            conn.executescript(
                """
                CREATE VIRTUAL TABLE places_fts USING fts5(
                    name, display_name, display_name_halfwidth,
                    street, area, township,
                    content='places', content_rowid='id', tokenize='unicode61'
                );
                CREATE VIRTUAL TABLE places_rtree USING rtree(
                    id, min_lat, max_lat, min_lon, max_lon
                );
                CREATE TABLE metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            insert_place(conn, 1, 24.0, 120.0, "３１號")
            insert_place(conn, 2, 24.0, 120.0, "３２號二樓")
            conn.execute(
                "INSERT INTO places_rtree "
                "SELECT id,lat,lat,lon,lon FROM places"
            )
            conn.execute("INSERT INTO places_fts(places_fts) VALUES('rebuild')")
            conn.execute(
                "INSERT INTO metadata(key,value) VALUES('inserted','2')"
            )
            conn.commit()
            conn.close()

            stats_conn = sqlite3.connect(db_path)
            try:
                stats = dedup_floors._summary(stats_conn)
            finally:
                stats_conn.close()
            self.assertEqual(stats["to_remove"], 0)
            self.assertEqual(stats["to_synthesize"], 1)
            dedup_floors._apply(
                db_path,
                stats["to_remove"],
                stats["to_synthesize"],
                stats["non_suffix_floor_markers"],
            )

            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute(
                    "SELECT number,display_name FROM places ORDER BY id"
                ).fetchall()
                self.assertEqual(
                    rows,
                    [
                        ("３１號", "台中市中區測試里測試路３１號"),
                        ("３２號", "台中市中區測試里測試路３２號"),
                    ],
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM places_rtree").fetchone()[0],
                    2,
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM places_fts").fetchone()[0],
                    2,
                )
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
