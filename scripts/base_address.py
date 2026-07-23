"""Shared helpers for TGOS floor-to-base address consolidation."""
from __future__ import annotations

from dataclasses import dataclass

import normalize_address as na


def is_floor_number(number: str | None) -> bool:
    """Return whether TGOS ``number`` carries a floor suffix after ``號``.

    A marker before ``號`` can be part of a proper name such as
    ``合作大樓１之１號`` and must not be treated as a floor address.
    """
    value = number or ""
    number_marker = value.find("號")
    if number_marker < 0:
        return False
    return any(
        marker > number_marker
        for marker in (value.find("樓"), value.find("層"))
    )


def base_number(number: str | None) -> str | None:
    """Return the house-number prefix through the first ``號``.

    Non-floor numbers are returned unchanged.
    """
    value = number or ""
    if not is_floor_number(value):
        return value
    return value[: value.find("號") + 1]


@dataclass(frozen=True)
class SynthesizedFields:
    number: str
    name: str
    display_name: str
    display_name_halfwidth: str


def synthesize_fields(
    *,
    county: str,
    township: str,
    village: str | None,
    street: str | None,
    area: str | None,
    lane: str | None,
    alley: str | None,
    number: str,
) -> SynthesizedFields:
    """Compose all address strings for a synthesized base-address row."""
    parts = na.AddressParts(
        county=county,
        district=township,
        village=village or "",
        street=street or "",
        area=area or "",
        lane=lane or "",
        alley=alley or "",
        number=number,
    )
    display_name = na.compose_display_name(parts, halfwidth=False)
    display_name_halfwidth = na.compose_display_name(parts, halfwidth=True)

    compact = na.AddressParts(
        county="",
        district="",
        village="",
        street=(street or area or ""),
        area="",
        lane=lane or "",
        alley=alley or "",
        number=number,
    )
    name = na.compose_display_name(compact, halfwidth=True)
    return SynthesizedFields(
        number=number,
        name=name,
        display_name=display_name,
        display_name_halfwidth=display_name_halfwidth,
    )
