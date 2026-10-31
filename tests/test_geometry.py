"""OSM element -> Shapely geometry: assembly, validation and repair."""

from __future__ import annotations

import pytest
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiPolygon,
    Point,
    Polygon,
)

from iris_cre41.enrich.geometry import (
    REJECTED_EMPTY,
    REJECTED_TYPE,
    REJECTED_UNBUILDABLE,
    REPAIRED,
    VALID,
    as_multipolygon,
    element_to_geometry,
    stitch_rings,
    validate_and_repair,
)


def way(coords, **tags):
    return {
        "type": "way",
        "id": 1,
        "tags": tags or {"building": "yes"},
        "geometry": [{"lat": lat, "lon": lon} for lon, lat in coords],
    }


# -- element -> geometry ----------------------------------------------------

def test_closed_way_becomes_polygon():
    geometry = element_to_geometry(way([(0, 0), (0, 1), (1, 1), (1, 0)]))
    assert geometry.geom_type == "Polygon"
    assert geometry.area == pytest.approx(1.0)


def test_way_is_closed_automatically_when_needed():
    explicit = element_to_geometry(way([(0, 0), (0, 1), (1, 1), (1, 0), (0, 0)]))
    implicit = element_to_geometry(way([(0, 0), (0, 1), (1, 1), (1, 0)]))
    assert explicit.equals(implicit)


def test_node_has_no_footprint():
    assert element_to_geometry({"type": "node", "id": 5, "lat": 1.0, "lon": 2.0}) is None


def test_way_with_too_few_vertices_is_unbuildable():
    assert element_to_geometry(way([(0, 0), (1, 1)])) is None


def test_way_with_missing_node_coordinates_is_handled():
    element = {
        "type": "way",
        "id": 2,
        "tags": {"building": "yes"},
        "geometry": [{"lat": 0, "lon": 0}, {"lat": None, "lon": None}, {"lat": 1, "lon": 1}],
    }
    assert element_to_geometry(element) is None


def test_unknown_element_type_returns_none():
    assert element_to_geometry({"type": "changeset", "id": 1}) is None


# -- multipolygon relations -------------------------------------------------

def test_stitch_rings_joins_split_segments_in_any_direction():
    segments = [
        [(0, 0), (0, 4)],
        [(4, 4), (4, 0), (0, 0)],
        [(0, 4), (4, 4)],
    ]
    rings = stitch_rings(segments)
    assert len(rings) == 1
    assert rings[0][0] == rings[0][-1]
    assert Polygon(rings[0]).area == pytest.approx(16.0)


def test_stitch_rings_drops_unclosable_segments():
    assert stitch_rings([[(0, 0), (1, 0)]]) == []


def test_relation_with_hole_has_correct_area():
    relation = {
        "type": "relation",
        "id": 9,
        "tags": {"building": "yes", "type": "multipolygon"},
        "members": [
            {"type": "way", "role": "outer",
             "geometry": [{"lat": 0, "lon": 0}, {"lat": 0, "lon": 4}]},
            {"type": "way", "role": "outer",
             "geometry": [{"lat": 0, "lon": 4}, {"lat": 4, "lon": 4},
                          {"lat": 4, "lon": 0}, {"lat": 0, "lon": 0}]},
            {"type": "way", "role": "inner",
             "geometry": [{"lat": 1, "lon": 1}, {"lat": 1, "lon": 2},
                          {"lat": 2, "lon": 2}, {"lat": 2, "lon": 1},
                          {"lat": 1, "lon": 1}]},
        ],
    }
    geometry = element_to_geometry(relation)
    assert geometry.area == pytest.approx(16.0 - 1.0)
    validation = validate_and_repair(geometry)
    assert validation.status == VALID
    assert validation.geometry.area == pytest.approx(15.0)


def test_relation_with_two_outer_rings_becomes_multipolygon():
    relation = {
        "type": "relation",
        "id": 10,
        "tags": {"building": "yes"},
        "members": [
            {"type": "way", "role": "outer",
             "geometry": [{"lat": 0, "lon": 0}, {"lat": 0, "lon": 1},
                          {"lat": 1, "lon": 1}, {"lat": 1, "lon": 0},
                          {"lat": 0, "lon": 0}]},
            {"type": "way", "role": "outer",
             "geometry": [{"lat": 5, "lon": 5}, {"lat": 5, "lon": 6},
                          {"lat": 6, "lon": 6}, {"lat": 6, "lon": 5},
                          {"lat": 5, "lon": 5}]},
        ],
    }
    geometry = element_to_geometry(relation)
    assert geometry.geom_type == "MultiPolygon"
    assert geometry.area == pytest.approx(2.0)


def test_relation_without_usable_members_returns_none():
    assert element_to_geometry({"type": "relation", "id": 11, "members": []}) is None


# -- validation / repair ----------------------------------------------------

def test_self_intersecting_polygon_is_repaired_and_flagged():
    bowtie = Polygon([(0, 0), (1, 1), (1, 0), (0, 1)])
    assert not bowtie.is_valid
    validation = validate_and_repair(bowtie)
    assert validation.status == REPAIRED
    assert "make_valid_applied" in validation.notes
    assert validation.geometry.is_valid
    assert validation.geometry.area > 0
    assert validation.ok


def test_empty_geometry_is_rejected():
    validation = validate_and_repair(Polygon())
    assert validation.status == REJECTED_EMPTY
    assert validation.geometry is None
    assert validation.ok is False


def test_none_geometry_is_rejected():
    assert validate_and_repair(None).status == REJECTED_UNBUILDABLE


@pytest.mark.parametrize(
    "geometry",
    [LineString([(0, 0), (1, 1)]), Point(0, 0)],
)
def test_non_polygonal_geometry_is_rejected(geometry):
    validation = validate_and_repair(geometry)
    assert validation.status == REJECTED_TYPE
    assert validation.geometry is None


def test_geometry_collection_keeps_only_polygonal_parts():
    collection = GeometryCollection(
        [Polygon([(0, 0), (0, 1), (1, 1), (1, 0)]), LineString([(5, 5), (6, 6)])]
    )
    validation = validate_and_repair(collection)
    assert validation.ok
    assert validation.geometry.geom_type in {"Polygon", "MultiPolygon"}
    assert validation.geometry.area == pytest.approx(1.0)


def test_valid_polygon_passes_through_unchanged():
    polygon = Polygon([(0, 0), (0, 2), (2, 2), (2, 0)])
    validation = validate_and_repair(polygon)
    assert validation.status == VALID
    assert validation.geometry.area == pytest.approx(4.0)


def test_as_multipolygon_normalises_types():
    polygon = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])
    assert as_multipolygon(polygon).geom_type == "MultiPolygon"
    multi = MultiPolygon([polygon])
    assert as_multipolygon(multi) is multi


def test_as_multipolygon_refuses_non_polygonal():
    with pytest.raises(ValueError):
        as_multipolygon(LineString([(0, 0), (1, 1)]))
