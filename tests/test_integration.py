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


def test_flatten_split_conserves_length_and_cuts_only_on_change(built):
    from duckoverture import flatten
    out, _ = built
    flatten(out, split=True, log=lambda *a: None)
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
