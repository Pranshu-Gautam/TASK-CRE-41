"""CRS validation, projection and geometry reprojection."""

from __future__ import annotations

import math

import pytest
from shapely.geometry import Point, Polygon

from iris_cre41.crs import (
    InvalidCoordinateError,
    UnsupportedCRSError,
    get_transformer,
    in_germany,
    is_metric_crs,
    to_target,
    transform_geometry,
    validate_source_epsg,
    validate_wgs84,
)


def test_known_point_projects_to_expected_utm32n():
    # Mr. Wash Aachen, Breslauer Straße 10 (geocoded 50.7784123, 6.1188465).
    x, y = to_target(50.7784123, 6.1188465)
    assert 290_000 < x < 300_000
    assert 5_620_000 < y < 5_640_000
    # Round-trip must return to the original coordinate within 1 mm.
    back = get_transformer(25832, 4326).transform(x, y)
    assert back[0] == pytest.approx(6.1188465, abs=1e-7)
    assert back[1] == pytest.approx(50.7784123, abs=1e-7)


def test_projection_is_metre_based():
    x1, y1 = to_target(53.0, 9.0)
    x2, y2 = to_target(53.0089932, 9.0)  # ~1 km north
    assert math.dist((x1, y1), (x2, y2)) == pytest.approx(1000, rel=0.01)


@pytest.mark.parametrize(
    "lat,lon",
    [(None, 9.0), (53.0, None), (0.0, 0.0), (91.0, 9.0), (-91.0, 9.0),
     (53.0, 181.0), (53.0, -181.0), (float("nan"), 9.0), ("x", 9.0)],
)
def test_invalid_coordinates_are_rejected_not_corrected(lat, lon):
    with pytest.raises(InvalidCoordinateError):
        to_target(lat, lon)


def test_validate_wgs84_returns_floats():
    assert validate_wgs84("53.0", "9.0") == (53.0, 9.0)


@pytest.mark.parametrize("epsg", [3857, 31467, 4258, 0, None])
def test_unsupported_source_crs_is_refused(epsg):
    with pytest.raises(UnsupportedCRSError):
        validate_source_epsg(epsg)


@pytest.mark.parametrize("epsg", [4326, 25832])
def test_supported_source_crs(epsg):
    assert validate_source_epsg(epsg) == epsg


def test_unsupported_crs_blocks_projection():
    with pytest.raises(UnsupportedCRSError):
        to_target(53.0, 9.0, source_epsg=3857)


def test_is_metric_crs():
    assert is_metric_crs(25832) is True
    assert is_metric_crs(4326) is False


def test_in_germany_envelope_flags_outliers():
    assert in_germany(50.77, 6.11) is True
    assert in_germany(48.85, 2.35) is False   # Paris
    assert in_germany(40.71, -74.0) is False  # New York


def test_transform_geometry_preserves_topology_and_scales_area():
    # ~100 m x 100 m box near Hamburg expressed in degrees.
    lon, lat = 9.9937, 53.5511
    d_lon = 100 / (111_320 * math.cos(math.radians(lat)))
    d_lat = 100 / 110_574
    polygon = Polygon(
        [(lon, lat), (lon + d_lon, lat), (lon + d_lon, lat + d_lat), (lon, lat + d_lat)]
    )
    projected = transform_geometry(polygon, 4326)
    assert projected.geom_type == "Polygon"
    assert projected.is_valid
    assert projected.area == pytest.approx(10_000, rel=0.02)


def test_transform_geometry_is_identity_for_same_crs():
    point = Point(500_000, 5_900_000)
    assert transform_geometry(point, 25832) is point


def test_transform_geometry_refuses_unsupported_crs():
    with pytest.raises(UnsupportedCRSError):
        transform_geometry(Point(0, 0), 3857)
