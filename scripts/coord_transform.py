"""Coordinate transforms between TWD97 (EPSG:3826) and WGS84 (EPSG:4326).

TWD97 / TM2 zone 121 is the Taiwan government's standard projection for
land surveys. MOI TGOS address files for non-municipality counties (e.g.
彰化縣) ship only TWD97 X/Y; we reproject them to WGS84 lon/lat for
GPS-compatible storage.

Run this file directly to execute a round-trip self-test against known
landmarks:
    python3 scripts/coord_transform.py
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Tuple

from pyproj import Transformer

# Cached transformers — building one is non-trivial.
_TO_WGS84 = Transformer.from_crs("EPSG:3826", "EPSG:4326", always_xy=True)
_TO_TWD97 = Transformer.from_crs("EPSG:4326", "EPSG:3826", always_xy=True)


def twd97_to_wgs84(x: float, y: float) -> Tuple[float, float]:
    """TWD97 (east_m, north_m) → WGS84 (lon, lat)."""
    lon, lat = _TO_WGS84.transform(x, y)
    return lon, lat


def wgs84_to_twd97(lon: float, lat: float) -> Tuple[float, float]:
    """WGS84 (lon, lat) → TWD97 (east_m, north_m)."""
    x, y = _TO_TWD97.transform(lon, lat)
    return x, y


def twd97_to_wgs84_bulk(pairs: Iterable[Tuple[float, float]]):
    """Streamed bulk projection. Yields (lon, lat) for each (x, y).

    pyproj is much faster on arrays than per-call invocation, but for a
    streaming CSV pipeline a generator is the convenient shape.
    """
    for x, y in pairs:
        yield _TO_WGS84.transform(x, y)


@dataclass(frozen=True)
class _Landmark:
    name: str
    twd97_x: float
    twd97_y: float
    wgs84_lon: float
    wgs84_lat: float
    tolerance_m: float = 5.0


# Ground-truth pairs lifted directly from the TGOS Taichung CSV, which
# carries both TWD97 and WGS84 columns for every row — they ARE the data
# we're projecting, so any drift here points to a misconfigured datum.
# Three rows spread across the bounding box.
_LANDMARKS = [
    _Landmark(
        name="台中中區大誠街 (central)",
        twd97_x=217337.1278, twd97_y=2671158.6333,
        wgs84_lon=120.678601565596, wgs84_lat=24.145357080518,
        tolerance_m=1.0,
    ),
    _Landmark(
        name="台中和平區栗林里 (eastern mountain)",
        twd97_x=220537.631598, twd97_y=2681215.7003,
        wgs84_lon=120.709888447488, wgs84_lat=24.236227780267,
        tolerance_m=1.0,
    ),
    _Landmark(
        name="台中大安區南勢里 (coastal)",
        twd97_x=236612.154, twd97_y=2673787.926,
        wgs84_lon=120.868240306006, wgs84_lat=24.169379455535,
        tolerance_m=1.0,
    ),
]


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance in metres."""
    import math

    r = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _selftest() -> int:
    """Validate the projection round-trips and matches known landmarks.

    Returns 0 on pass, non-zero on failure.
    """
    failures = 0
    print("Coordinate transform self-test")
    print("=" * 60)

    for lm in _LANDMARKS:
        lon, lat = twd97_to_wgs84(lm.twd97_x, lm.twd97_y)
        err = _haversine_m(lon, lat, lm.wgs84_lon, lm.wgs84_lat)
        verdict = "PASS" if err <= lm.tolerance_m else "FAIL"
        if verdict == "FAIL":
            failures += 1
        print(
            f"[{verdict}] {lm.name:<40s} "
            f"got ({lon:.4f}, {lat:.4f}), expected ({lm.wgs84_lon}, {lm.wgs84_lat}), "
            f"err={err:.1f}m / tol={lm.tolerance_m}m"
        )

    # Round-trip stability: bouncing one value back and forth should not
    # drift more than 0.1m, regardless of where it is in the projection.
    print("-" * 60)
    cases = [
        (217337.13, 2671158.63),    # Taichung 大誠街 (from CSV row 2)
        (209310.67, 2665209.68),    # Changhua 三村里 (from CSV row 2)
    ]
    for x, y in cases:
        lon, lat = twd97_to_wgs84(x, y)
        x2, y2 = wgs84_to_twd97(lon, lat)
        err = ((x - x2) ** 2 + (y - y2) ** 2) ** 0.5
        verdict = "PASS" if err <= 0.1 else "FAIL"
        if verdict == "FAIL":
            failures += 1
        print(
            f"[{verdict}] round-trip ({x:.2f}, {y:.2f}) → "
            f"({lon:.6f}, {lat:.6f}) → ({x2:.2f}, {y2:.2f}); err={err:.3f}m"
        )

    print("=" * 60)
    print(f"{'OK' if failures == 0 else 'FAIL'}: {failures} failures")
    return failures


if __name__ == "__main__":
    raise SystemExit(_selftest())
