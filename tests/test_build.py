"""Unit tests for the parts that do not need the network.

The extraction itself is exercised by `tests/test_integration.py`, which is skipped unless
DUCKOVERTURE_NETWORK_TESTS=1 — pulling from S3 in CI is slow and flaky.
"""
import json

import pytest

from duckoverture import TYPES, THEMES, resolve
from duckoverture.build import _load_boundary, _connect


def test_catalogue_is_complete():
    assert len(TYPES) == 15
    assert THEMES == ["addresses", "base", "buildings", "divisions", "places", "transportation"]
    assert {t.type for t in TYPES} >= {"segment", "connector", "building", "place", "address"}


def test_table_and_url():
    seg = next(t for t in TYPES if t.type == "segment")
    assert seg.table == "transportation.segment"
    url = seg.url("2026-07-22.0")
    assert url.endswith("theme=transportation/type=segment/*")
    assert "2026-07-22.0" in url


def test_resolve_all_by_default():
    assert len(resolve(None)) == 15
    assert len(resolve([])) == 15


def test_resolve_by_type_and_theme():
    assert [t.table for t in resolve(["segment"])] == ["transportation.segment"]
    # a theme selects every type in it
    assert {t.table for t in resolve(["buildings"])} == {"buildings.building",
                                                        "buildings.building_part"}
    # mixing is fine, and no duplicates when both match
    assert len(resolve(["transportation", "segment"])) == 2


def test_resolve_rejects_unknown():
    with pytest.raises(ValueError, match="unknown type/theme"):
        resolve(["not_a_theme"])


@pytest.mark.parametrize("wrapper", ["geometry", "feature", "featurecollection"])
def test_boundary_accepts_every_geojson_shape(tmp_path, wrapper):
    poly = {"type": "Polygon", "coordinates": [[[0, 0], [0, 2], [3, 2], [3, 0], [0, 0]]]}
    doc = {
        "geometry": poly,
        "feature": {"type": "Feature", "properties": {}, "geometry": poly},
        "featurecollection": {"type": "FeatureCollection",
                              "features": [{"type": "Feature", "properties": {},
                                            "geometry": poly}]},
    }[wrapper]
    p = tmp_path / "b.geojson"
    p.write_text(json.dumps(doc))
    con = _connect(tmp_path / "out.duckdb")
    assert _load_boundary(con, p) == (0.0, 0.0, 3.0, 2.0)
    con.close()


def test_boundary_unions_multiple_features(tmp_path):
    """Two disjoint squares -> one boundary whose bbox spans both."""
    def sq(x):
        return {"type": "Polygon",
                "coordinates": [[[x, 0], [x, 1], [x + 1, 1], [x + 1, 0], [x, 0]]]}
    p = tmp_path / "b.geojson"
    p.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": sq(0)},
        {"type": "Feature", "properties": {}, "geometry": sq(5)}]}))
    con = _connect(tmp_path / "out.duckdb")
    assert _load_boundary(con, p) == (0.0, 0.0, 6.0, 1.0)
    con.close()
