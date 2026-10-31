"""Deterministic spatial matching of OSM features to a geocoded POI.

The rules are fixed, ordered and documented so that any reviewer can replay a
decision from the stored evidence.

Building rules (evaluated in order, first hit wins)
---------------------------------------------------
B1  the POI point lies inside a ``building`` polygon
        -> method ``point_in_building``,            confidence ``high``
B2  nearest ``building`` polygon within 30 m
        -> method ``nearest_building_le_30m``,      confidence ``medium``
B3  nearest ``building`` polygon within the search radius (default 80 m)
        -> method ``nearest_building_le_<R>m``,     confidence ``low``
otherwise no building match; metrics stay NULL and the POI keeps its point.

Parking rules (same shape)
--------------------------
P1  the POI point lies inside an ``amenity=parking`` polygon
        -> ``point_in_parking``,                    confidence ``high``
P2  nearest parking polygon within 15 m (contiguous with the site)
        -> ``adjacent_parking_le_15m``,             confidence ``medium``
P3  nearest parking polygon within the search radius (default 120 m)
        -> ``nearby_parking_le_<R>m``,              confidence ``low``

IMPORTANT — what "confidence" means here
----------------------------------------
Confidence describes the strength of the *geometric* association between the
geocoded address point and an OSM feature. It is **not** a statement about
property ownership, tenancy, or the operator's right to use the parking area.
Proximity does not prove ownership. An explicit
``osm_operator_tag_match`` flag records the only operator-level corroboration
available from OSM: a matching ``amenity=car_wash`` / ``shop=car_wash`` tag or
an operator name on the feature itself.

All distances and areas are computed in EPSG:25832 (metres). Nothing is
computed in degrees.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from shapely.geometry import Point
from shapely.geometry.base import BaseGeometry

from ..crs import TARGET_EPSG, is_metric_crs, transform_geometry
from ..logging_setup import get_logger
from ..models import SpatialMatch
from ..normalize import normalize_token
from .geometry import as_multipolygon, element_to_geometry, validate_and_repair
from .overpass import feature_id, feature_url

log = get_logger(__name__)

OPERATOR_NAME_TOKENS = {
    "Mr. Wash": ("mr wash", "mrwash"),
    "Clean Car": ("clean car", "cleancar"),
}

CAR_WASH_TAGS = (("amenity", "car_wash"), ("shop", "car_wash"))

# A high-throughput car-wash building is typically 300-2 500 m2; a whole site
# including forecourt and canopy reaches ~6 000 m2. A footprint above this
# threshold is almost certainly a host structure (retail park, mall, hall) that
# merely contains the car wash, so it is flagged rather than reported silently.
AREA_OUTLIER_SQM = 10_000.0

# Tags that identify the *primary* occupant of a building. If one of these
# names a different business and the feature is not itself tagged as a car
# wash, the footprint belongs to that occupant, not to our operator.
OCCUPANT_NAME_TAGS = ("name", "operator", "brand", "official_name")


@dataclass(frozen=True)
class MatchConfig:
    building_radius_m: float = 80.0
    parking_radius_m: float = 120.0
    building_medium_distance_m: float = 30.0
    parking_adjacent_distance_m: float = 15.0
    operator_tag_radius_m: float = 60.0
    target_epsg: int = TARGET_EPSG


@dataclass
class Candidate:
    element: dict[str, Any]
    geometry: BaseGeometry          # already in target CRS
    validity: str
    notes: list[str]
    distance_m: float
    area_sqm: float
    contains_point: bool

    @property
    def tags(self) -> dict[str, Any]:
        return self.element.get("tags") or {}


def _is_car_wash(tags: dict[str, Any]) -> bool:
    return any(tags.get(key) == value for key, value in CAR_WASH_TAGS)


def _operator_name_match(tags: dict[str, Any], operator: str) -> bool:
    tokens = OPERATOR_NAME_TOKENS.get(operator)
    if not tokens:
        tokens = (normalize_token(operator),)
    haystack = normalize_token(
        " ".join(
            str(tags.get(key, ""))
            for key in ("name", "operator", "brand", "official_name", "alt_name")
        )
    )
    return any(token and token in haystack for token in tokens)


def _build_candidates(
    elements: list[dict[str, Any]],
    point: Point,
    predicate,
    source_epsg: int,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    for element in elements:
        if not predicate(element.get("tags") or {}):
            continue
        if element.get("type") == "node":
            continue
        raw = element_to_geometry(element)
        validation = validate_and_repair(raw)
        if not validation.ok:
            log.debug(
                "candidate_geometry_rejected",
                extra={
                    "feature": feature_id(element),
                    "status": validation.status,
                    "notes": validation.notes,
                },
            )
            continue
        projected = transform_geometry(validation.geometry, source_epsg)
        if projected.is_empty:
            continue
        candidates.append(
            Candidate(
                element=element,
                geometry=projected,
                validity=validation.status,
                notes=list(validation.notes),
                distance_m=round(projected.distance(point), 2),
                area_sqm=round(projected.area, 1),
                contains_point=projected.covers(point),
            )
        )
    # Deterministic ordering: containment first, then distance, then feature id.
    candidates.sort(
        key=lambda c: (not c.contains_point, c.distance_m, feature_id(c.element))
    )
    return candidates


def _to_match(
    candidate: Candidate,
    kind: str,
    method: str,
    confidence: str,
    extra_notes: list[str] | None = None,
) -> SpatialMatch:
    geometry = as_multipolygon(candidate.geometry)
    return SpatialMatch(
        feature_kind=kind,
        source="openstreetmap",
        source_url=feature_url(candidate.element),
        feature_id=feature_id(candidate.element),
        geometry_type=geometry.geom_type,
        match_method=method,
        match_confidence=confidence,
        distance_m=candidate.distance_m,
        area_sqm=round(geometry.area, 1),
        validity_status=candidate.validity,
        wkt_25832=geometry.wkt,
        tags=dict(candidate.tags),
        notes=candidate.notes + (extra_notes or []),
    )


def match_site(
    elements: list[dict[str, Any]],
    x_25832: float,
    y_25832: float,
    operator: str,
    config: MatchConfig | None = None,
    source_epsg: int = 4326,
) -> dict[str, Any]:
    """Apply the documented rules to one site's Overpass elements."""
    config = config or MatchConfig()
    if not is_metric_crs(config.target_epsg):
        raise ValueError(
            f"EPSG:{config.target_epsg} is not a metric CRS; area computation refused"
        )
    point = Point(x_25832, y_25832)

    buildings = _build_candidates(
        elements, point, lambda tags: "building" in tags, source_epsg
    )
    parkings = _build_candidates(
        elements, point, lambda tags: tags.get("amenity") == "parking", source_epsg
    )

    # ---- operator corroboration (tags only; never ownership) --------------
    operator_tag_match = False
    car_wash_feature: str | None = None
    for element in elements:
        tags = element.get("tags") or {}
        if not (_is_car_wash(tags) or _operator_name_match(tags, operator)):
            continue
        distance: float | None = None
        if element.get("type") == "node":
            lat, lon = element.get("lat"), element.get("lon")
            if lat is None or lon is None:
                continue
            node_point = transform_geometry(Point(float(lon), float(lat)), source_epsg)
            distance = node_point.distance(point)
        else:
            validation = validate_and_repair(element_to_geometry(element))
            if not validation.ok:
                continue
            distance = transform_geometry(validation.geometry, source_epsg).distance(point)
        if distance is not None and distance <= config.operator_tag_radius_m:
            if _operator_name_match(tags, operator) or _is_car_wash(tags):
                operator_tag_match = operator_tag_match or _operator_name_match(
                    tags, operator
                )
                car_wash_feature = car_wash_feature or feature_id(element)

    # ---- building rules ---------------------------------------------------
    building: SpatialMatch | None = None
    if buildings:
        best = buildings[0]
        if best.contains_point:
            building = _to_match(best, "building", "point_in_building", "high")
        elif best.distance_m <= config.building_medium_distance_m:
            building = _to_match(
                best,
                "building",
                f"nearest_building_le_{int(config.building_medium_distance_m)}m",
                "medium",
            )
        elif best.distance_m <= config.building_radius_m:
            building = _to_match(
                best,
                "building",
                f"nearest_building_le_{int(config.building_radius_m)}m",
                "low",
            )
    quality_flags: list[str] = []
    if building is not None:
        if _is_car_wash(building.tags):
            building.notes.append("matched_building_tagged_car_wash")
        if _operator_name_match(building.tags, operator):
            building.notes.append("matched_building_name_matches_operator")
            operator_tag_match = True

        # --- host-structure detection ------------------------------------
        # The geometric statement ("the point lies inside this polygon") stays
        # true and keeps its confidence; what is added here is the semantic
        # caveat that the polygon is not the operator's own structure.
        occupant = next(
            (
                str(building.tags[tag])
                for tag in OCCUPANT_NAME_TAGS
                if building.tags.get(tag)
            ),
            None,
        )
        attributed_elsewhere = bool(
            occupant
            and not _is_car_wash(building.tags)
            and not _operator_name_match(building.tags, operator)
        )
        if attributed_elsewhere:
            quality_flags.append("building_attributed_to_other_occupant")
            building.notes.append(
                f"osm_feature_named_{normalize_token(occupant).replace(' ', '_')}"
                "_not_the_operator"
            )
        if (building.area_sqm or 0) > AREA_OUTLIER_SQM:
            quality_flags.append("building_area_outlier_gt_10000_sqm")
            building.notes.append(
                "footprint_exceeds_plausible_car_wash_site_area_likely_host_structure"
            )

    # ---- parking rules ---------------------------------------------------
    parking: SpatialMatch | None = None
    if parkings:
        best = parkings[0]
        if best.contains_point:
            parking = _to_match(best, "parking", "point_in_parking", "high")
        elif best.distance_m <= config.parking_adjacent_distance_m:
            parking = _to_match(
                best,
                "parking",
                f"adjacent_parking_le_{int(config.parking_adjacent_distance_m)}m",
                "medium",
            )
        elif best.distance_m <= config.parking_radius_m:
            parking = _to_match(
                best,
                "parking",
                f"nearby_parking_le_{int(config.parking_radius_m)}m",
                "low",
            )
    if parking is not None:
        parking.notes.append("proximity_does_not_prove_ownership")

    parking_total = round(sum(c.area_sqm for c in parkings), 1) if parkings else None

    return {
        "building": building,
        "parking": parking,
        "building_candidates": len(buildings),
        "parking_candidates": len(parkings),
        "parking_area_total_sqm": parking_total,
        "osm_operator_tag_match": operator_tag_match,
        "osm_car_wash_feature_id": car_wash_feature,
        "quality_flags": quality_flags,
        "rejected_geometries": sum(
            1
            for element in elements
            if (element.get("type") != "node")
            and not validate_and_repair(element_to_geometry(element)).ok
        ),
    }
