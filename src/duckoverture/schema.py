"""The Overture type catalogue — theme, type, and where each lives on S3.

Overture publishes one Parquet dataset per (theme, type) under a monthly release prefix. The
output db mirrors that: schema per theme, table per type, so `transportation.segment` in our db is
the same thing as `theme=transportation/type=segment/` upstream. Nothing is renamed.
"""
from __future__ import annotations

from typing import NamedTuple

S3_ROOT = "s3://overturemaps-us-west-2/release"


class OvertureType(NamedTuple):
    theme: str
    type: str

    @property
    def table(self) -> str:
        return f"{self.theme}.{self.type}"

    def url(self, release: str) -> str:
        return f"{S3_ROOT}/{release}/theme={self.theme}/type={self.type}/*"


# The 15 types, as returned by overturemaps.core.get_all_overture_types() (release 2026-07-22.0).
TYPES: list[OvertureType] = [
    OvertureType("addresses", "address"),
    OvertureType("base", "bathymetry"),
    OvertureType("base", "infrastructure"),
    OvertureType("base", "land"),
    OvertureType("base", "land_cover"),
    OvertureType("base", "land_use"),
    OvertureType("base", "water"),
    OvertureType("buildings", "building"),
    OvertureType("buildings", "building_part"),
    OvertureType("divisions", "division"),
    OvertureType("divisions", "division_area"),
    OvertureType("divisions", "division_boundary"),
    OvertureType("places", "place"),
    OvertureType("transportation", "connector"),
    OvertureType("transportation", "segment"),
]

THEMES = sorted({t.theme for t in TYPES})


def resolve(names: list[str] | None) -> list[OvertureType]:
    """Select types by type name, theme name, or both. ``None`` means everything."""
    if not names:
        return list(TYPES)
    wanted = {n.strip() for n in names}
    out = [t for t in TYPES if t.type in wanted or t.theme in wanted]
    unknown = wanted - {t.type for t in TYPES} - set(THEMES)
    if unknown:
        raise ValueError(f"unknown type/theme: {sorted(unknown)}. "
                         f"themes={THEMES} types={[t.type for t in TYPES]}")
    return out
