#!/usr/bin/env python3
"""Verify release data versions across SQLite, manifests, and ZIP sidecars."""
from __future__ import annotations

import argparse
import hashlib
import re
import zipfile
from pathlib import Path

import data_version as dv


ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"
FULL_RELEASE_ARTIFACTS = {
    "townships.sqlite",
    "roads.sqlite",
    "places-osm.sqlite",
    "places-taichung.sqlite",
    "places-changhua.sqlite",
    "base.zip",
    "places-taichung.zip",
    "places-changhua.zip",
    "tw-central-full.zip",
    "base.manifest.txt",
    "places-taichung.manifest.txt",
    "places-changhua.manifest.txt",
    "tw-central-full.manifest.txt",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def check_manifest(
    path: Path, version: dv.DataVersion, zip_path: Path
) -> list[str]:
    text = path.read_text(encoding="utf-8")
    errors = []
    expected_lines = (
        f"Data version:      {version.data_version}",
        f"Address policy:    {version.address_policy_version}",
    )
    for line in expected_lines:
        if line not in text:
            errors.append(f"{path}: missing {line!r}")
    match = re.search(r"^ZIP SHA-256:\s+([0-9a-f]{64})$", text, re.MULTILINE)
    if not match:
        errors.append(f"{path}: missing valid ZIP SHA-256")
    elif zip_path.is_file() and match.group(1) != sha256(zip_path):
        errors.append(f"{path}: ZIP SHA-256 does not match {zip_path.name}")
    return errors


def check_zip(path: Path, version: dv.DataVersion) -> list[str]:
    errors = []
    expected = {
        "timestamp.data-version": version.data_version,
        "timestamp.address-policy-version":
            version.address_policy_version,
    }
    with zipfile.ZipFile(path) as archive:
        for name, value in expected.items():
            try:
                got = archive.read(name).decode("utf-8")
            except KeyError:
                errors.append(f"{path}: missing {name}")
                continue
            if got.strip() != value:
                errors.append(
                    f"{path}: {name}={got.strip()!r}, expected {value!r}"
                )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--config", type=Path, default=dv.DEFAULT_CONFIG)
    parser.add_argument(
        "--require-full-kit",
        action="store_true",
        help="Fail unless all standard SQLite, ZIP, and manifest artifacts exist.",
    )
    args = parser.parse_args()

    version = dv.load(args.config)
    errors: list[str] = []
    checks = 0
    if args.require_full_kit:
        present = {path.name for path in args.output_dir.iterdir() if path.is_file()}
        for missing in sorted(FULL_RELEASE_ARTIFACTS - present):
            errors.append(f"{args.output_dir}: missing release artifact {missing}")

    for database in sorted(args.output_dir.glob("*.sqlite")):
        checks += 1
        metadata = dv.read_metadata(database)
        for mismatch in dv.mismatches(metadata, version):
            errors.append(f"{database}: {mismatch}")

    for manifest in sorted(args.output_dir.glob("*.manifest.txt")):
        checks += 1
        zip_path = manifest.with_name(
            manifest.name.removesuffix(".manifest.txt") + ".zip"
        )
        if not zip_path.is_file():
            errors.append(f"{manifest}: matching ZIP not found: {zip_path.name}")
        errors.extend(check_manifest(manifest, version, zip_path))

    for zip_path in sorted(args.output_dir.glob("*.zip")):
        checks += 1
        errors.extend(check_zip(zip_path, version))

    if checks == 0:
        print(f"No release artifacts found under {args.output_dir}")
        return 1
    if errors:
        for error in errors:
            print(f"[FAIL] {error}")
        print(f"{len(errors)} error(s) across {checks} artifact(s)")
        return 1

    print(
        f"PASS: {checks} artifact(s), data_version={version.data_version}, "
        f"address_policy_version={version.address_policy_version}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
