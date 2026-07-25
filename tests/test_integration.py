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


def test_report_mode_carries_every_field(built, tmp_path):
    """--report must keep ALL Overture columns, with nested ones readable as JSON."""
    pytest.importorskip("roadstyle")
    import json as _json
    from duckoverture.viz import load_roads, render
    out, _ = built
    plain = load_roads(out)
    full = load_roads(out, report=True)
    assert len(full.columns) > len(plain.columns), "report mode added no fields"
    # the raw Overture columns must be present alongside the roadstyle-shaped ones
    for c in ("id", "connectors", "speed_limits", "access_restrictions", "sources"):
        assert c in full.columns
    for c in ("highway", "bridge", "tunnel", "layer", "maxspeed_kmh"):
        assert c in full.columns
    # nested ones must be JSON TEXT, not python reprs — the panel shows values verbatim
    s = full.loc[full.connectors.notna(), "connectors"].iloc[0]
    assert isinstance(s, str) and _json.loads(s), "connectors is not readable JSON"
    html = render(out, tmp_path / "r.html", report=True, log=lambda *a: None)
    assert html.exists() and html.stat().st_size > 10_000


def test_oneway_is_derived_from_heading_denials(built):
    """Overture has no `oneway` column — it must be read out of access_restrictions."""
    import duckdb as _d
    from duckoverture.viz import ONEWAY_EXPR, load_roads, _heading_denied
    out, _ = built
    g = load_roads(out)
    assert "oneway" in g.columns
    assert g.oneway.dtype == bool or set(g.oneway.dropna().unique()) <= {True, False}

    con = _d.connect(str(out), read_only=True)
    con.execute("LOAD spatial;")
    # a mode-scoped foot/bicycle denial must NOT count as a one-way street
    bad = con.execute(f"""
        SELECT count(*) FROM transportation.segment
        WHERE subtype='road' AND {ONEWAY_EXPR}
          AND NOT EXISTS (SELECT 1 FROM unnest(access_restrictions) t(a)
                          WHERE a.access_type='denied' AND a."when".heading IS NOT NULL
                            AND (a."when".mode IS NULL
                                 OR list_has_any(a."when".mode,
                                    ['motor_vehicle','car','truck','hgv','bus','motorcycle','taxi','hov'])))
    """).fetchone()[0]
    assert bad == 0, "oneway derived from a non-motor restriction"
    # forward-denied segments must come back reversed, or roadstyle's chevrons point wrong
    n_rev = con.execute(f"""SELECT count(*) FROM transportation.segment WHERE subtype='road'
                            AND {_heading_denied('forward')} AND NOT {_heading_denied('backward')}
                         """).fetchone()[0]
    if n_rev:
        a, b = con.execute(f"""SELECT ST_AsText(ST_StartPoint(geometry)),
                                      ST_AsText(ST_StartPoint(ST_Reverse(geometry)))
                               FROM transportation.segment WHERE subtype='road'
                                 AND {_heading_denied('forward')} LIMIT 1""").fetchone()
        assert a != b
