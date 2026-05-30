"""Package the per-kit ZIPs and their manifests.

Produces (in ``output/``):

- ``base.zip``                — townships + roads + places-osm
- ``places-taichung.zip``     — places-taichung.sqlite alone
- ``places-changhua.zip``     — places-changhua.sqlite alone
- ``tw-central-full.zip``     — all four sqlite files in one folder

Each ZIP has a sibling ``*.manifest.txt`` carrying SHA-256, source SHA-256
from the sqlite metadata table, region bbox, and ISO 8601 build time —
mirroring the upstream VNS pipeline's manifest style.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import shutil
import sqlite3
import sys
import zipfile
from pathlib import Path

import yaml

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
STAGING = OUTPUT_DIR / "staging"


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_meta(sqlite_path: Path) -> dict:
    if not sqlite_path.exists():
        return {}
    conn = sqlite3.connect(str(sqlite_path))
    try:
        rows = conn.execute("SELECT key, value FROM metadata").fetchall()
        return dict(rows)
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


def write_zip(staging_dir: Path, zip_path: Path) -> None:
    """Recursively zip staging_dir contents (flat, relative paths)."""
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p in sorted(staging_dir.rglob("*")):
            if p.is_file():
                zf.write(p, arcname=p.relative_to(staging_dir))


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_manifest(
    kit_name: str,
    zip_path: Path,
    manifest_path: Path,
    region_id: str,
    bbox_str: str,
    contents: list[tuple[str, Path]],
) -> None:
    zip_sha = sha256_of_file(zip_path)
    zip_size_mib = zip_path.stat().st_size / (1 << 20)
    lines = [
        f"Kit:              {kit_name}",
        f"Region:           {region_id}",
        f"Bbox:             {bbox_str}",
        f"Generated at:     {now_iso()}",
        f"ZIP size:         {zip_size_mib:.1f} MiB",
        f"ZIP SHA-256:      {zip_sha}",
        f"Source repo:      atak_vns_offline_routing/atak-tw-address-generator",
        f"",
        f"Contents:",
    ]
    for label, p in contents:
        if not p.exists():
            continue
        size_mib = p.stat().st_size / (1 << 20)
        file_sha = sha256_of_file(p)
        lines.append(f"  {label}")
        lines.append(f"    file:    {p.name}")
        lines.append(f"    size:    {size_mib:.1f} MiB")
        lines.append(f"    sha256:  {file_sha}")
        meta = read_meta(p)
        for k in ("schema_version", "source", "county", "data_date", "csv_sha256",
                  "inserted", "skipped_dirty", "inserted_level4",
                  "inserted_level7", "inserted_level8",
                  "landmarks", "addrs_kept", "addrs_excluded"):
            if k in meta:
                lines.append(f"    meta.{k}: {meta[k]}")
    manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def stage_kit(kit_name: str, files: list[Path], data_date_files: dict[str, str]) -> Path:
    """Copy files into staging/<kit_name>/ and write a timestamp file."""
    staging = STAGING / kit_name
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for f in files:
        if f.exists():
            shutil.copy2(f, staging / f.name)
    if data_date_files:
        for label, value in data_date_files.items():
            (staging / f"timestamp.{label}").write_text(value + "\n", encoding="utf-8")
    return staging


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--region", default="tw-central")
    args = p.parse_args()

    region_cfg = yaml.safe_load((CONFIG_DIR / "regions.yaml").read_text("utf-8"))[args.region]
    csv_sources = yaml.safe_load((CONFIG_DIR / "csv_sources.yaml").read_text("utf-8"))
    bbox = region_cfg["bbox"]
    bbox_str = f"{bbox['west']},{bbox['south']},{bbox['east']},{bbox['north']}"

    townships = OUTPUT_DIR / "townships.sqlite"
    roads = OUTPUT_DIR / "roads.sqlite"
    places_osm = OUTPUT_DIR / "places-osm.sqlite"

    # ---- base.zip ----
    print("[manifest] packaging base.zip ...")
    base_staging = stage_kit(
        "base",
        [townships, roads, places_osm],
        {"base": read_meta(places_osm).get("region", args.region)},
    )
    base_zip = OUTPUT_DIR / "base.zip"
    write_zip(base_staging, base_zip)
    write_manifest(
        kit_name="base",
        zip_path=base_zip,
        manifest_path=OUTPUT_DIR / "base.manifest.txt",
        region_id=args.region,
        bbox_str=bbox_str,
        contents=[
            ("townships (admin_level 4/7/8 polygons)", townships),
            ("roads (named highways)", roads),
            ("places-osm (landmarks + non-TGOS addr)", places_osm),
        ],
    )
    print(f"[manifest]   base.zip: {base_zip.stat().st_size / (1 << 20):.1f} MiB")

    # ---- per-county ZIPs ----
    county_zips: list[tuple[str, Path]] = []
    for county_id, src in csv_sources.items():
        county_sqlite = OUTPUT_DIR / src["output_sqlite"]
        if not county_sqlite.exists():
            print(f"[manifest] SKIP {county_id}: {county_sqlite} not present")
            continue
        print(f"[manifest] packaging places-{county_id}.zip ...")
        staging = stage_kit(
            f"places-{county_id}",
            [county_sqlite],
            {county_id: src.get("data_date", "")},
        )
        z = OUTPUT_DIR / f"places-{county_id}.zip"
        write_zip(staging, z)
        write_manifest(
            kit_name=f"places-{county_id}",
            zip_path=z,
            manifest_path=OUTPUT_DIR / f"places-{county_id}.manifest.txt",
            region_id=args.region,
            bbox_str=bbox_str,
            contents=[(f"places-{county_id} ({src['county_name']} TGOS)", county_sqlite)],
        )
        county_zips.append((county_id, z))
        print(f"[manifest]   places-{county_id}.zip: {z.stat().st_size / (1 << 20):.1f} MiB")

    # ---- tw-central-full.zip ----
    print("[manifest] packaging tw-central-full.zip ...")
    full_files = [townships, roads, places_osm]
    full_timestamps = {"base": args.region}
    for county_id, src in csv_sources.items():
        full_files.append(OUTPUT_DIR / src["output_sqlite"])
        full_timestamps[county_id] = src.get("data_date", "")
    full_staging = stage_kit("tw-central-full", full_files, full_timestamps)
    full_zip = OUTPUT_DIR / f"{args.region}-full.zip"
    write_zip(full_staging, full_zip)
    full_contents = [
        ("townships", townships),
        ("roads", roads),
        ("places-osm", places_osm),
    ]
    for county_id, _ in county_zips:
        full_contents.append((f"places-{county_id}", OUTPUT_DIR / csv_sources[county_id]["output_sqlite"]))
    write_manifest(
        kit_name=f"{args.region}-full",
        zip_path=full_zip,
        manifest_path=OUTPUT_DIR / f"{args.region}-full.manifest.txt",
        region_id=args.region,
        bbox_str=bbox_str,
        contents=full_contents,
    )
    print(f"[manifest]   {args.region}-full.zip: {full_zip.stat().st_size / (1 << 20):.1f} MiB")

    # Cleanup staging
    shutil.rmtree(STAGING, ignore_errors=True)

    print(f"\n[manifest] DONE — see {OUTPUT_DIR}/*.zip and *.manifest.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
