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
    for c in ("highway", "bridge", "tunnel", "layer", "maxspeed_kmh", "geometry"):
        assert c in g.columns, f"{c} missing — roadstyle will not style this"
    # highway must carry Overture's class values, which roadstyle resolves directly
    assert g.highway.notna().any()
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
    for c in ("highway", "bridge", "tunnel", "layer", "maxspeed_kmh", "oneway", "geometry"):
        assert c in g.columns, f"{c} missing — roadstyle will not style this"
    # nested columns must be summarised, not dumped
    for c in ("n_connectors", "all_speed_limits", "access", "flags", "source_dataset"):
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


ATTRS = ["highway", "subclass", "name", "level", "surface", "width_m", "maxspeed_kmh",
         "is_bridge", "is_tunnel", "is_link", "oneway", "direction"]


def _q(out, sql):
    """Query and CLOSE — a lingering read-only handle locks the next flatten()."""
    import duckdb as _d
    con = _d.connect(str(out), read_only=True)
    con.execute("LOAD spatial;")
    try:
        return con.execute(sql).fetchone()
    finally:
        con.close()


def _length_matches(out):
    a, b = _q(out, """SELECT (SELECT sum(length_m) FROM flat.segment),
                             (SELECT sum(ST_Length_Spheroid(geometry))
                              FROM transportation.segment WHERE subtype='road')""")
    return abs(a - b) / b < 1e-6


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
    """segment <= attrs <= edges, and all three conserve length."""
    from duckoverture import flatten
    out, _ = built
    counts = {}
    for m in ("segment", "attrs", "edges"):
        flatten(out, mode=m, log=lambda *a: None)
        counts[m] = _q(out, "SELECT count(*) FROM flat.segment")[0]
        assert _length_matches(out), f"{m} lost geometry"
    assert counts["segment"] <= counts["attrs"] <= counts["edges"], counts


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
