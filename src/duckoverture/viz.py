"""Render an extract with roadstyle.

Overture's `class` values are OSM-derived (`residential`, `footway`, `secondary`, …), so roadstyle
resolves them directly — mapping Overture to roadstyle is a **rename**, not a translation table.
The only real work is unpacking three nested columns into the flat ones roadstyle reads for the
grade-separation sandwich:

    road_flags   -> bridge / tunnel     (list of struct(values[], between[]))
    level_rules  -> layer               (list of struct(value, between[]))
    speed_limits -> maxspeed_kmh        (list of struct(max_speed struct(value, unit)))

Sub-range geometry (`between`) is collapsed to the segment's first rule: roadstyle styles a whole
feature, so a segment that is a bridge for part of its length is drawn as a bridge. Splitting at
sub-range boundaries belongs in the flatten stage, not here.

Needs the `viz` extra: ``pip install "duckoverture[viz]"``.
"""
from __future__ import annotations

from pathlib import Path

import duckdb

# Overture's nested columns -> the flat columns roadstyle reads.
SEGMENT_SQL = """
SELECT
    id,
    class                                                          AS highway,
    names.primary                                                  AS name,
    subclass,
    CASE WHEN list_contains(
         flatten(list_transform(road_flags, x -> x.values)), 'is_bridge') THEN 'yes' END AS bridge,
    CASE WHEN list_contains(
         flatten(list_transform(road_flags, x -> x.values)), 'is_tunnel') THEN 'yes' END AS tunnel,
    CAST(level_rules[1].value AS VARCHAR)                          AS layer,
    speed_limits[1].max_speed.value                                AS maxspeed_kmh,
    ST_AsWKB(geometry)                                             AS wkb
FROM transportation.segment
WHERE subtype = 'road'
"""


def load_roads(db: str | Path):
    """Overture road segments as a GeoDataFrame with roadstyle-shaped columns."""
    import geopandas as gpd

    con = duckdb.connect(str(db), read_only=True)
    con.execute("INSTALL spatial; LOAD spatial;")
    try:
        df = con.execute(SEGMENT_SQL).df()
    except duckdb.CatalogException as e:
        raise SystemExit(
            f"{db} has no transportation.segment table — build it with "
            f"`duckoverture build -t transportation ...`"
        ) from e
    finally:
        con.close()
    return gpd.GeoDataFrame(df.drop(columns=["wkb"]),
                            geometry=gpd.GeoSeries.from_wkb(df.wkb.map(bytes)), crs=4326)


def render(db: str | Path, out: str | Path, palette: str = "highsat",
           backend: str = "web", log=print) -> Path:
    """Render the road network of an extract to a self-contained HTML map."""
    try:
        import roadstyle as rs
    except ImportError as e:                                    # pragma: no cover
        raise SystemExit('viz needs roadstyle: pip install "duckoverture[viz]"') from e

    g = load_roads(db)
    log(f"{len(g):,} road segments  |  bridges {g.bridge.notna().sum():,} · "
        f"tunnels {g.tunnel.notna().sum():,} · levelled {g.layer.notna().sum():,} · "
        f"speed limits {g.maxspeed_kmh.notna().sum():,}")
    m = rs.render_edges(g, backend=backend, palette=palette,
                        tooltip=["name", "highway", "subclass", "maxspeed_kmh"])
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    m.save(str(out))
    log(f"wrote {out}")
    return out
