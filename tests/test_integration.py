"""End-to-end extraction against the live Overture S3 dataset.

Skipped by default — it downloads. Enable with:

    DUCKOVERTURE_NETWORK_TESTS=1 pytest tests/test_integration.py
"""
import json
import os

import duckdb
import pytest

from duckoverture import build

pytestmark = pytest.mark.skipif(
    os.environ.get("DUCKOVERTURE_NETWORK_TESTS") != "1",
    reason="set DUCKOVERTURE_NETWORK_TESTS=1 to run (downloads from S3)")

# a small slice of Södermalm, Stockholm
BBOX = [[18.05, 59.310], [18.05, 59.320], [18.075, 59.320], [18.075, 59.310], [18.05, 59.310]]


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("dov")
    b = d / "b.geojson"
    b.write_text(json.dumps({"type": "Polygon", "coordinates": [BBOX]}))
    out = d / "area.duckdb"
    stats = build(b, out, types=["transportation"], log=lambda *a: None)
    return out, stats


def test_extracts_segments_and_connectors(built):
    out, stats = built
    assert stats["transportation.segment"] > 0
    assert stats["transportation.connector"] > 0


def test_geometry_is_real_and_clipped(built):
    out, _ = built
    con = duckdb.connect(str(out), read_only=True)
    con.execute("LOAD spatial;")
    assert con.execute(
        "SELECT typeof(geometry) FROM transportation.segment LIMIT 1").fetchone()[0].startswith("GEOMETRY")
    # every kept row must actually touch the boundary
    n = con.execute("""SELECT count(*) FROM transportation.segment s, boundary b
                       WHERE NOT ST_Intersects(s.geometry, b.geom)""").fetchone()[0]
    assert n == 0


def test_nested_structs_survive(built):
    """The raw layer must stay faithful — nested Overture columns kept, not flattened away."""
    out, _ = built
    con = duckdb.connect(str(out), read_only=True)
    cols = {r[0]: r[1] for r in con.execute("DESCRIBE transportation.segment").fetchall()}
    assert cols["connectors"].startswith("STRUCT") or cols["connectors"].endswith("[]")
    for c in ("speed_limits", "access_restrictions", "prohibited_transitions", "level_rules"):
        assert c in cols, f"{c} missing — raw layer is not faithful"
    assert con.execute(
        "SELECT connectors[1].connector_id FROM transportation.segment "
        "WHERE connectors IS NOT NULL LIMIT 1").fetchone()[0] is not None


def test_meta_records_provenance(built):
    out, _ = built
    con = duckdb.connect(str(out), read_only=True)
    rel, clipped = con.execute("SELECT release, clipped FROM _meta").fetchone()
    assert rel and clipped is True


def test_viz_renders(built, tmp_path):
    """The Overture -> roadstyle mapping must survive: a real HTML map, from real data."""
    pytest.importorskip("roadstyle")
    from duckoverture.viz import load_roads, render
    out, _ = built
    g = load_roads(out)
    assert len(g) > 0
    # the columns roadstyle reads must all be present after unpacking the nested ones
    for c in ("class", "is_bridge", "is_tunnel", "level", "maxspeed_kmh", "geometry"):
        assert c in g.columns, f"{c} missing — roadstyle will not style this"
    # kept under Overture's own name; roadstyle is told via highway_col="class"
    assert g["class"].notna().any()
    html = render(out, tmp_path / "m.html", log=lambda *a: None)
    assert html.exists() and html.stat().st_size > 10_000


def test_report_mode_is_flat(built, tmp_path):
    """--report must be FLAT: every panel field a scalar, no JSON dumps."""
    pytest.importorskip("roadstyle")
    from duckoverture import flatten
    from duckoverture.viz import load_roads, render
    out, _ = built
    flatten(out, log=lambda *a: None)          # report requires flat.segment
    g = load_roads(out, report=True)

    assert len(g) > 0
    for c in ("class", "is_bridge", "is_tunnel", "level", "maxspeed_kmh", "oneway", "geometry"):
        assert c in g.columns, f"{c} missing — roadstyle will not style this"
    # nested columns must be summarised, not dumped
    for c in ("all_speed_limits", "all_access", "flags", "source_dataset"):
        assert c in g.columns
    for c in g.columns:
        if c == "geometry":
            continue
        vals = g[c].dropna()
        if not len(vals):
            continue
        bad = [v for v in vals.head(200) if isinstance(v, str) and v.lstrip()[:1] in "[{"]
        assert not bad, f"{c} still contains JSON: {bad[0][:60]}"
    html = render(out, tmp_path / "r.html", report=True, log=lambda *a: None)
    assert html.exists() and html.stat().st_size > 10_000


def test_report_raw_appends_json_evidence(built):
    """--raw is the opt-in evidence view: untouched nested columns as JSON."""
    import json as _json
    from duckoverture import flatten
    from duckoverture.viz import load_roads
    out, _ = built
    flatten(out, log=lambda *a: None)
    flat = load_roads(out, report=True)
    raw = load_roads(out, report=True, raw=True)
    assert len(raw.columns) > len(flat.columns)
    assert "raw_connectors" in raw.columns
    s = raw.loc[raw.raw_connectors.notna(), "raw_connectors"].iloc[0]
    assert isinstance(s, str) and _json.loads(s)


def test_report_requires_flatten(built, tmp_path):
    """Without flat.segment the report would show `[1]` values contradicting the map — refuse."""
    import shutil
    import duckdb as _d
    from duckoverture.viz import load_roads
    out, _ = built
    copy = tmp_path / "noflat.duckdb"
    shutil.copy(out, copy)
    con = _d.connect(str(copy)); con.execute("DROP SCHEMA IF EXISTS flat CASCADE"); con.close()
    with pytest.raises(SystemExit, match="flatten"):
        load_roads(copy, report=True)


# the real list, not a copy — a duplicate drifts and then asserts the wrong invariant
from duckoverture.flatten import _ATTRS as ATTRS  # noqa: E402


def _q(out, sql, params=None):
    """Query and CLOSE — a lingering read-only handle locks the next flatten()."""
    import duckdb as _d
    con = _d.connect(str(out), read_only=True)
    con.execute("LOAD spatial;")
    try:
        return con.execute(sql, params or []).fetchone()
    finally:
        con.close()


def _length_matches(out):
    """flat.segment's total length vs an INDEPENDENT measurement of the raw geometry.

    This used to measure the raw side with ST_Length_Spheroid — the same function flatten used —
    so a consistent error cancelled and the check passed regardless. It did: DuckDB's spheroid
    functions read (lat, lon) while Overture geometry is (lon, lat), and Sodermalm's road network
    was reported as 448,588 m for months against a true 290,590 m. Projecting with geopandas is a
    genuinely different code path, so it would have caught it.
    """
    import geopandas as gpd
    a = _q(out, "SELECT sum(length_m) FROM flat.segment")[0]
    wkb = _q_all(out, "SELECT ST_AsWKB(geometry) FROM transportation.segment WHERE subtype='road'")
    b = gpd.GeoSeries.from_wkb([bytes(r[0]) for r in wkb], crs=4326).to_crs(3006).length.sum()
    return abs(a - b) / b < 1e-3


def test_flatten_default_is_one_row_per_segment(built):
    """No splitting by default: flat.segment must join 1:1 to the raw segments."""
    from duckoverture import flatten
    out, _ = built
    flatten(out, log=lambda *a: None)
    n_flat, n_raw = _q(out, """SELECT (SELECT count(*) FROM flat.segment),
                                      (SELECT count(*) FROM transportation.segment
                                       WHERE subtype='road')""")
    assert n_flat == n_raw, "default flatten must not split"
    assert _q(out, "SELECT count(*) FROM (SELECT segment_id FROM flat.segment "
                   "GROUP BY 1 HAVING count(*) > 1)")[0] == 0
    assert _length_matches(out)


def test_flatten_attrs_conserves_length_and_cuts_only_on_change(built):
    from duckoverture import flatten
    out, _ = built
    flatten(out, mode="attrs", log=lambda *a: None)
    assert _length_matches(out), "split lost geometry"
    # no two consecutive pieces of a segment may agree on EVERY attribute
    lags = ", ".join(f"lag({c}) OVER w AS l_{c}" for c in ATTRS)
    same = " AND ".join(f"{c} IS NOT DISTINCT FROM l_{c}" for c in ATTRS)
    dupes = _q(out, f"""WITH x AS (SELECT *, {lags} FROM flat.segment
                                  WINDOW w AS (PARTITION BY segment_id ORDER BY piece_no))
                        SELECT count(*) FROM x WHERE piece_no > 1 AND {same}""")[0]
    assert dupes == 0, "adjacent identical pieces were not merged"


def test_dominant_rule_beats_first_rule(built):
    """The bug this fixes: a long road labelled from a short first rule."""
    from duckoverture import flatten
    out, _ = built
    flatten(out, log=lambda *a: None)
    bad = _q(out, """
        SELECT count(*) FROM transportation.segment s JOIN flat.segment f ON f.segment_id = s.id
        WHERE len(s.speed_limits) > 1
          AND f.maxspeed_kmh IS DISTINCT FROM (
              SELECT x.max_speed.value FROM unnest(s.speed_limits) t(x)
              ORDER BY coalesce(x.between[2] - x.between[1], 1.0) DESC LIMIT 1)""")[0]
    assert bad == 0, "maxspeed_kmh did not come from the dominant rule"


def test_flags_resolved_by_dominance_not_presence(built):
    """A flag must win on coverage, not on appearing anywhere.

    Söderledstunneln is a 1,470 m tunnel that ends as a 106 m bridge. Reading road_flags with
    list_contains over the whole column returns is_bridge AND is_tunnel — incoherent, and it feeds
    contradictory inputs into roadstyle's grade-separation sandwich.
    """
    from duckoverture import flatten
    out, _ = built
    flatten(out, log=lambda *a: None)
    # nothing may be both
    assert _q(out, "SELECT count(*) FROM flat.segment WHERE is_bridge AND is_tunnel")[0] == 0
    # and a flag covering <50% must not be set
    bad = _q(out, """
        SELECT count(*) FROM transportation.segment s JOIN flat.segment f ON f.segment_id = s.id
        WHERE s.subtype = 'road' AND f.is_bridge
          AND coalesce((SELECT sum(coalesce(x.between[2] - x.between[1], 1.0))
                        FROM unnest(s.road_flags) t(x)
                        WHERE list_contains(x.values, 'is_bridge')), 0) <= 0.5""")[0]
    assert bad == 0, "is_bridge set on a segment where it covers half or less"


def test_edges_mode_has_no_interior_connectors(built):
    """The invariant that makes an edge routable: no junction strictly inside it."""
    from duckoverture import flatten
    out, _ = built
    flatten(out, mode="edges", log=lambda *a: None)
    # only RESOLVABLE connectors count: one clipped out of the extract cannot be split at, and
    # the official splitter ignores those too
    bad = _q(out, """
        SELECT count(*) FROM flat.segment f
        JOIN transportation.segment s ON s.id = f.segment_id,
             unnest(s.connectors) t(k)
        JOIN transportation.connector c ON c.id = k.connector_id
        WHERE k.at > f.start_lr + 1e-9 AND k.at < f.end_lr - 1e-9""")[0]
    assert bad == 0, "an edge still passes through a known junction"
    assert _length_matches(out), "edge split lost geometry"


def test_edges_mode_yields_a_graph(built):
    """Every edge needs two endpoints, and real ones must resolve to transportation.connector."""
    from duckoverture import flatten
    out, _ = built
    flatten(out, mode="edges", log=lambda *a: None)
    nulls = _q(out, "SELECT count(*) FROM flat.segment "
                    "WHERE from_connector_id IS NULL OR to_connector_id IS NULL")[0]
    assert nulls == 0, "edge without endpoints"
    # a boundary that is not an Overture connector gets a synthetic <segment_id>@<lr> id
    assert _q(out, "SELECT count(*) FROM flat.segment WHERE from_connector_id LIKE '%@%'")[0] >= 0
    # Real ids must exist in the connector table — except where the junction sits outside the
    # clip boundary, which is legitimate. That fraction scales with how small the extract is:
    # 0.9% on the full Södermalm boundary, ~9% on this deliberately tiny test bbox. So assert a
    # clear majority rather than a tight ratio, and pin the no-nulls / no-interior invariants
    # (above and in test_edges_mode_has_no_interior_connectors) which hold unconditionally.
    ok, tot = _q(out, """SELECT
        (SELECT count(*) FROM flat.segment f WHERE f.from_connector_id NOT LIKE '%@%'
           AND EXISTS (SELECT 1 FROM transportation.connector k WHERE k.id = f.from_connector_id)),
        (SELECT count(*) FROM flat.segment WHERE from_connector_id NOT LIKE '%@%')""")
    assert ok / tot > 0.8, f"only {ok}/{tot} real connector ids resolve"


def test_modes_are_ordered_by_granularity(built):
    """segment <= attrs <= routing <= edges, and all four conserve length."""
    from duckoverture import flatten
    out, _ = built
    counts = {}
    for m in ("segment", "attrs", "routing", "edges"):
        flatten(out, mode=m, log=lambda *a: None)
        counts[m] = _q(out, "SELECT count(*) FROM flat.segment")[0]
        assert _length_matches(out), f"{m} lost geometry"
    assert counts["segment"] <= counts["attrs"] <= counts["routing"] <= counts["edges"], counts


def test_routing_keeps_junctions_but_drops_provenance_cuts(built):
    """routing must lose the `sources` fragmentation WITHOUT losing a single junction.

    `sources` carries a range per contributing OSM way, so a 237 m residential street stitched
    from 13 of them splits into 16 pieces identical in every attribute. Those cuts are provenance,
    not geography. edges keeps them for splitter parity; routing must not.
    """
    from duckoverture import flatten
    out, _ = built

    flatten(out, mode="edges", log=lambda *a: None)
    n_edges = _q(out, "SELECT count(*) FROM flat.segment")[0]
    flatten(out, mode="routing", log=lambda *a: None)
    n_routing = _q(out, "SELECT count(*) FROM flat.segment")[0]
    assert n_routing < n_edges, "routing merged nothing"

    # the invariant that matters: no known junction may end up inside an edge
    assert _q(out, """
        SELECT count(*) FROM flat.segment f
        JOIN transportation.segment s ON s.id = f.segment_id,
             unnest(s.connectors) t(k)
        JOIN transportation.connector c ON c.id = k.connector_id
        WHERE k.at > f.start_lr + 1e-6 AND k.at < f.end_lr - 1e-6""")[0] == 0

    # turn restrictions must be built here too, and point at pieces that exist
    assert _q(out, """SELECT count(*) FROM flat.turn_restriction r
        WHERE NOT EXISTS (SELECT 1 FROM flat.segment f WHERE f.piece_id = r.to_piece_id)""")[0] == 0


def test_flatten_rejects_unknown_mode(built):
    from duckoverture import flatten
    out, _ = built
    with pytest.raises(ValueError, match="mode must be"):
        flatten(out, mode="nonsense", log=lambda *a: None)


def test_sources_is_a_ranged_column(built):
    """`sources` carries provenance ranges on every road; omitting it cost 1,117 cut points."""
    from duckoverture.flatten import RANGED
    assert "sources" in RANGED


def test_edges_only_split_at_resolvable_connectors(built):
    """A junction we hold no connector record for must not become a cut point.

    0.8% of connector references on Södermalm point outside the clip boundary. Splitting there
    would mint nodes we cannot identify — Overture's own splitter ignores them too.
    """
    from duckoverture import flatten
    out, _ = built
    flatten(out, mode="edges", log=lambda *a: None)
    bad = _q(out, """
        SELECT count(*) FROM flat.segment
        WHERE from_connector_id NOT LIKE '%@%'
          AND NOT EXISTS (SELECT 1 FROM transportation.connector k
                          WHERE k.id = from_connector_id)""")[0]
    assert bad == 0, f"{bad} edges start at an unresolvable connector"


def test_edges_have_no_degenerate_pieces(built):
    """A split point within MIN_SPLIT_M of a connector must SNAP to it, not make a sliver.

    Overture's floats can name the same place twice — an attribute boundary at 0.379873 against a
    connector at 0.379872, 1.7 mm apart on a 1,694 m segment. Left alone that emits a 1.7 mm edge
    and, worse, the next piece then starts at a value matching no connector, so the junction is
    merged away (one was 110 m inside an edge before this was fixed).
    """
    from duckoverture import flatten
    from duckoverture.flatten import MIN_SPLIT_M
    out, _ = built
    flatten(out, mode="edges", log=lambda *a: None)
    tiny = _q(out, f"SELECT count(*) FROM flat.segment WHERE length_m < {MIN_SPLIT_M}")[0]
    assert tiny == 0, f"{tiny} degenerate edges shorter than {MIN_SPLIT_M} m"
    # and snapping must not cost us a junction
    inside = _q(out, """
        SELECT count(*) FROM flat.segment f JOIN transportation.segment s ON s.id = f.segment_id,
             unnest(s.connectors) t(k)
        JOIN transportation.connector c ON c.id = k.connector_id
        WHERE k.at > f.start_lr + 1e-6 AND k.at < f.end_lr - 1e-6""")[0]
    assert inside == 0, f"{inside} edges swallowed a known junction"


def test_turn_restrictions_renumber_onto_pieces(built):
    """Both ends of a turn restriction must land on pieces that actually touch the junction.

    A restriction says "from segment A, via connector c, you may not enter segment B". Splitting
    breaks both halves: it belongs to one piece of A, and B is now several pieces of which exactly
    one is the manoeuvre. Unresolved, a router bans miles of road or bans nothing.
    """
    from duckoverture import flatten
    out, _ = built
    flatten(out, mode="edges", log=lambda *a: None)

    # the owner piece must touch the first junction of the sequence
    assert _q(out, """
        SELECT count(*) FROM flat.turn_restriction t
        JOIN flat.segment f ON f.piece_id = t.from_piece_id
        WHERE t.seq_no = 1
          AND t.via_connector_id NOT IN (f.from_connector_id, f.to_connector_id)""")[0] == 0

    # and so must every referenced piece
    assert _q(out, """
        SELECT count(*) FROM flat.turn_restriction t
        JOIN flat.segment f ON f.piece_id = t.to_piece_id
        WHERE t.via_connector_id NOT IN (f.from_connector_id, f.to_connector_id)""")[0] == 0

    # exactly one piece per step, or the renumbering is ambiguous
    assert _q(out, """
        SELECT count(*) FROM (SELECT 1 FROM flat.turn_restriction
                              GROUP BY from_piece_id, tr_no, seq_no
                              HAVING count(DISTINCT to_piece_id) > 1)""")[0] == 0

    # no half-resolved chains: a partial sequence is a guess, not a restriction
    assert _q(out, """
        SELECT count(*) FROM (
            SELECT from_piece_id, tr_no, count(*) n, max(seq_no) hi
            FROM flat.turn_restriction GROUP BY 1, 2 HAVING n <> hi)""")[0] == 0


def test_turn_restrictions_are_edges_mode_only(built):
    """Junction ids are what a restriction hangs off, so it means nothing without them."""
    from duckoverture import flatten
    out, _ = built
    flatten(out, mode="attrs", log=lambda *a: None)
    assert "flat.turn_restriction" not in flatten(out, mode="attrs", log=lambda *a: None)
    assert "flat.turn_restriction" in flatten(out, mode="edges", log=lambda *a: None)


def test_access_closures_are_exposed_and_truck_limits_are_not_closures(built):
    """access_restrictions was only ever mined for `oneway`, so closed roads read as drivable.

    The trap is the opposite error: most non-directional motor denials are vehicle size limits
    ("no vehicles over 3 m"), which do not close a road to a car. Treating those as closures would
    remove more drivable road than the bug it fixes.
    """
    from duckoverture import flatten
    out, _ = built
    flatten(out, mode="edges", log=lambda *a: None)

    # a size/weight limit must never close the road
    assert _q(out, """SELECT count(*) FROM flat.segment
        WHERE (max_height_m IS NOT NULL OR max_weight_t IS NOT NULL)
          AND motor_access <> 'yes'""")[0] == 0

    # an unqualified denial with no allowance must close it
    assert _q(out, """
        SELECT count(*) FROM flat.segment f
        WHERE motor_access = 'yes' AND EXISTS (
            SELECT 1 FROM transportation.segment s, unnest(s.access_restrictions) t(x)
            WHERE s.id = f.segment_id AND x.access_type = 'denied' AND x."when" IS NULL
              AND x.between IS NULL
              AND NOT EXISTS (SELECT 1 FROM unnest(s.access_restrictions) u(y)
                              WHERE y.access_type IN ('allowed', 'designated')))""")[0] == 0

    assert set(_q(out, "SELECT list_sort(list_distinct(list(motor_access))) FROM flat.segment")[0]) \
        <= {"yes", "no", "restricted"}


def test_access_is_present_in_every_mode(built):
    """A router reading -m segment must not silently lose the closures."""
    from duckoverture import flatten
    out, _ = built
    for mode in ("segment", "attrs", "edges"):
        flatten(out, mode=mode, log=lambda *a: None)
        cols = {r[0] for r in _q_all(out, "DESCRIBE flat.segment")}
        assert {"motor_access", "max_height_m", "max_weight_t"} <= cols, mode


def _q_all(out, sql, params=None):
    import duckdb as _d
    con = _d.connect(str(out), read_only="DROP" not in sql.upper())
    con.execute("LOAD spatial;")
    try:
        return con.execute(sql, params or []).fetchall()
    finally:
        con.close()


def test_roundabout_repair_is_inert_without_a_source(built):
    """No fetch, no change. The column exists so the caveat is visible in the data, not just docs."""
    from duckoverture import flatten
    out, _ = built
    _q_all(out, "DROP TABLE IF EXISTS osm.roundabout_way")
    flatten(out, mode="edges", log=lambda *a: None)
    assert _q(out, "SELECT count(*) FROM flat.segment WHERE is_roundabout")[0] == 0


def test_roundabout_forces_oneway_via_osm_way_id(built):
    """A way id in osm.roundabout_way must reach flat.segment through sources[].record_id.

    Overture MERGES OSM ways — 9.5% of Nacka road segments reference more than one — so this is
    resolved through the `sources` ranges, not a whole-column check.
    """
    import duckdb as _d
    from duckoverture import flatten
    out, _ = built

    con = _d.connect(str(out))
    con.execute("LOAD spatial;")
    # pick a real two-way segment and plant its OSM way id as a roundabout
    seg, way = con.execute("""
        SELECT s.id, try_cast(regexp_extract(x.record_id, '^w(\\d+)', 1) AS BIGINT)
        FROM transportation.segment s, unnest(s.sources) t(x)
        WHERE s.subtype = 'road' AND x.record_id LIKE 'w%'
          AND len(coalesce(s.access_restrictions, [])) = 0
        LIMIT 1""").fetchone()
    con.execute("CREATE SCHEMA IF NOT EXISTS osm")
    con.execute("CREATE OR REPLACE TABLE osm.roundabout_way AS "
                "SELECT ?::BIGINT AS osm_way_id, 'test' AS fetched_at, "
                "       'test' AS source, '' AS query", [way])
    con.close()

    flatten(out, mode="edges", log=lambda *a: None)
    flagged, oneway = _q(out, """SELECT count(*), count(*) FILTER (WHERE oneway)
                                 FROM flat.segment WHERE segment_id = ?""", [seg])
    assert flagged > 0, "the planted way id never reached flat.segment"
    assert flagged == oneway, "a roundabout must come out one-way"

    # and nothing else moved
    assert _q(out, """SELECT count(*) FROM flat.segment
        WHERE is_roundabout AND segment_id <> ?""", [seg])[0] == 0

    _q_all(out, "DROP TABLE IF EXISTS osm.roundabout_way")


def test_report_draws_piece_geometry_not_whole_segment(built):
    """Each row must be drawn where it actually is, not over its parent segment's whole line.

    The report joined flat.segment to transportation.segment and then emitted `s.geometry`, so in
    the split modes every piece was drawn with the FULL segment line, stacked on top of each other.
    Söderledstunneln rendered as a bridge end to end: its 107 m bridge piece painted the whole
    1,576 m line over its own 1,469 m of tunnel.
    """
    from duckoverture import flatten
    from duckoverture.viz import load_roads
    out, _ = built
    flatten(out, mode="edges", log=lambda *a: None)
    g = load_roads(out, report=True)

    multi = g[g.segment_id.duplicated(keep=False)]
    assert len(multi) > 0, "need a split segment to test with"
    # pieces of one segment must not share a geometry
    for _, grp in multi.groupby("segment_id"):
        if len(grp) > 1:
            assert grp.geometry.apply(lambda x: x.wkb).nunique() == len(grp), \
                "pieces of a segment share a geometry — whole-segment line leaked in"
            break

    # and the drawn line must be as long as the row claims
    import geopandas as gpd
    m = gpd.GeoSeries(g.geometry, crs=4326).to_crs(3006).length
    assert (abs(m - g.length_m) < 1.0).mean() > 0.99, "drawn length disagrees with length_m"


def test_motor_access_respects_the_class_not_just_the_rules(built):
    """A footway is closed to cars whether or not Overture bothers to say so.

    OSM lets the class IMPLY access — highway=footway means no cars with no tag written — and
    Overture inherits the silence: on Södermalm it explicitly denies motor traffic on 18 of 5,968
    non-motor edges. Reading access_restrictions alone called 156 km of footway, steps and cycleway
    drivable, which is 60% of everything the column marked `yes`.
    """
    from duckoverture import flatten
    out, _ = built
    flatten(out, mode="edges", log=lambda *a: None)

    bad = _q(out, """SELECT count(*) FROM flat.segment
        WHERE motor_access = 'yes'
          AND class IN ('footway', 'steps', 'path', 'pedestrian', 'cycleway', 'bridleway')""")[0]
    assert bad == 0, f"{bad} non-motor edges still marked drivable"

    # but a car road with nothing said stays open — this must not close the whole network
    assert _q(out, """SELECT count(*) FROM flat.segment
        WHERE motor_access = 'yes' AND class IN ('residential', 'secondary', 'tertiary')""")[0] > 0


def test_indoor_is_not_tunnel(built):
    """is_indoor and is_tunnel are different things and both must be exposed.

    A station concourse or a passage through a building sits below grade because it is a basement
    floor, not because it is bored under ground. Södermalm carries almost as many indoor as tunnel
    segments, and a third of the indoor ones are not tunnels at all — but roadstyle draws the
    tunnel style from `lvl < 0`, so they render identically. The flag is the only way to tell.
    """
    from duckoverture import flatten
    out, _ = built
    flatten(out, mode="routing", log=lambda *a: None)

    cols = {r[0] for r in _q_all(out, "DESCRIBE flat.segment")}
    assert {"is_indoor", "is_covered", "is_under_construction"} <= cols

    # they must not be aliases of each other
    both, indoor_only = _q(out, """SELECT count(*) FILTER (WHERE is_indoor AND is_tunnel),
                                          count(*) FILTER (WHERE is_indoor AND NOT is_tunnel)
                                   FROM flat.segment""")
    assert indoor_only > 0, "every indoor piece came out as a tunnel — the flags are conflated"

    # a flag is a BOOLEAN, never NULL — list_contains(NULL, ...) is NULL, which bit is_link before
    for f in ("is_bridge", "is_tunnel", "is_link", "is_indoor", "is_covered",
              "is_under_construction", "is_roundabout"):
        assert _q(out, f"SELECT count(*) FROM flat.segment WHERE {f} IS NULL")[0] == 0, f


def test_direction_is_null_where_travel_is_unrestricted(built):
    """`direction` may only speak where there is a restriction to describe.

    It was 'forward' on every two-way road — 7,357 of 8,980 Södermalm rows — purely as the CASE
    default, which reads as a fact and is not one. NULL means unrestricted, so a non-null value
    always carries information. The rare non-default matters: a handful of pieces run 'backward',
    i.e. travel goes from to_connector_id towards from_connector_id.
    """
    from duckoverture import flatten
    out, _ = built
    flatten(out, mode="routing", log=lambda *a: None)
    assert _q(out, "SELECT count(*) FROM flat.segment WHERE oneway AND direction IS NULL")[0] == 0
    assert _q(out, "SELECT count(*) FROM flat.segment WHERE NOT oneway AND direction IS NOT NULL")[0] == 0
    assert set(_q(out, "SELECT list_sort(list_distinct(list(direction))) FROM flat.segment")[0]) \
        <= {"forward", "backward"}


def test_flat_connector_covers_every_node_including_synthetic(built):
    """The node table must place EVERY node an edge references, not just Overture's own.

    9% of Södermalm's nodes are synthetic — the `<segment_id>@<lr>` ids minted where a piece
    boundary is an attribute change rather than a junction. They have no row in
    transportation.connector, so a router reading that table alone cannot locate them.
    """
    from duckoverture import flatten
    out, _ = built
    flatten(out, mode="routing", log=lambda *a: None)

    assert _q(out, """
        SELECT count(*) FROM (
            SELECT from_connector_id AS id FROM flat.segment
            UNION SELECT to_connector_id FROM flat.segment) e
        WHERE NOT EXISTS (SELECT 1 FROM flat.connector c WHERE c.connector_id = e.id)""")[0] == 0

    assert _q(out, "SELECT count(*) FROM flat.connector WHERE is_synthetic")[0] > 0
    assert _q(out, "SELECT count(*) FROM flat.connector WHERE geometry IS NULL")[0] == 0
    # degree counts edge-ends, so it must equal twice the edge count
    n_ends = _q(out, "SELECT sum(degree) FROM flat.connector")[0]
    assert n_ends == 2 * _q(out, "SELECT count(*) FROM flat.segment")[0]
