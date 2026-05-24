"""Download Geofabrik Taiwan PBF (with Last-Modified cache) and clip to bbox.

Modelled on the upstream VNS pipeline's ``is_file_current`` pattern:
the remote ``Last-Modified`` header is compared against a sidecar
timestamp file; if unchanged, skip the download. After download, run
``osmium extract --bbox`` to produce ``cache/tw-central.osm.pbf``.

Usage (inside container):
    python3 scripts/clip_pbf.py
    python3 scripts/clip_pbf.py --no-refresh   # skip Last-Modified check
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import yaml

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

GEOFABRIK_BASE = "https://download.geofabrik.de"


def load_region(region_id: str) -> dict:
    config = yaml.safe_load((CONFIG_DIR / "regions.yaml").read_text("utf-8"))
    if region_id not in config:
        print(f"ERROR: region '{region_id}' not in regions.yaml", file=sys.stderr)
        sys.exit(2)
    return config[region_id]


def remote_last_modified(url: str) -> str | None:
    """HEAD the URL; return Last-Modified header or None on failure."""
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.headers.get("Last-Modified")
    except Exception as e:
        print(f"WARN: HEAD {url} failed: {e}", file=sys.stderr)
        return None


def download_with_progress(url: str, dest: Path) -> None:
    """Stream the URL to dest, printing rough progress."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"[clip] downloading {url}")
    print(f"[clip] → {dest}")
    t0 = time.time()
    with urllib.request.urlopen(url, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length", "0"))
        with open(tmp, "wb") as f:
            chunk = 1 << 20  # 1 MiB
            done = 0
            last_print = 0.0
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                f.write(buf)
                done += len(buf)
                now = time.time()
                if now - last_print > 1.0:
                    last_print = now
                    pct = (done / total * 100) if total else 0
                    mbps = done / (now - t0) / (1 << 20)
                    print(f"[clip]   {done / (1 << 20):8.1f} MiB "
                          f"{pct:5.1f}% @ {mbps:.1f} MiB/s")
    tmp.replace(dest)
    print(f"[clip] download complete in {time.time() - t0:.1f}s")


def ensure_pbf_current(region: dict, *, refresh: bool) -> Path:
    """Return path to a current Geofabrik PBF for the region's geofabrik_region.

    If the remote Last-Modified header matches the local sidecar timestamp,
    skip the download. Auto-backup the prior file on update.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    geofabrik = region["geofabrik_region"]
    name = geofabrik.split("/")[-1]      # 'asia/taiwan' → 'taiwan'
    url = f"{GEOFABRIK_BASE}/{geofabrik}-latest.osm.pbf"
    pbf = CACHE_DIR / f"{name}-latest.osm.pbf"
    ts_file = CACHE_DIR / f"{name}-latest.osm.pbf.last-modified"

    if not refresh:
        if not pbf.exists():
            print(f"ERROR: --no-refresh but cached PBF missing: {pbf}", file=sys.stderr)
            sys.exit(3)
        print(f"[clip] --no-refresh; using cached {pbf}")
        return pbf

    remote_lm = remote_last_modified(url)
    cached_lm = ts_file.read_text("utf-8").strip() if ts_file.exists() else None

    if pbf.exists() and remote_lm and cached_lm == remote_lm:
        print(f"[clip] cached PBF current ({remote_lm}); skipping download")
        return pbf

    if pbf.exists():
        backup = pbf.with_suffix(pbf.suffix + ".bak")
        print(f"[clip] backing up old PBF to {backup}")
        pbf.replace(backup)

    download_with_progress(url, pbf)
    if remote_lm:
        ts_file.write_text(remote_lm, encoding="utf-8")
    return pbf


def osmium_extract(src: Path, dst: Path, bbox: dict) -> None:
    """Run `osmium extract --bbox` to clip the regional PBF."""
    bbox_str = f"{bbox['west']},{bbox['south']},{bbox['east']},{bbox['north']}"
    cmd = [
        "osmium", "extract",
        "--bbox", bbox_str,
        "--overwrite",
        "--strategy=smart",   # picks the right strategy for our medium size
        "-o", str(dst),
        str(src),
    ]
    print(f"[clip] osmium extract bbox={bbox_str}")
    print(f"[clip] cmd: {' '.join(cmd)}")
    t0 = time.time()
    subprocess.run(cmd, check=True)
    print(f"[clip] done in {time.time() - t0:.1f}s; "
          f"output {dst.stat().st_size / (1 << 20):.1f} MiB")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--region", default="tw-central")
    p.add_argument("--no-refresh", action="store_true",
                   help="Skip Last-Modified check; use cached PBF as-is.")
    args = p.parse_args()

    region = load_region(args.region)
    src_pbf = ensure_pbf_current(region, refresh=not args.no_refresh)

    out_dir = CACHE_DIR
    out = out_dir / f"{args.region}.osm.pbf"
    osmium_extract(src_pbf, out, region["bbox"])
    print(f"[clip] OK — {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
