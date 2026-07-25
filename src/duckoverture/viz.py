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

# Overture has no `oneway` column. One-way streets are expressed as a heading-scoped denial in
# `access_restrictions`: "denied when heading = backward" means traffic runs forward only.
#
# Mode matters. A denial scoped to [foot, bicycle] is a footway rule, not a one-way street, so only
# denials that apply to everything (mode IS NULL) or name a motorised mode count. On Södermalm that
# is 644 backward-denials (all modes) and 2 forward-denials (motor_vehicle).
_MOTOR = "['motor_vehicle','car','truck','hgv','bus','motorcycle','taxi','hov']"
_DENIED = (f"a.access_type = 'denied' AND a.\"when\".heading = %s "
           f"AND (a.\"when\".mode IS NULL OR list_has_any(a.\"when\".mode, {_MOTOR}))")


def _heading_denied(heading: str) -> str:
    """EXISTS predicate: this segment denies travel in `heading` for motor traffic."""
    return (f"EXISTS (SELECT 1 FROM unnest(access_restrictions) t(a) "
            f"WHERE {_DENIED % repr(heading)})")


ONEWAY_EXPR = f"({_heading_denied('backward')} OR {_heading_denied('forward')})"

# roadstyle draws the direction chevrons ALONG the geometry, so a segment whose forward travel is
# denied must be flipped or its arrows point into oncoming traffic.
GEOM_EXPR = (f"CASE WHEN {_heading_denied('forward')} AND NOT {_heading_denied('backward')} "
             f"THEN ST_Reverse(geometry) ELSE geometry END")

# Overture's nested columns -> the flat columns roadstyle reads.
SEGMENT_SQL = f"""
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
    {ONEWAY_EXPR}                                                  AS oneway,
    ST_AsWKB({GEOM_EXPR})                                          AS wkb
FROM transportation.segment
WHERE subtype = 'road'
"""


def _report_select(con) -> str:
    """Every `transportation.segment` column, with nested ones rendered as readable JSON.

    The side panel shows raw values, and a Python ``dict``/``list`` repr of Overture's structs is
    unreadable. ``to_json`` turns each into text the panel can display verbatim. Nested columns are
    detected from the live schema rather than hardcoded, so anything Overture adds still appears.
    """
    cols = con.execute("DESCRIBE transportation.segment").fetchall()
    parts = []
    for name, dtype, *_ in cols:
        if name == "geometry":
            continue
        nested = "STRUCT" in dtype.upper() or dtype.endswith("[]") or "MAP(" in dtype.upper()
        q = f'"{name}"'
        parts.append(f"to_json({q})::VARCHAR AS {q}" if nested else f"{q}")
    # the flat columns roadstyle styles by — derived, so they must not collide with originals
    parts += [
        'class AS highway',
        'names.primary AS name',
        "CASE WHEN list_contains(flatten(list_transform(road_flags, x -> x.values)), 'is_bridge')"
        " THEN 'yes' END AS bridge",
        "CASE WHEN list_contains(flatten(list_transform(road_flags, x -> x.values)), 'is_tunnel')"
        " THEN 'yes' END AS tunnel",
        "CAST(level_rules[1].value AS VARCHAR) AS layer",
        "speed_limits[1].max_speed.value AS maxspeed_kmh",
        f"{ONEWAY_EXPR} AS oneway",
        f"ST_AsWKB({GEOM_EXPR}) AS wkb",
    ]
    return ("SELECT " + ", ".join(parts) +
            " FROM transportation.segment WHERE subtype = 'road'")


def default_output(db: str | Path) -> Path:
    """Where a map goes when the caller does not say: ``viz/<stem>.html``, beside the data dir.

    Rendered maps are outputs, not data — keeping them out of ``data/`` means the two can be
    cleaned, gitignored and synced independently.
    """
    db = Path(db)
    root = db.parent.parent if db.parent.name == "data" else db.parent
    return root / "viz" / f"{db.stem}.html"


def load_roads(db: str | Path, report: bool = False):
    """Overture road segments as a GeoDataFrame with roadstyle-shaped columns.

    ``report=True`` keeps **every** Overture column as well, nested ones as JSON text, for the
    click-to-inspect side panel.
    """
    import geopandas as gpd

    con = duckdb.connect(str(db), read_only=True)
    con.execute("INSTALL spatial; LOAD spatial;")
    try:
        df = con.execute(_report_select(con) if report else SEGMENT_SQL).df()
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
           backend: str = "web", report: bool = False, log=print) -> Path:
    """Render the road network of an extract to a self-contained HTML map.

    ``report=True`` produces the **inspection** version: every Overture column travels with the
    map, and clicking a road docks a side panel showing all of them
    (roadstyle ``road_popup="all"`` + ``popup_mode="panel"``).
    """
    try:
        import roadstyle as rs
    except ImportError as e:                                    # pragma: no cover
        raise SystemExit('viz needs roadstyle: pip install "duckoverture[viz]"') from e

    g = load_roads(db, report=report)
    log(f"{len(g):,} road segments  |  oneway {int(g.oneway.sum()):,} · "
        f"bridges {g.bridge.notna().sum():,} · tunnels {g.tunnel.notna().sum():,} · "
        f"levelled {g.layer.notna().sum():,} · speed limits {g.maxspeed_kmh.notna().sum():,}")
    kw = dict(backend=backend, palette=palette)
    if report:
        log(f"  report mode: {len(g.columns) - 1} fields per road in the click panel")
        kw.update(road_popup="all", popup_mode="panel", road_tooltip=["name", "class"])
    else:
        kw.update(tooltip=["name", "highway", "subclass", "maxspeed_kmh"])
    m = rs.render_edges(g, **kw)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    m.save(str(out))
    log(f"wrote {out}")
    return out
