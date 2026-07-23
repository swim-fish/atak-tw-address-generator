"""Load and validate the release-facing data version configuration."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "data_version.yaml"
DATA_VERSION_PATTERN = re.compile(r"^\d{4}\.\d{2}\.\d{2}\.[1-9]\d*$")
POLICY_VERSION_PATTERN = re.compile(r"^[1-9]\d*$")


@dataclass(frozen=True)
class DataVersion:
    data_version: str
    address_policy_version: str

    def common_metadata(self) -> dict[str, str]:
        return {"data_version": self.data_version}

    def tgos_metadata(self) -> dict[str, str]:
        return {
            "data_version": self.data_version,
            "address_policy_version": self.address_policy_version,
        }


def load(path: Path = DEFAULT_CONFIG) -> DataVersion:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data = str(raw.get("data_version", "")).strip()
    policy = str(raw.get("address_policy_version", "")).strip()
    if not DATA_VERSION_PATTERN.fullmatch(data):
        raise ValueError(
            "data_version must use YYYY.MM.DD.REVISION with a positive revision"
        )
    if not POLICY_VERSION_PATTERN.fullmatch(policy):
        raise ValueError("address_policy_version must be a positive integer")
    return DataVersion(data, policy)


def read_metadata(sqlite_path: Path) -> dict[str, str]:
    uri = f"file:{sqlite_path.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        return dict(connection.execute("SELECT key, value FROM metadata"))
    finally:
        connection.close()


def expected_metadata(
    version: DataVersion, source: str | None
) -> dict[str, str]:
    if source == "tgos":
        return version.tgos_metadata()
    return version.common_metadata()


def mismatches(
    metadata: dict[str, str], version: DataVersion
) -> list[str]:
    expected = expected_metadata(version, metadata.get("source"))
    return [
        f"{key}={metadata.get(key)!r}, expected {value!r}"
        for key, value in expected.items()
        if metadata.get(key) != value
    ]
