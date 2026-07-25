"""Render an extract with roadstyle.

Overture's `class` values are OSM-derived (`residential`, `footway`, `secondary`, …), so roadstyle
resolves them directly — mapping Overture to roadstyle is a **rename**, not a translation table.
The only real work is unpacking three nested columns into the flat ones roadstyle reads for the
grade-separation sandwich:

    road_flags   -> is_bridge / is_tunnel  (list of struct(values[], between[]))
    level_rules  -> layer               (list of struct(value, between[]))
    speed_limits -> maxspeed_kmh        (list of struct(max_speed struct(value, unit)))

**Prefers `flat.segment` when it exists.** That table resolves each ranged attribute from its
*dominant* rule, so a 1,639 m road whose first speed rule covers 115 m is labelled 60 km/h rather
than 40. Falling back to `transportation.segment` re-derives with the naive `[1]` shortcut and
says so in the log — run `duckoverture flatten` to fix it.

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
    list_contains(flatten(list_transform(road_flags, x -> x.values)),
                  'is_bridge')                                     AS is_bridge,
    list_contains(flatten(list_transform(road_flags, x -> x.values)),
                  'is_tunnel')                                     AS is_tunnel,
    CAST(level_rules[1].value AS VARCHAR)                          AS layer,
    speed_limits[1].max_speed.value                                AS maxspeed_kmh,
    {ONEWAY_EXPR}                                                  AS oneway,
    ST_AsWKB({GEOM_EXPR})                                          AS wkb
FROM transportation.segment
WHERE subtype = 'road'
"""


def _report_select(con, raw: bool = False) -> str:
    """Columns for the click-to-inspect panel.

    Flat by default: every field a scalar the panel can print, taken from ``flat.segment`` where
    each ranged attribute is already resolved from its dominant rule. Overture's nested columns are
    *summarised* — a count, or the values joined — rather than dumped as JSON, because a wall of
    ``[{"connector_id":"a0c1c021-…"}]`` is not a report.

    ``raw=True`` appends the untouched nested columns as JSON for when the evidence behind a
    resolved value is what you are after.
    """
    if not _has_flat(con):
        raise SystemExit("report needs flat.segment — run `duckoverture flatten <db>` first")

    parts = [
        # identity
        "f.segment_id", "f.piece_id",
        # what roadstyle styles by
        "f.highway", "f.subclass", "f.name",
        # resolved scalars
        "f.maxspeed_kmh", "f.oneway", "f.direction", "f.surface", "f.width_m",
        "round(f.length_m, 1) AS length_m", "f.level AS layer",
        "f.is_bridge", "f.is_tunnel",
        "f.is_link",
        # resolved access — the raw `access` field below is the evidence behind it
        "f.motor_access", "f.max_height_m", "f.max_weight_t", "f.is_roundabout",
        # nested columns, summarised to one readable value each
        "len(s.connectors) AS n_connectors",
        "len(s.prohibited_transitions) AS n_turn_restrictions_segment",
        "list_aggregate(list_transform(s.speed_limits, x -> x.max_speed.value::VARCHAR),"
        " 'string_agg', ' / ') AS all_speed_limits",
        "list_aggregate(list_transform(s.access_restrictions, x -> x.access_type"
        " || coalesce(' ' || x.\"when\".heading, '')), 'string_agg', ' / ') AS all_access",
        "list_aggregate(flatten(list_transform(list_filter(s.road_flags,"
        "     x -> x.between IS NULL OR (x.between[1] <= (f.start_lr + f.end_lr) / 2"
        "                            AND x.between[2] >= (f.start_lr + f.end_lr) / 2)),"
        "     x -> x.values)), 'string_agg', ' / ') AS flags",
        "list_aggregate(list_transform(s.routes, x -> x.\"ref\"), 'string_agg', ' / ') AS routes",
        "s.sources[1].dataset AS source_dataset",
        "s.\"version\" AS overture_version",
    ]
    # Renumbered turn bans exist only after `flatten -m edges`; the other modes have no such
    # table and the report must still render.
    if _has_table(con, "flat", "turn_restriction"):
        parts.append("(SELECT count(*) FROM flat.turn_restriction r "
                     "WHERE r.from_piece_id = f.piece_id) AS turn_bans_here")
    if raw:
        for name, dtype, *_ in con.execute("DESCRIBE transportation.segment").fetchall():
            if name == "geometry":
                continue
            nested = "STRUCT" in dtype.upper() or dtype.endswith("[]") or "MAP(" in dtype.upper()
            if nested:
                parts.append(f'to_json(s."{name}")::VARCHAR AS raw_{name}')
    parts.append("ST_AsWKB(CASE WHEN f.direction = 'backward' THEN ST_Reverse(s.geometry)"
                 " ELSE s.geometry END) AS wkb")
    return ("SELECT " + ", ".join(parts) +
            " FROM flat.segment f JOIN transportation.segment s ON s.id = f.segment_id")


def default_output(db: str | Path) -> Path:
    """Where a map goes when the caller does not say: ``viz/<stem>.html``, beside the data dir.

    Rendered maps are outputs, not data — keeping them out of ``data/`` means the two can be
    cleaned, gitignored and synced independently.
    """
    db = Path(db)
    root = db.parent.parent if db.parent.name == "data" else db.parent
    return root / "viz" / f"{db.stem}.html"


# flat.segment already holds the resolved scalars, with ranged attributes taken from their
# dominant rule — so when it exists the map uses corrected values instead of re-deriving them
# with the naive `[1]` shortcut.
FLAT_SQL = """
SELECT piece_id, segment_id, highway, subclass, name, level AS layer,
       is_bridge, is_tunnel,
       maxspeed_kmh, oneway, surface, width_m, length_m, motor_access,
       ST_AsWKB(CASE WHEN direction = 'backward' THEN ST_Reverse(geometry)
                     ELSE geometry END) AS wkb
FROM flat.segment
"""


def _has_table(con, schema: str, table: str) -> bool:
    return bool(con.execute(
        "SELECT count(*) FROM duckdb_tables() "
        "WHERE schema_name = ? AND table_name = ?", [schema, table]).fetchone()[0])


def _has_flat(con) -> bool:
    return _has_table(con, "flat", "segment")


def load_roads(db: str | Path, report: bool = False, raw: bool = False):
    """Overture road segments as a GeoDataFrame with roadstyle-shaped columns.

    ``report=True`` keeps **every** Overture column as well, nested ones as JSON text, for the
    click-to-inspect side panel.
    """
    import geopandas as gpd

    con = duckdb.connect(str(db), read_only=True)
    con.execute("INSTALL spatial; LOAD spatial;")
    flat = not report and _has_flat(con)
    try:
        df = con.execute(_report_select(con, raw) if report
                         else FLAT_SQL if flat else SEGMENT_SQL).df()
    except duckdb.CatalogException as e:
        # Only claim the segment table is missing when it actually is — this guard used to
        # attribute every catalog error to it, which sent me looking in the wrong place.
        if not _has_table(con, "transportation", "segment"):
            raise SystemExit(
                f"{db} has no transportation.segment table — build it with "
                f"`duckoverture build -t transportation ...`") from e
        raise
    finally:
        con.close()
    return gpd.GeoDataFrame(df.drop(columns=["wkb"]),
                            geometry=gpd.GeoSeries.from_wkb(df.wkb.map(bytes)), crs=4326)


def render(db: str | Path, out: str | Path, palette: str = "highsat",
           backend: str = "web", report: bool = False, raw: bool = False,
           log=print) -> Path:
    """Render the road network of an extract to a self-contained HTML map.

    ``report=True`` produces the **inspection** version: every Overture column travels with the
    map, and clicking a road docks a side panel showing all of them
    (roadstyle ``road_popup="all"`` + ``popup_mode="panel"``).
    """
    try:
        import roadstyle as rs
    except ImportError as e:                                    # pragma: no cover
        raise SystemExit('viz needs roadstyle: pip install "duckoverture[viz]"') from e

    g = load_roads(db, report=report, raw=raw)
    if not report:
        import duckdb as _d
        _c = _d.connect(str(db), read_only=True)
        log("  source: flat.segment (dominant-rule values)" if _has_flat(_c)
            else "  source: transportation.segment (raw; run `duckoverture flatten` for corrected values)")
        _c.close()
    log(f"{len(g):,} road segments  |  oneway {int(g.oneway.sum()):,} · "
        f"bridges {g.is_bridge.fillna(False).sum():,} · "
        f"tunnels {g.is_tunnel.fillna(False).sum():,} · "
        f"levelled {g.layer.notna().sum():,} · speed limits {g.maxspeed_kmh.notna().sum():,}")
    # roadstyle defaults these to "bridge"/"tunnel". We name them is_bridge/is_tunnel to match
    # flat.segment, so they have to be passed — without this the grade-separation sandwich silently
    # flattens and every tunnel draws on top of the road above it.
    kw = dict(backend=backend, palette=palette,
              bridge_col="is_bridge", tunnel_col="is_tunnel")
    if report:
        log(f"  report mode: {len(g.columns) - 1} fields per road in the click panel")
        kw.update(road_popup="all", popup_mode="panel", road_tooltip=["name", "class"])
    else:
        # the default popup list names "bridge"/"tunnel", which no longer exist here
        kw.update(tooltip=["name", "highway", "subclass", "maxspeed_kmh"],
                  road_popup=["name", "highway", "subclass", "maxspeed_kmh", "oneway",
                              "is_bridge", "is_tunnel", "layer", "length_m"])
    m = rs.render_edges(g, **kw)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    m.save(str(out))
    log(f"wrote {out}")
    return out
