"""Row building and content hashing (no database required)."""

from __future__ import annotations

import datetime as dt

import pytest

from iris_cre41.db.loader import build_poi_row, compute_row_hash
from iris_cre41.models import (
    EnrichedPoi,
    GeocodeResult,
    NormalizedLocation,
    SpatialMatch,
)

WHEN = dt.datetime(2026, 10, 31, 9, 0, tzinfo=dt.timezone.utc)


def location(**overrides) -> NormalizedLocation:
    base = dict(
        poi_uid="cre41-abc", source_uid="mrwash:mrwash.de:branch-36",
        dedup_key="deadbeef", operator="Mr. Wash", operator_slug="mrwash",
        location_name="Aachen Rothe Erde", location_name_raw="Aachen Rothe Erde",
        street="Breslauer Straße", house_number="10", postal_code="52068",
        city="Aachen", country="DE",
        address_raw="Breslauer Straße 10\n52068 Aachen",
        address_normalized="Breslauer Straße 10, 52068 Aachen",
        source_system="mrwash.de", source_record_id="branch-36",
        source_url="https://www.mrwash.de/de/frontend/branch?id=36",
        retrieved_at=WHEN,
        source_attributes={"max_vehicle_height_m": 1.95, "max_vehicle_width_m": 2.2},
    )
    base.update(overrides)
    return NormalizedLocation(**base)


def poi(**overrides) -> EnrichedPoi:
    base = EnrichedPoi(
        location=location(),
        geocode=GeocodeResult(
            status="ok", provider="nominatim", query="q", lat=50.7784, lon=6.1188,
            source_epsg=4326, precision="address", postcode_match=True,
            city_match=True, geocoded_at=WHEN,
        ),
        x_25832=296569.09,
        y_25832=5627972.58,
        point_wkt_25832="POINT (296569.09 5627972.58)",
        enrichment_status="partial",
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def test_row_contains_mandated_fields():
    row = build_poi_row(poi(), "collect-1", "load-1")
    assert row["poi_uid"] == "cre41-abc"
    assert row["source_uid"] == "mrwash:mrwash.de:branch-36"
    assert row["dedup_key"] == "deadbeef"
    assert row["geocode_lat"] == pytest.approx(50.7784)
    assert row["geocode_srid"] == 4326
    assert row["x_25832"] == pytest.approx(296569.09)
    assert row["geom"].srid == 25832
    assert row["parking_ownership_verified"] is False
    assert row["max_vehicle_height_m"] == pytest.approx(1.95)
    assert row["country"] == "DE"


def test_no_building_match_defaults_to_no_match_attribution():
    row = build_poi_row(poi(), "c", "l")
    assert row["building_operator_attribution_status"] == "no_match"
    assert row["building_operator_attributable_area_sqm"] is None
    assert row["parking_operator_attribution_status"] == "no_match"
    assert row["operator_usable_area_verified"] is False


def test_missing_geocode_leaves_geometry_null_not_zero():
    record = poi(geocode=None, x_25832=None, y_25832=None, point_wkt_25832=None)
    row = build_poi_row(record, "c", "l")
    assert row["geom"] is None
    assert row["geocode_status"] == "not_attempted"
    assert row["x_25832"] is None
    assert row["building_footprint_area_sqm"] is None


def test_building_match_maps_onto_columns():
    match = SpatialMatch(
        feature_kind="building", feature_id="way/1", geometry_type="MultiPolygon",
        match_method="point_in_building", geometric_match_confidence="high",
        distance_m=0.0, area_sqm=5381.7, validity_status="valid",
        operator_attribution_status="operator_tagged",
        operator_attribution_evidence="OSM tag names the operator (name=Mr. Wash)",
        operator_attributable_area_sqm=5381.7,
        wkt_25832="MULTIPOLYGON (((296560 5627960, 296570 5627960, "
                  "296570 5627970, 296560 5627970, 296560 5627960)))",
        tags={"building": "yes"}, source_url="https://www.openstreetmap.org/way/1",
    )
    row = build_poi_row(poi(building=match), "c", "l")
    assert row["building_feature_id"] == "way/1"
    assert row["building_footprint_area_sqm"] == pytest.approx(5381.7)
    assert row["building_geometric_match_confidence"] == "high"
    assert row["building_operator_attribution_status"] == "operator_tagged"
    assert row["building_operator_attributable_area_sqm"] == pytest.approx(5381.7)
    assert row["building_geom"].srid == 25832
    assert row["building_source"] == "openstreetmap"
    # Assertion 3 is never asserted, whatever the match looks like.
    assert row["operator_usable_area_verified"] is False


def test_row_hash_is_deterministic():
    assert compute_row_hash(build_poi_row(poi(), "c", "l")) == compute_row_hash(
        build_poi_row(poi(), "c", "l")
    )


def test_row_hash_ignores_run_ids_and_timestamps():
    first = build_poi_row(poi(), "collect-1", "load-1")
    second = build_poi_row(poi(), "collect-2", "load-2")
    assert first["row_hash"] == second["row_hash"]


def test_row_hash_changes_when_content_changes():
    baseline = build_poi_row(poi(), "c", "l")["row_hash"]
    changed = build_poi_row(
        poi(location=location(city="Köln")), "c", "l"
    )["row_hash"]
    assert baseline != changed


def test_row_hash_changes_when_geometry_changes():
    baseline = build_poi_row(poi(), "c", "l")["row_hash"]
    moved = poi()
    moved.point_wkt_25832 = "POINT (296570.00 5627980.00)"
    assert build_poi_row(moved, "c", "l")["row_hash"] != baseline


def test_data_quality_flags_are_merged_and_sorted():
    record = poi(data_quality_flags=["geocode_ambiguous"])
    record.location.data_quality_flags = ["house_number_missing"]
    row = build_poi_row(record, "c", "l")
    assert row["data_quality_flags"] == ["geocode_ambiguous", "house_number_missing"]


def test_page_hash_is_stored_but_excluded_from_the_content_hash():
    """mrwash.de embeds a per-response widget id, so the page hash is unstable.

    It must be recorded as provenance without making every run look changed.
    """
    first = poi(location=location(html_sha256="a" * 64))
    second = poi(location=location(html_sha256="b" * 64))
    row_a = build_poi_row(first, "c", "l")
    row_b = build_poi_row(second, "c", "l")
    assert row_a["source_html_sha256"] == "a" * 64
    assert row_b["source_html_sha256"] == "b" * 64
    assert row_a["row_hash"] == row_b["row_hash"]


def test_jsonb_columns_use_sql_null_not_json_null():
    """An absent JSONB value must be SQL NULL, not the JSON value `null`.

    SQLAlchemy's default renders Python None as JSON null, which would make
    `IS NULL` false and count() over-count in a staging table whose contract
    is that unavailable data is NULL.
    """
    from iris_cre41.db.schema import build_metadata

    metadata = build_metadata("iris_staging")
    table = metadata.tables["iris_staging.commercial_poi_staging"]
    jsonb_columns = [
        column
        for column in table.columns
        if column.type.__class__.__name__ == "JSONB"
    ]
    assert jsonb_columns, "expected JSONB columns in the staging table"
    for column in jsonb_columns:
        assert column.type.none_as_null is True, (
            f"{column.name} would store Python None as JSON null"
        )
