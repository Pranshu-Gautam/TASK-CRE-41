"""Spatial matching rules: deterministic, documented, metric-only."""

from __future__ import annotations

import pytest

from iris_cre41.crs import to_target
from iris_cre41.enrich import MatchConfig, match_site

# A site in Aachen used as the anchor for all synthetic geometries.
SITE_LAT, SITE_LON = 50.7784123, 6.1188465
SITE_X, SITE_Y = to_target(SITE_LAT, SITE_LON)

# Degrees per metre at this latitude (good enough for building synthetic cases).
import math  # noqa: E402

DEG_PER_M_LON = 1 / (111_320 * math.cos(math.radians(SITE_LAT)))
DEG_PER_M_LAT = 1 / 110_574


def box(offset_east_m: float, offset_north_m: float, size_m: float, **tags):
    """A square polygon of ``size_m`` with its south-west corner at the offset."""
    lon0 = SITE_LON + offset_east_m * DEG_PER_M_LON
    lat0 = SITE_LAT + offset_north_m * DEG_PER_M_LAT
    lon1 = SITE_LON + (offset_east_m + size_m) * DEG_PER_M_LON
    lat1 = SITE_LAT + (offset_north_m + size_m) * DEG_PER_M_LAT
    return {
        "type": "way",
        "id": abs(int(offset_east_m * 1000 + offset_north_m)) + 1,
        "tags": tags,
        "geometry": [
            {"lat": lat0, "lon": lon0},
            {"lat": lat0, "lon": lon1},
            {"lat": lat1, "lon": lon1},
            {"lat": lat1, "lon": lon0},
            {"lat": lat0, "lon": lon0},
        ],
    }


def run(elements, operator="Mr. Wash", config=None):
    return match_site(
        elements, SITE_X, SITE_Y, operator, config=config or MatchConfig(),
        source_epsg=4326,
    )


# -- building rules ---------------------------------------------------------

def test_b1_point_inside_building_is_high_confidence():
    outcome = run([box(-10, -10, 20, building="commercial")])
    building = outcome["building"]
    assert building.match_method == "point_in_building"
    assert building.geometric_match_confidence == "high"
    assert building.distance_m == 0.0
    assert building.area_sqm == pytest.approx(400, rel=0.05)
    assert building.geometry_type == "MultiPolygon"
    assert building.validity_status == "valid"


def test_b2_nearby_building_is_medium_confidence():
    outcome = run([box(20, 0, 10, building="yes")])
    building = outcome["building"]
    assert building.match_method == "nearest_building_le_30m"
    assert building.geometric_match_confidence == "medium"
    assert 0 < building.distance_m <= 30


def test_b3_distant_building_within_radius_is_low_confidence():
    outcome = run([box(50, 0, 10, building="yes")])
    building = outcome["building"]
    assert building.match_method == "nearest_building_le_80m"
    assert building.geometric_match_confidence == "low"


def test_building_beyond_the_radius_is_not_matched():
    outcome = run([box(200, 0, 10, building="yes")])
    assert outcome["building"] is None
    assert outcome["building_candidates"] == 0 or outcome["building"] is None


def test_containment_wins_over_a_closer_centroid():
    elements = [box(-30, -30, 60, building="yes"), box(5, 5, 4, building="yes")]
    # The small box is 5 m away; the large one contains the point.
    outcome = run(elements)
    assert outcome["building"].match_method == "point_in_building"
    assert outcome["building"].area_sqm == pytest.approx(3600, rel=0.05)


def test_untagged_polygons_are_not_building_candidates():
    outcome = run([box(-10, -10, 20, landuse="retail")])
    assert outcome["building"] is None
    assert outcome["building_candidates"] == 0


def test_no_elements_yields_no_matches_and_no_invented_areas():
    outcome = run([])
    assert outcome["building"] is None
    assert outcome["parking"] is None
    assert outcome["parking_area_total_sqm"] is None
    assert outcome["osm_operator_tag_match"] is False


# -- parking rules ----------------------------------------------------------

def test_p1_point_inside_parking_is_high_confidence():
    outcome = run([box(-15, -15, 30, amenity="parking")])
    parking = outcome["parking"]
    assert parking.match_method == "point_in_parking"
    assert parking.geometric_match_confidence == "high"
    assert "proximity_does_not_prove_ownership" in parking.notes


def test_p2_adjacent_parking_is_medium_confidence():
    outcome = run([box(10, 0, 20, amenity="parking")])
    assert outcome["parking"].match_method == "adjacent_parking_le_15m"
    assert outcome["parking"].geometric_match_confidence == "medium"


def test_p3_nearby_parking_is_low_confidence():
    outcome = run([box(60, 0, 20, amenity="parking")])
    assert outcome["parking"].match_method == "nearby_parking_le_120m"
    assert outcome["parking"].geometric_match_confidence == "low"


def test_parking_total_sums_every_candidate_in_the_radius():
    outcome = run(
        [box(20, 0, 10, amenity="parking"), box(-40, 0, 10, amenity="parking")]
    )
    assert outcome["parking_candidates"] == 2
    assert outcome["parking_area_total_sqm"] == pytest.approx(200, rel=0.05)
    # The *matched* polygon is only the nearest one, not the sum.
    assert outcome["parking"].area_sqm == pytest.approx(100, rel=0.05)


# -- operator corroboration -------------------------------------------------

def test_operator_name_on_the_building_sets_the_tag_match():
    outcome = run([box(-10, -10, 20, building="yes", name="Mr. Wash Aachen")])
    assert outcome["osm_operator_tag_match"] is True
    assert "matched_building_name_matches_operator" in outcome["building"].notes


def test_car_wash_node_nearby_is_recorded_without_claiming_ownership():
    node = {
        "type": "node",
        "id": 77,
        "lat": SITE_LAT + 5 * DEG_PER_M_LAT,
        "lon": SITE_LON,
        "tags": {"amenity": "car_wash"},
    }
    outcome = run([box(-10, -10, 20, building="yes"), node])
    assert outcome["osm_car_wash_feature_id"] == "node/77"
    # A bare car_wash tag is corroboration of use, not of operator identity.
    assert outcome["osm_operator_tag_match"] is False


def test_operator_match_is_operator_specific():
    outcome = run(
        [box(-10, -10, 20, building="yes", name="Clean Car Bremen")],
        operator="Mr. Wash",
    )
    assert outcome["osm_operator_tag_match"] is False


# -- geometry handling in matching -----------------------------------------

def test_invalid_candidate_geometry_is_repaired_and_still_matched():
    bowtie = {
        "type": "way",
        "id": 500,
        "tags": {"building": "yes"},
        "geometry": [
            {"lat": SITE_LAT - 10 * DEG_PER_M_LAT, "lon": SITE_LON - 10 * DEG_PER_M_LON},
            {"lat": SITE_LAT + 10 * DEG_PER_M_LAT, "lon": SITE_LON + 10 * DEG_PER_M_LON},
            {"lat": SITE_LAT - 10 * DEG_PER_M_LAT, "lon": SITE_LON + 10 * DEG_PER_M_LON},
            {"lat": SITE_LAT + 10 * DEG_PER_M_LAT, "lon": SITE_LON - 10 * DEG_PER_M_LON},
        ],
    }
    outcome = run([bowtie])
    assert outcome["building"] is not None
    assert outcome["building"].validity_status == "repaired"
    assert outcome["building"].area_sqm > 0


def test_degenerate_candidates_are_skipped_not_counted_as_matches():
    open_way = {
        "type": "way", "id": 501, "tags": {"building": "yes"},
        "geometry": [{"lat": SITE_LAT, "lon": SITE_LON},
                     {"lat": SITE_LAT + 0.0001, "lon": SITE_LON}],
    }
    outcome = run([open_way])
    assert outcome["building"] is None
    assert outcome["rejected_geometries"] == 1


def test_areas_are_only_computed_in_a_metric_crs():
    with pytest.raises(ValueError, match="not a metric CRS"):
        match_site([], SITE_X, SITE_Y, "Mr. Wash",
                   config=MatchConfig(target_epsg=4326))


def test_stored_wkt_is_in_the_target_crs():
    outcome = run([box(-10, -10, 20, building="yes")])
    wkt = outcome["building"].wkt_25832
    assert wkt.startswith("MULTIPOLYGON")
    # UTM32N easting for Aachen is ~296 000 — degrees would be ~6.
    assert "296" in wkt.split("((")[1][:8]


def test_match_results_are_reproducible():
    elements = [box(-10, -10, 20, building="yes"), box(30, 0, 10, amenity="parking")]
    first = run(elements)
    second = run(list(reversed(elements)))
    assert first["building"].feature_id == second["building"].feature_id
    assert first["parking"].feature_id == second["parking"].feature_id
    assert first["building"].area_sqm == second["building"].area_sqm


# -- host-structure detection ----------------------------------------------

def test_oversized_footprint_is_flagged_as_a_likely_host_structure():
    # 150 m x 150 m = 22 500 m2: a retail park, not a car wash.
    outcome = run([box(-75, -75, 150, building="retail", shop="mall",
                       name="Citti-Park Kiel")])
    assert "building_area_outlier_gt_10000_sqm" in outcome["quality_flags"]
    assert outcome["building"].area_sqm > 10_000
    # The geometric fact is unchanged: the point really is inside it.
    assert outcome["building"].geometric_match_confidence == "high"


def test_building_named_after_another_occupant_is_flagged():
    outcome = run([box(-10, -10, 20, building="retail", name="REWE Center")])
    assert "building_attributed_to_other_occupant" in outcome["quality_flags"]
    assert outcome["building"].operator_attribution_status == "third_party_named"
    assert "REWE Center" in outcome["building"].operator_attribution_evidence


def test_operator_owned_building_is_not_flagged_as_another_occupant():
    outcome = run([box(-10, -10, 20, building="yes", name="Mr. Wash Aachen")])
    assert "building_attributed_to_other_occupant" not in outcome["quality_flags"]


def test_car_wash_tagged_building_is_not_flagged_as_another_occupant():
    outcome = run([box(-10, -10, 20, building="yes", amenity="car_wash",
                       name="Autowaschanlage")])
    assert "building_attributed_to_other_occupant" not in outcome["quality_flags"]


def test_untagged_building_name_produces_no_occupant_flag():
    outcome = run([box(-10, -10, 20, building="yes")])
    assert "building_attributed_to_other_occupant" not in outcome["quality_flags"]
    assert outcome["building"].operator_attribution_status == "unverified"


# --------------------------------------------------------------------------
# Operator attribution — the three assertions must stay separate
# --------------------------------------------------------------------------

def test_operator_named_building_yields_an_attributable_area():
    outcome = run([box(-10, -10, 20, building="yes", name="Mr. Wash Aachen")])
    building = outcome["building"]
    assert building.geometric_match_confidence == "high"
    assert building.operator_attribution_status == "operator_tagged"
    assert building.operator_attributable_area_sqm == building.area_sqm
    # Assertion 3 is never claimed.
    assert building.operator_usable_area_verified is False


def test_car_wash_tagged_building_corroborates_use_not_operator():
    outcome = run([box(-10, -10, 20, building="yes", amenity="car_wash")])
    building = outcome["building"]
    assert building.operator_attribution_status == "car_wash_tagged"
    # The use is corroborated, the operator is not, so no operator area.
    assert building.operator_attributable_area_sqm is None
    assert "building_operator_area_not_attributable" in outcome["quality_flags"]


def test_shopping_mall_gets_no_operator_specific_area():
    """A point inside a mall must not hand the mall's roof to the operator."""
    outcome = run([box(-75, -75, 150, building="retail", shop="mall",
                       name="Citti-Park Kiel")])
    building = outcome["building"]
    # The geometric fact is preserved...
    assert building.geometric_match_confidence == "high"
    assert building.area_sqm > 10_000          # raw OSM metric kept for provenance
    # ...but no operator-specific area is asserted.
    assert building.operator_attribution_status == "third_party_named"
    assert building.operator_attributable_area_sqm is None
    assert "building_area_outlier_gt_10000_sqm" in outcome["quality_flags"]
    assert "building_attributed_to_other_occupant" in outcome["quality_flags"]


def test_untagged_footprint_gets_no_operator_specific_area():
    outcome = run([box(-10, -10, 20, building="yes")])
    assert outcome["building"].operator_attribution_status == "unverified"
    assert outcome["building"].operator_attributable_area_sqm is None


def test_oversized_operator_named_building_still_gets_no_area():
    """Even an operator-named polygon is refused if it is implausibly large."""
    outcome = run([box(-75, -75, 150, building="yes", name="Mr. Wash")])
    building = outcome["building"]
    assert building.operator_attribution_status == "operator_tagged"
    assert building.operator_attributable_area_sqm is None
    assert "exceeds the plausible single-site threshold" in (
        building.operator_attribution_evidence
    )


def test_shared_parking_lot_gets_no_operator_specific_area():
    outcome = run([box(-75, -75, 150, amenity="parking", name="Citti-Park")])
    parking = outcome["parking"]
    assert parking.geometric_match_confidence == "high"      # point is inside it
    assert parking.operator_attribution_status == "third_party_named"
    assert parking.operator_attributable_area_sqm is None
    assert "parking_operator_area_not_attributable" in outcome["quality_flags"]
    assert "proximity_does_not_prove_ownership" in parking.notes


def test_parking_is_never_classified_as_car_wash_tagged():
    outcome = run([box(-15, -15, 30, amenity="parking")])
    assert outcome["parking"].operator_attribution_status == "unverified"


def test_operator_named_parking_is_attributable():
    outcome = run([box(-15, -15, 30, amenity="parking", operator="Mr. Wash")])
    parking = outcome["parking"]
    assert parking.operator_attribution_status == "operator_tagged"
    assert parking.operator_attributable_area_sqm == parking.area_sqm


def test_no_match_yields_no_match_status():
    outcome = run([])
    assert outcome["building"] is None and outcome["parking"] is None


@pytest.mark.parametrize(
    "tags,expected",
    [
        ({"name": "Mr. Wash"}, "operator_tagged"),
        ({"operator": "Mr. Wash Autoservice AG"}, "operator_tagged"),
        ({"amenity": "car_wash"}, "car_wash_tagged"),
        ({"shop": "car_wash"}, "car_wash_tagged"),
        ({"name": "REWE Center"}, "third_party_named"),
        ({"building": "yes"}, "unverified"),
        ({}, "unverified"),
    ],
)
def test_classify_operator_attribution_statuses(tags, expected):
    from iris_cre41.enrich.matching import classify_operator_attribution

    status, evidence, _ = classify_operator_attribution(tags, "Mr. Wash", 400.0)
    assert status == expected
    assert evidence


def test_classify_returns_no_match_for_an_absent_polygon():
    from iris_cre41.enrich.matching import classify_operator_attribution

    status, _, area = classify_operator_attribution({}, "Mr. Wash", None)
    assert status == "no_match"
    assert area is None
