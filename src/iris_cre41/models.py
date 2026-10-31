"""Data contracts between pipeline stages.

Each stage reads and writes plain dataclasses that serialise to JSON, so every
intermediate artefact on disk is auditable and each stage is independently
re-runnable.

Provenance vocabulary used throughout (and in the staging table):

``observed``   value read verbatim from the operator's official website
``derived``    deterministic transform of an observed value (normalisation)
``geocoded``   coordinate produced by an external geocoding service
``inferred``   spatial match against third-party open data (OSM)
``unavailable`` not published by any consulted source — stored as NULL
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Any


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None


@dataclass
class RawLocation:
    """Verbatim observation from an operator website. No normalisation."""

    operator: str
    source_system: str
    source_record_id: str
    source_url: str
    retrieved_at: dt.datetime
    location_name_raw: str | None = None
    address_block_raw: str | None = None
    street_raw: str | None = None
    postal_code_raw: str | None = None
    city_raw: str | None = None
    country_raw: str | None = "DE"
    phone_raw: str | None = None
    email_raw: str | None = None
    opening_hours_raw: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    extraction_warnings: list[str] = field(default_factory=list)
    html_sha256: str | None = None

    def to_json(self) -> dict[str, Any]:
        out = asdict(self)
        out["retrieved_at"] = iso(self.retrieved_at)
        return out

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "RawLocation":
        data = dict(data)
        data["retrieved_at"] = dt.datetime.fromisoformat(data["retrieved_at"])
        return cls(**data)


@dataclass
class NormalizedLocation:
    """Deterministically normalised record with stable identifiers."""

    poi_uid: str
    source_uid: str
    dedup_key: str
    operator: str
    operator_slug: str
    location_name: str | None
    location_name_raw: str | None
    street: str | None
    house_number: str | None
    postal_code: str | None
    city: str | None
    country: str
    address_raw: str | None
    address_normalized: str | None
    phone: str | None = None
    email: str | None = None
    opening_hours_raw: str | None = None
    source_system: str = ""
    source_record_id: str = ""
    source_url: str = ""
    retrieved_at: dt.datetime | None = None
    html_sha256: str | None = None
    source_attributes: dict[str, Any] = field(default_factory=dict)
    data_quality_flags: list[str] = field(default_factory=list)
    is_duplicate: bool = False
    duplicate_of_source_uid: str | None = None

    def to_json(self) -> dict[str, Any]:
        out = asdict(self)
        out["retrieved_at"] = iso(self.retrieved_at)
        return out

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "NormalizedLocation":
        data = dict(data)
        if data.get("retrieved_at"):
            data["retrieved_at"] = dt.datetime.fromisoformat(data["retrieved_at"])
        return cls(**data)


@dataclass
class GeocodeResult:
    """Outcome of one geocoding attempt. ``status`` is always meaningful."""

    status: str  # ok | not_found | ambiguous_resolved | invalid_coordinates | error
    provider: str
    query: str
    strategy: str = "structured"  # structured | freeform | cache | fixture
    lat: float | None = None
    lon: float | None = None
    source_epsg: int | None = None
    display_name: str | None = None
    match_type: str | None = None
    osm_type: str | None = None
    osm_id: str | None = None
    place_rank: int | None = None
    importance: float | None = None
    precision: str | None = None  # address | street | locality | unknown
    result_count: int = 0
    ambiguous: bool = False
    ambiguity_spread_m: float | None = None
    postcode_match: bool | None = None
    city_match: bool | None = None
    name_operator_match: bool | None = None
    returned_postcode: str | None = None
    returned_city: str | None = None
    alternatives_rejected: int = 0
    error: str | None = None
    geocoded_at: dt.datetime | None = None
    from_cache: bool = False

    @property
    def usable(self) -> bool:
        return self.status in {"ok", "ambiguous_resolved"} and None not in (
            self.lat,
            self.lon,
        )

    def to_json(self) -> dict[str, Any]:
        out = asdict(self)
        out["geocoded_at"] = iso(self.geocoded_at)
        return out

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "GeocodeResult":
        data = dict(data)
        if data.get("geocoded_at"):
            data["geocoded_at"] = dt.datetime.fromisoformat(data["geocoded_at"])
        return cls(**data)


@dataclass
class SpatialMatch:
    """A matched OSM geometry (building or parking) for one POI."""

    feature_kind: str  # building | parking
    source: str = "openstreetmap"
    source_url: str | None = None
    feature_id: str | None = None          # e.g. "way/123456"
    geometry_type: str | None = None       # Polygon | MultiPolygon
    match_method: str | None = None
    # ASSERTION 1 — geometry only: how strongly the geocoded point is tied to
    # this polygon. Says nothing about who occupies or controls it.
    geometric_match_confidence: str = "none"   # high | medium | low | none
    distance_m: float | None = None
    # Raw metric of the matched OSM feature, kept for provenance.
    area_sqm: float | None = None
    # ASSERTION 2 — how far the evidence supports attributing this polygon to
    # the operator. See enrich/matching.py::classify_operator_attribution.
    operator_attribution_status: str = "no_match"
    operator_attribution_evidence: str | None = None
    # Operator-specific area. NULL unless the polygon is positively linked to
    # the operator and is of plausible single-site size.
    operator_attributable_area_sqm: float | None = None
    # ASSERTION 3 — always False: no property/tenancy source was consulted.
    operator_usable_area_verified: bool = False
    validity_status: str | None = None     # valid | repaired | rejected_*
    wkt_25832: str | None = None
    tags: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SpatialMatch":
        return cls(**data)


@dataclass
class EnrichedPoi:
    """Fully processed POI, ready for persistence into iris_staging."""

    location: NormalizedLocation
    geocode: GeocodeResult | None = None
    x_25832: float | None = None
    y_25832: float | None = None
    point_wkt_25832: str | None = None
    building: SpatialMatch | None = None
    parking: SpatialMatch | None = None
    parking_features_within_radius: int | None = None
    parking_area_total_sqm: float | None = None
    osm_operator_tag_match: bool = False
    osm_car_wash_feature_id: str | None = None
    enrichment_status: str = "pending"  # enriched | partial | point_only | failed
    geometry_provenance: dict[str, Any] = field(default_factory=dict)
    data_quality_flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "location": self.location.to_json(),
            "geocode": self.geocode.to_json() if self.geocode else None,
            "x_25832": self.x_25832,
            "y_25832": self.y_25832,
            "point_wkt_25832": self.point_wkt_25832,
            "building": self.building.to_json() if self.building else None,
            "parking": self.parking.to_json() if self.parking else None,
            "parking_features_within_radius": self.parking_features_within_radius,
            "parking_area_total_sqm": self.parking_area_total_sqm,
            "osm_operator_tag_match": self.osm_operator_tag_match,
            "osm_car_wash_feature_id": self.osm_car_wash_feature_id,
            "enrichment_status": self.enrichment_status,
            "geometry_provenance": self.geometry_provenance,
            "data_quality_flags": self.data_quality_flags,
            "notes": self.notes,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "EnrichedPoi":
        return cls(
            location=NormalizedLocation.from_json(data["location"]),
            geocode=GeocodeResult.from_json(data["geocode"]) if data.get("geocode") else None,
            x_25832=data.get("x_25832"),
            y_25832=data.get("y_25832"),
            point_wkt_25832=data.get("point_wkt_25832"),
            building=SpatialMatch.from_json(data["building"]) if data.get("building") else None,
            parking=SpatialMatch.from_json(data["parking"]) if data.get("parking") else None,
            parking_features_within_radius=data.get("parking_features_within_radius"),
            parking_area_total_sqm=data.get("parking_area_total_sqm"),
            osm_operator_tag_match=data.get("osm_operator_tag_match", False),
            osm_car_wash_feature_id=data.get("osm_car_wash_feature_id"),
            enrichment_status=data.get("enrichment_status", "pending"),
            geometry_provenance=data.get("geometry_provenance", {}),
            data_quality_flags=data.get("data_quality_flags", []),
            notes=data.get("notes", []),
        )
