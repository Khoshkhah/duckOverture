"""Render an extract with roadstyle.

Overture's `class` values are OSM-derived (`residential`, `footway`, `secondary`, …), so roadstyle
resolves them directly — mapping Overture to roadstyle is a **rename**, not a translation table.
The only real work is unpacking three nested columns into the flat ones roadstyle reads for the
grade-separation sandwich:

    class        -> class               (kept under Overture's own name)
    road_flags   -> is_bridge / is_tunnel  (list of struct(values[], between[]))
    level_rules  -> level               (list of struct(value, between[]))
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

_DISPLAY_FLAGS = ("is_bridge", "is_tunnel", "is_indoor", "is_covered", "is_link",
                  "is_under_construction", "is_roundabout")

# Overture's nested columns -> the flat columns roadstyle reads.
SEGMENT_SQL = f"""
SELECT
    id,
    class                                                          AS "class",
    names.primary                                                  AS name,
    subclass,
    list_contains(flatten(list_transform(road_flags, x -> x.values)),
                  'is_bridge')                                     AS is_bridge,
    list_contains(flatten(list_transform(road_flags, x -> x.values)),
                  'is_tunnel')                                     AS is_tunnel,
    CAST(level_rules[1].value AS VARCHAR)                          AS level,
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

    # `segment` mode has no junctions to name — the connector columns only exist once the rows
    # are cut at them. Selecting them unconditionally broke the report in that mode, the same way
    # flat.turn_restriction did.
    flat_cols = {r[0] for r in con.execute("DESCRIBE flat.segment").fetchall()}
    parts = [
        # identity
        "f.segment_id", "f.piece_id",
        # what roadstyle styles by
        "f.\"class\"", "f.subclass", "f.name",
        # resolved scalars
        "f.maxspeed_kmh", "f.oneway", "f.direction", "f.surface", "f.width_m",
        "CASE WHEN f.oneway THEN 'one-way ' || f.direction END AS one_way",
        # `level` is Overture's own statement, NULL on 95% of pieces. The resolved elevation is
        # already in the panel as roadstyle's `lvl` (bridge -> >=1, tunnel -> <=-1, else level or
        # 0); a `grade` column of our own came out identical to it on all 8,980 rows, so it is not
        # worth printing the same number twice.
        "round(f.length_m, 1) AS length_m", "f.level",
        *(f"CASE WHEN f.{b} THEN true END AS {b}" for b in _DISPLAY_FLAGS[:4]),
        *(f"CASE WHEN f.{b} THEN true END AS {b}" for b in _DISPLAY_FLAGS[4:6]),
        # resolved access — the raw `access` field below is the evidence behind it
        "f.motor_access", "f.max_height_m", "f.max_weight_t",
        "CASE WHEN f.is_roundabout THEN true END AS is_roundabout",
        # nested columns, summarised to one readable value each
        # No connector COUNT: it was the parent segment's, so a 2.6 m piece with two junctions
        # reported 39, and it is pieces+1 on 92% of segments anyway. What a piece can actually say
        # is which two junctions are its own — from_connector_id / to_connector_id below, where a
        # synthetic `<segment_id>@<lr>` id marks a cut at an attribute change rather than a
        # junction.
        "len(s.prohibited_transitions) AS n_turn_restrictions_segment",
        "list_aggregate(list_transform(s.subclass_rules, x -> x.value"
        "     || coalesce(' [' || round(x.between[1], 3) || '-' || round(x.between[2], 3) || ']', ''))," 
        "     'string_agg', ' / ') AS subclass_rules",
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
    if {"from_connector_id", "to_connector_id"} <= flat_cols:
        parts += ["f.from_connector_id", "f.to_connector_id"]
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
    # f.geometry, NOT s.geometry: in split modes a piece is a SUBSTRING of the segment, and
    # drawing each piece with the whole segment's line stacks every piece on top of every other.
    # Söderledstunneln then rendered as a bridge end to end, because its 107 m bridge piece
    # (lvl +1) painted the full 1,576 m line over its own 1,469 m of tunnel.
    parts.append("ST_AsWKB(CASE WHEN f.direction = 'backward' THEN ST_Reverse(f.geometry)"
                 " ELSE f.geometry END) AS wkb")
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
SELECT piece_id, segment_id, "class", subclass, name, level,
       -- TRUE or NULL: the popup hides nulls, so a road lists only the flags that apply
       CASE WHEN is_bridge THEN true END AS is_bridge,
       CASE WHEN is_tunnel THEN true END AS is_tunnel,
       CASE WHEN is_indoor THEN true END AS is_indoor,
       maxspeed_kmh, oneway, surface, width_m, length_m, motor_access,
       ST_AsWKB(CASE WHEN direction = 'backward' THEN ST_Reverse(geometry)
                     ELSE geometry END) AS wkb
FROM flat.segment
"""


# The report's turn-restriction layer: one dot per via junction, every ban there spelled out in
# road names. The via connector is the only geometry a restriction has — its pieces are lines the
# road layer already draws — and flat.connector is where a synthetic `<segment_id>@<lr>` junction
# gets coordinates, so the join works for those too.
#
# A ban is the WHOLE manoeuvre — flat.turn_restriction is one row per restriction, so the piece
# chain is just from_piece + via_piece_ids + to_piece. Each ban is listed at EVERY junction it
# crosses (via_connector_ids), so a via-way ban is findable from either of its dots.
#
# `banned` is the compact sentence for the hover tooltip; `bans` is the same data as JSON —
# roads/pieces arrays (index 0 = from, last = banned target, middles = via legs) plus the mode
# scope and time window — for the panel's per-stage rendering. JSON, because the first version
# packed everything into arrow-joined strings and the annotations baked into the last road name
# came out unparseable.
_TURN_OVERLAY_SQL = """
WITH stage AS (   -- each ban unrolled to (owner, tr, position, piece)
    SELECT t.tr_id, t.from_piece_id AS owner, u.i AS idx, t.chain[u.i] AS piece_id
    FROM (SELECT tr_id, from_piece_id,
                 list_prepend(from_piece_id, coalesce(via_piece_ids, []::VARCHAR[]))
                     || [to_piece_id] AS chain
          FROM flat.turn_restriction) t,
         UNNEST(range(1, len(t.chain) + 1)) u(i)
),
named AS (        -- the chain again, as road names (piece id when the road is unnamed)
    SELECT s.tr_id, s.owner,
           list(coalesce(f.name, f."class", s.piece_id) ORDER BY s.idx) AS roads,
           list(s.piece_id ORDER BY s.idx)                              AS pieces
    FROM stage s LEFT JOIN flat.segment f ON f.piece_id = s.piece_id
    GROUP BY s.tr_id, s.owner
),
ban AS (
    SELECT t.tr_id, t.via_connector_ids, t.during,
           list_aggregate(t.mode, 'string_agg', ', ') AS modes,
           n.roads, n.pieces
    FROM flat.turn_restriction t
    JOIN named n ON n.tr_id = t.tr_id AND n.owner = t.from_piece_id
)
SELECT v.via AS via_connector_id,
       count(*)::INTEGER AS n_bans,
       string_agg(b.tr_id::VARCHAR, ' · ' ORDER BY b.tr_id) AS tr_ids,
       string_agg(array_to_string(b.roads, ' → ')
           || coalesce(' (' || b.modes || ')', '')
           || coalesce(' [' || b.during || ']', ''),
           '  ·  ' ORDER BY b.tr_id)                                       AS banned,
       to_json(list(struct_pack(tr_id := b.tr_id,
                                roads := b.roads, pieces := b.pieces,
                                vias  := b.via_connector_ids,
                                modes := b.modes, during := b.during)
           ORDER BY b.tr_id))                                              AS bans,
       ST_AsWKB(any_value(c.geometry)) AS wkb
FROM ban b, UNNEST(b.via_connector_ids) AS v(via)
JOIN flat.connector c ON c.connector_id = v.via
GROUP BY v.via
"""


def load_turn_restrictions(db: str | Path):
    """Turn bans as one point per via junction, or None when the extract has none.

    Needs `duckoverture flatten -m routing` (or edges) — the modes that build
    flat.turn_restriction and the flat.connector geometry it is located by.
    """
    import geopandas as gpd

    con = duckdb.connect(str(db), read_only=True)
    con.execute("INSTALL spatial; LOAD spatial;")
    try:
        if not _has_table(con, "flat", "turn_restriction"):
            return None
        df = con.execute(_TURN_OVERLAY_SQL).df()
    finally:
        con.close()
    return gpd.GeoDataFrame(df.drop(columns=["wkb"]),
                            geometry=gpd.GeoSeries.from_wkb(df.wkb.map(bytes)), crs=4326)


def _has_table(con, schema: str, table: str) -> bool:
    return bool(con.execute(
        "SELECT count(*) FROM duckdb_tables() "
        "WHERE schema_name = ? AND table_name = ?", [schema, table]).fetchone()[0])


def _has_flat(con) -> bool:
    return _has_table(con, "flat", "segment")


# `motor_access = 'no'` is the resolved answer to "can a car use this", class rule included, so a
# drivable filter is one predicate rather than a class list that would drift from flatten's.
_DRIVABLE = "motor_access <> 'no'"


def load_roads(db: str | Path, report: bool = False, raw: bool = False,
               drivable: bool = False):
    """Overture road segments as a GeoDataFrame with roadstyle-shaped columns.

    ``report=True`` keeps **every** Overture column as well, nested ones as JSON text, for the
    click-to-inspect side panel.
    """
    import geopandas as gpd

    con = duckdb.connect(str(db), read_only=True)
    con.execute("INSTALL spatial; LOAD spatial;")
    flat = not report and _has_flat(con)
    try:
        sql = _report_select(con, raw) if report else FLAT_SQL if flat else SEGMENT_SQL
        if drivable:
            if not (report or flat):
                raise SystemExit("--drivable needs flat.segment — run `duckoverture flatten` first")
            sql = f"SELECT * FROM ({sql}) WHERE {_DRIVABLE}"
        df = con.execute(sql).df()
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


# Clicking a turn-ban dot lists that junction's bans in the side panel, one clickable card each:
# a labelled line per stage (FROM / VIA / INTO), the road name in its trace colour, the piece id
# shortened beside it (full id on hover), and a small note line for U-turn / mode scope / time
# window. Clicking a card traces exactly that manoeuvre on the map — from-piece navy, via legs
# teal, banned target dark red (a U-turn ban is one piece — it reads red). The first ban traces
# automatically. Painting ALL bans at once was tried and read ambiguously the moment a junction
# had two: four coloured pieces and no way to pair them. Data comes from the dot's `bans` JSON.
# Lives here, not in roadstyle — piece_id is duckoverture's schema; only roadstyle's public JS
# API (rs:select / rsQuery / multi-set rsColor) plus its .rs-side-bd panel element are touched.
_TURN_CLICK_JS = """
<script>
(function(){
  // Dark trio on purpose: highsat's own fills are all bright (orange primary, yellow secondary,
  // cyan motorway, green tertiary) and hover/select take violet — the first amber/blue/red
  // attempt vanished against them (amber #ffa000 vs primary #FF9100 was invisible on the very
  // junction that motivated this). Dark navy/teal/red collide with nothing on the map.
  var TO="#b71c1c", VIA="#00695c", FROM="#0d47a1";
  function esc(s){ return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;")
                                   .replace(/\"/g,"&quot;"); }
  function short(id){ var i=id.indexOf("#");
    return id.slice(0,8)+"\\u2026"+(i>=0?id.slice(i):""); }
  // Paint the ban's two ENDS only — from blue, banned target red. Via legs are context, not
  // targets (painting them teal read as three bans' worth of pieces); they stay in the card's
  // via line and note instead. TEAL still colours the card's via text.
  function paint(b){
    var last=b.pieces.length-1;
    rsColor([[rsQuery(function(r){ return r.piece_id===b.pieces[last]; }), TO],
             [rsQuery(function(r){ return r.piece_id===b.pieces[0]; }), FROM]]);
  }
  document.addEventListener("rs:select", function(e){
    var d=e.detail||{};
    if(d.overlay!=="Turn restrictions"){ rsColor(null); return; }
    var raw=(d.properties||{}).bans, bans=[];
    try{ bans = typeof raw==="string" ? JSON.parse(raw||"[]") : (raw||[]); }catch(err){}
    bans=bans.filter(function(b){ return b.pieces && b.pieces.length>=2; });
    if(!bans.length){ rsColor(null); return; }
    var bd=document.querySelector(".rs-side-bd");
    if(!bd){ paint(bans[0]); return; }
    var box=document.createElement("div");
    box.innerHTML='<div style="margin-top:10px;color:#777">'+bans.length+
      (bans.length>1?" bans":" ban")+" at this junction \\u2014 click one to trace it</div>";
    bans.forEach(function(b){
      var row=document.createElement("div");
      row.style.cssText="cursor:pointer;padding:6px 8px;margin:5px 0;"+
        "border:1px solid #e3e3e3;border-radius:6px;line-height:1.4";
      var lbl='display:inline-block;width:36px;font-size:9px;color:#999;'+
              'text-transform:uppercase;letter-spacing:.4px';
      function roadHtml(color, road, piece){
        return '<span style="color:'+color+';font-weight:600">'+esc(road)+'</span>'+
          ' <span style="font:10px monospace;color:#999" title="'+esc(piece)+'">'+
          esc(short(piece))+'</span>';
      }
      var last=b.pieces.length-1;
      var html=(b.tr_id!=null
          ? '<div style="float:right;font:10px monospace;color:#bbb">tr '+esc(b.tr_id)+'</div>'
          : '')+
        '<div><span style="'+lbl+'">from</span>'+roadHtml(FROM,b.roads[0],b.pieces[0])+'</div>'+
        '<div><span style="'+lbl+'">via</span><span style="font:10px monospace;color:'+VIA+'">'+
          (b.vias||[]).map(function(v){
            return '<span title="'+esc(v)+'">'+esc(short(v))+'</span>';
          }).join(" \\u00b7 ")+'</span></div>'+
        '<div><span style="'+lbl+'">into</span>'+roadHtml(TO,b.roads[last],b.pieces[last])+'</div>';
      var notes=[];
      // U-turn comes in two shapes: reverse onto the SAME piece, or (dual carriageway) onto the
      // opposite one of the same road — the latter looked like nonsense ("Ringvägen → Ringvägen")
      // once cards stopped naming the via legs, so both get said out loud, with the leg's name.
      if(b.pieces[0]===b.pieces[last]) notes.push("U-turn");
      else if(b.roads[0]===b.roads[last]) notes.push("U-turn across the median");
      if(b.roads.length>2) notes.push("via "+b.roads.slice(1,-1).filter(function(r,i,a){
        return a.indexOf(r)===i; }).join(", "));
      if(b.modes)  notes.push(b.modes);
      if(b.during) notes.push(b.during);
      if(notes.length) html+='<div style="font-size:10px;color:#999;margin-top:2px">'+
        esc(notes.join(" \\u00b7 "))+"</div>";
      row.innerHTML=html;
      row.onclick=function(ev){ ev.stopPropagation();
        for(var c=box.firstChild; c; c=c.nextSibling) c.style.background="";
        row.style.background="#f3eefc";
        paint(b); };
      box.appendChild(row);
    });
    bd.appendChild(box);
    box.children[1].click();
  });
  document.addEventListener("rs:deselect", function(){ rsColor(null); });
})();
</script>"""


def render(db: str | Path, out: str | Path, palette: str = "highsat",
           backend: str = "web", report: bool = False, raw: bool = False,
           drivable: bool = False, tiles: bool = False, minzoom: bool = True,
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

    g = load_roads(db, report=report, raw=raw, drivable=drivable)
    if not report:
        import duckdb as _d
        _c = _d.connect(str(db), read_only=True)
        log("  source: flat.segment (dominant-rule values)" if _has_flat(_c)
            else "  source: transportation.segment (raw; run `duckoverture flatten` for corrected values)")
        _c.close()
    if drivable:
        log("  --drivable: motor_access <> 'no' (footways, steps, cycleways dropped)")
    if tiles:
        log("  tiles: PMTiles vector tiles embedded (--no-tiles for inline GeoJSON)")
    log(f"{len(g):,} road segments  |  oneway {int(g.oneway.sum()):,} · "
        f"bridges {g.is_bridge.fillna(False).sum():,} · "
        f"tunnels {g.is_tunnel.fillna(False).sum():,} · "
        f"levelled {g.level.notna().sum():,} · speed limits {g.maxspeed_kmh.notna().sum():,}")
    # roadstyle defaults these to "bridge"/"tunnel". We name them is_bridge/is_tunnel to match
    # flat.segment, so they have to be passed — without this the grade-separation sandwich silently
    # flattens and every tunnel draws on top of the road above it.
    # minzoom=True applies roadstyle's standard per-class zoom table (residential from z13,
    # footway from z15, …) — zoomed out, minor classes drop instead of smearing sub-pixel lines.
    # voyager_nolabels: the only street names on the map are roadstyle's own (arrow-grey,
    # class-prioritised) — with the labelled basemap, CARTO's black names showed through
    # wherever ours did not place.
    kw = dict(backend=backend, palette=palette, highway_col="class",
              bridge_col="is_bridge", tunnel_col="is_tunnel", layer_col="level",
              minzoom=minzoom, basemap="voyager_nolabels")
    if tiles:
        # PMTiles archive embedded in the page and served through addProtocol, so the file still
        # opens from disk. MapLibre then decodes only the tiles in view instead of tessellating
        # every vertex at every zoom — which is the whole cost on a 30k-vertex network.
        kw["tiles"] = True
    if report:
        # An explicit list, not road_popup="all". "all" sweeps up roadstyle's own post-hoc
        # properties, and `twoway` among them is a RENDERING flag — "has a reverse twin to offset
        # away from", which drives line-offset. Our rows are one per road, so it is always False
        # and sat next to `oneway: False` reading as a contradiction. A list also fixes the field
        # ORDER, which "all" leaves to column order.
        # `oneway` and `direction` stay in the properties for roadstyle and for anyone reading
        # the GeoJSON, but the panel shows `one_way` instead — present only when it means
        # something, rather than "oneway: False / direction: forward" on 88% of roads.
        hide = {"geometry", "oneway", "direction"}
        fields = [c for c in g.columns if c not in hide and not c.startswith("__rs")]
        order = ["segment_id", "piece_id", "class", "subclass", "name",
                 "motor_access", "one_way", "maxspeed_kmh",
                 "length_m", "level", "surface", "width_m",
                 "is_bridge", "is_tunnel", "is_indoor", "is_covered", "is_link",
                 "is_under_construction", "is_roundabout",
                 "max_height_m", "max_weight_t", "turn_bans_here",
                 "from_connector_id", "to_connector_id"]
        fields.sort(key=lambda c: (order.index(c) if c in order else len(order), c))
        # `lvl` is roadstyle's resolved elevation, added after we hand the data over, so it is not
        # in g.columns — name it anyway or the panel loses the only always-present grade number.
        fields.append("lvl")
        log(f"  report mode: {len(fields)} fields per road in the click panel")
        kw.update(road_popup=fields, popup_mode="panel", road_tooltip=["name", "class"])
        # Turn bans as their own toggleable layer. The roads only carry counts
        # (n_turn_restrictions_segment / turn_bans_here); the junction dot is where the ban is
        # readable as a sentence — click it for "Hornsgatan → Ringvägen".
        tr = load_turn_restrictions(db)
        if tr is not None and len(tr):
            kw["overlays"] = [rs.Overlay(
                tr, kind="circle", color="#d32f2f", radius=5, label="Turn restrictions",
                tooltip=["banned"],
                # No field rows at all: the injected click handler renders the ban cards (with
                # their tr ids) from rs:select's payload, so any panel field would duplicate
                # them. ["_"] rather than []: an empty popup makes the layer NON-interactive in
                # roadstyle, while "_"-prefixed fields are always skipped by its field filter —
                # interactive, zero rows.
                popup=["_"])]
            log(f"  turn restrictions: {int(tr.n_bans.sum())} bans at {len(tr)} junctions")
    else:
        # the default popup list names "bridge"/"tunnel", which no longer exist here
        kw.update(tooltip=["name", "class", "subclass", "maxspeed_kmh"],
                  road_popup=["name", "class", "subclass", "maxspeed_kmh", "oneway",
                              "is_bridge", "is_tunnel", "level", "length_m"])
    m = rs.render_edges(g, **kw)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    m.save(str(out))
    if kw.get("overlays"):
        # roadstyle has no custom-JS hook; append the turn-ban click handler before </body>
        html = out.read_text(encoding="utf-8")
        out.write_text(html.replace("</body>", _TURN_CLICK_JS + "</body>"), encoding="utf-8")
    log(f"wrote {out}")
    return out
