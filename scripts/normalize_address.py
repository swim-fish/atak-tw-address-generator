"""Taiwan address text normalisation helpers.

TGOS CSV stores street numbers in fullwidth Han glyphs:
    ２之３之２號
    ３９巷
    臨１１２之４號

User search input mixes fullwidth and halfwidth freely. We normalise to a
halfwidth canonical form for FTS5 indexing, but keep the original form for
display. We also compose the full Taiwan-style display name from the
TGOS column set.

Run this file directly for the self-test:
    python3 scripts/normalize_address.py
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Fullwidth digits → halfwidth.
_FULL_TO_HALF_DIGITS = {0xFF10 + i: 0x0030 + i for i in range(10)}

# Common fullwidth ASCII punctuation occasionally seen in addresses.
_FULL_TO_HALF_PUNCT = {
    0xFF0D: 0x002D,  # full hyphen → '-'
    0xFF0E: 0x002E,  # full period → '.'
}

_TO_HALF = {**_FULL_TO_HALF_DIGITS, **_FULL_TO_HALF_PUNCT}


def to_halfwidth(s: str) -> str:
    """Convert fullwidth digits/punctuation in s to halfwidth.

    Han chars (號, 巷, 弄, 之, 樓, 臨, etc.) are left as-is — they are not
    decorations, they are part of the address grammar.
    """
    return s.translate(_TO_HALF)


def normalize_number(number: str) -> str:
    """Normalise the 號 column from TGOS.

    Input examples (台中 and 彰化):
        '２之３之２號'   → '2之3之2號'
        '臨１１２之４號' → '臨112之4號'
        '５１號'         → '51號'
        ''               → ''
    The 之 character is intentionally retained — it carries semantic
    information (hyphenated sub-address, e.g. 「2-3-2號」). For a more
    human-friendly halfwidth display we also offer hyphenated form via
    :func:`number_with_hyphens`.
    """
    return to_halfwidth(number)


def number_with_hyphens(number: str) -> str:
    """Halfwidth + replace 之 with '-' for compact display.

    '２之３之２號' → '2-3-2號'
    '臨１１２之４號' → '臨112-4號'
    """
    return to_halfwidth(number).replace("之", "-")


@dataclass
class AddressParts:
    """Components used when composing a display name."""

    county: str           # 台中市 / 彰化縣
    district: str         # 北區 / 鹿港鎮
    village: str = ""     # 大誠里 / 三村里
    street: str = ""      # 大誠街
    area: str = ""        # 地區 (rarely used)
    lane: str = ""        # 巷
    alley: str = ""       # 弄
    number: str = ""      # 號


def _maybe(part: str, suffix: str = "") -> str:
    """Render part + suffix if part is non-empty, else empty string."""
    return f"{part}{suffix}" if part else ""


def compose_display_name(parts: AddressParts, *, halfwidth: bool = False) -> str:
    """Compose a Taiwan-style display name.

    Halfwidth=True returns the normalised form used for FTS5 search.
    Halfwidth=False preserves the original (fullwidth digit) form used in
    the human-facing UI.

    Empty 街 with non-empty 巷/弄 is the "orphan" case (1.3% in Taichung,
    9.4% in Changhua) — we fall back to 地區 if present.
    """
    if halfwidth:
        v = AddressParts(
            county=parts.county,
            district=parts.district,
            village=parts.village,
            street=to_halfwidth(parts.street),
            area=to_halfwidth(parts.area),
            lane=to_halfwidth(parts.lane),
            alley=to_halfwidth(parts.alley),
            number=number_with_hyphens(parts.number),
        )
    else:
        v = parts

    head = v.county + v.district + v.village
    body_parts = []
    if v.street:
        body_parts.append(v.street)
    elif v.area:
        body_parts.append(v.area)
    if v.lane:
        body_parts.append(v.lane)
    if v.alley:
        body_parts.append(v.alley)
    if v.number:
        body_parts.append(v.number)
    return head + "".join(body_parts)


# ============================================================================
# Self-test
# ============================================================================

def _selftest() -> int:
    failures = 0

    cases_halfwidth = [
        ("２之３之２號", "2之3之2號"),
        ("臨１１２之４號", "臨112之4號"),
        ("５１號", "51號"),
        ("３９巷", "39巷"),
        ("", ""),
    ]
    for raw, want in cases_halfwidth:
        got = normalize_number(raw)
        ok = got == want
        if not ok:
            failures += 1
        print(f"[{'PASS' if ok else 'FAIL'}] to_halfwidth({raw!r}) -> {got!r} (want {want!r})")

    cases_hyphen = [
        ("２之３之２號", "2-3-2號"),
        ("臨１１２之４號", "臨112-4號"),
    ]
    for raw, want in cases_hyphen:
        got = number_with_hyphens(raw)
        ok = got == want
        if not ok:
            failures += 1
        print(f"[{'PASS' if ok else 'FAIL'}] number_with_hyphens({raw!r}) -> {got!r} (want {want!r})")

    # display_name composition. Use the Taichung row 2 from the CSV:
    #   6600100,大誠里,016,大誠街,, ３９巷, , ２之３之２號
    p = AddressParts(
        county="台中市",
        district="中區",
        village="大誠里",
        street="大誠街",
        lane="３９巷",
        number="２之３之２號",
    )
    fw = compose_display_name(p, halfwidth=False)
    hw = compose_display_name(p, halfwidth=True)
    expected_fw = "台中市中區大誠里大誠街３９巷２之３之２號"
    expected_hw = "台中市中區大誠里大誠街39巷2-3-2號"
    for label, got, want in [("fullwidth", fw, expected_fw), ("halfwidth", hw, expected_hw)]:
        ok = got == want
        if not ok:
            failures += 1
        print(f"[{'PASS' if ok else 'FAIL'}] compose_display_name {label} -> {got!r} (want {want!r})")

    # Orphan address (empty street, 地區 fallback) — sample from Changhua patterns
    p2 = AddressParts(
        county="彰化縣",
        district="鹿港鎮",
        village="頂厝里",
        street="",
        area="頂厝段",
        number="５號",
    )
    expected_hw2 = "彰化縣鹿港鎮頂厝里頂厝段5號"
    got_hw2 = compose_display_name(p2, halfwidth=True)
    ok2 = got_hw2 == expected_hw2
    if not ok2:
        failures += 1
    print(f"[{'PASS' if ok2 else 'FAIL'}] orphan (area fallback) -> {got_hw2!r} (want {expected_hw2!r})")

    print(f"\n{'OK' if failures == 0 else 'FAIL'}: {failures} failures")
    return failures


if __name__ == "__main__":
    raise SystemExit(_selftest())
