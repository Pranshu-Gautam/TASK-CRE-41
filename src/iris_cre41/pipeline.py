"""Stage orchestration.

Five stages, each reading and writing a JSON artefact under ``data/`` so that
any stage can be re-run, inspected or replaced in isolation:

    collect -> normalize -> geocode -> enrich -> load

Counts are measured at every boundary and written to ``samples/`` as execution
evidence.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from shapely.geometry import Point

from . import TASK_ID, __version__
from .collectors import CleanCarCollector, HttpFetcher, MrWashCollector
from .collectors.base import CollectionResult, Fetcher
from .config import Settings
from .crs import (
    InvalidCoordinateError,
    UnsupportedCRSError,
    in_germany,
    to_target,
)
from .enrich import MatchConfig, OverpassClient, match_site
from .geocode import GeocodeQuery, build_geocoder
from .geocode.cache import SqliteCache
from .httpclient import PoliteSession
from .logging_setup import get_logger
from .models import EnrichedPoi, GeocodeResult, NormalizedLocation, RawLocation, utcnow
from .normalize import deduplicate, normalize_location

log = get_logger(__name__)

RAW_ARTEFACT = "raw_locations.json"
NORMALIZED_ARTEFACT = "normalized_locations.json"
GEOCODED_ARTEFACT = "geocoded_locations.json"
ENRICHED_ARTEFACT = "enriched_pois.json"
STATS_ARTEFACT = "pipeline_stats.json"

OSM_ATTRIBUTION = "© OpenStreetMap contributors, ODbL 1.0"

OPERATOR_COLLECTORS = {
    "mrwash": MrWashCollector,
    "cleancar": CleanCarCollector,
}


@dataclass
class StageStats:
    stage: str
    started_at: dt.datetime
    finished_at: dt.datetime | None = None
    counts: dict[str, Any] = field(default_factory=dict)

    def finish(self, **counts: Any) -> "StageStats":
        self.finished_at = utcnow()
        self.counts.update(counts)
        return self

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "duration_s": (
                round((self.finished_at - self.started_at).total_seconds(), 2)
                if self.finished_at
                else None
            ),
            **self.counts,
        }


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return path


def _read_json(path: Path) -> Any:
    if not path.exists():
        raise FileNotFoundError(
            f"missing pipeline artefact {path}; run the preceding stage first"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def new_run_id(prefix: str) -> str:
    return f"{prefix}-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------
# Stage 1 — collect
# --------------------------------------------------------------------------

def run_collect(
    settings: Settings,
    operators: Sequence[str] = ("mrwash", "cleancar"),
    limit: int | None = None,
    fetcher: Fetcher | None = None,
    run_id: str | None = None,
    only: dict[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Fetch and parse the official location directories."""
    stats = StageStats("collect", utcnow())
    run_id = run_id or new_run_id("collect")

    session = None
    if fetcher is None:
        session = PoliteSession(
            user_agent=settings.user_agent,
            timeout=settings.http_timeout,
            max_retries=settings.http_max_retries,
            min_interval=settings.scrape_min_interval,
        )
        fetcher = HttpFetcher(session)

    results: list[CollectionResult] = []
    for operator in operators:
        collector_cls = OPERATOR_COLLECTORS.get(operator)
        if collector_cls is None:
            raise ValueError(
                f"unknown operator {operator!r}; known: {sorted(OPERATOR_COLLECTORS)}"
            )
        collector = collector_cls(
            fetcher=fetcher,
            limit=limit,
            only=(only or {}).get(operator),
        )
        result = collector.collect()
        log.info(
            "collector_finished",
            extra={
                "operator": result.operator,
                "locations": len(result.locations),
                "failures": len(result.failed_urls),
            },
        )
        results.append(result)

    payload = {
        "task_id": TASK_ID,
        "pipeline_version": __version__,
        "collection_run_id": run_id,
        "generated_at": utcnow().isoformat(),
        "user_agent": settings.user_agent,
        "http": session.stats() if session else {"mode": "fixture"},
        "collectors": [result.to_json() for result in results],
    }
    path = _write_json(settings.data_dir / RAW_ARTEFACT, payload)

    per_operator = {
        result.operator_slug: len(result.locations) for result in results
    }
    stats.finish(
        artefact=str(path),
        collection_run_id=run_id,
        locations_collected=sum(per_operator.values()),
        per_operator=per_operator,
        failures=sum(len(result.failed_urls) for result in results),
        pages_fetched=sum(
            int(result.stats.get("pages_fetched") or 0) for result in results
        ),
    )
    return stats.as_dict()


# --------------------------------------------------------------------------
# Stage 2 — normalize + deduplicate
# --------------------------------------------------------------------------

def run_normalize(settings: Settings) -> dict[str, Any]:
    stats = StageStats("normalize", utcnow())
    payload = _read_json(settings.data_dir / RAW_ARTEFACT)

    raw_locations: list[RawLocation] = []
    for collector in payload["collectors"]:
        for item in collector["locations"]:
            raw_locations.append(RawLocation.from_json(item))

    normalized = [normalize_location(raw) for raw in raw_locations]
    canonical, duplicates = deduplicate(normalized)

    flag_histogram: dict[str, int] = {}
    for record in normalized:
        for flag in record.data_quality_flags:
            flag_histogram[flag] = flag_histogram.get(flag, 0) + 1

    complete = [
        record
        for record in canonical
        if record.street and record.postal_code and record.city
    ]

    out = {
        "task_id": TASK_ID,
        "collection_run_id": payload.get("collection_run_id"),
        "generated_at": utcnow().isoformat(),
        "canonical": [record.to_json() for record in canonical],
        "duplicates": [record.to_json() for record in duplicates],
        "raw_payloads": {
            raw.source_record_id: raw.to_json() for raw in raw_locations
        },
    }
    path = _write_json(settings.data_dir / NORMALIZED_ARTEFACT, out)

    stats.finish(
        artefact=str(path),
        locations_input=len(raw_locations),
        normalized=len(normalized),
        canonical=len(canonical),
        duplicates_prevented=len(duplicates),
        address_complete=len(complete),
        data_quality_flags=flag_histogram,
    )
    return stats.as_dict()


# --------------------------------------------------------------------------
# Stage 3 — geocode + project
# --------------------------------------------------------------------------

def _project(poi: EnrichedPoi, geocode: GeocodeResult) -> None:
    """Validate and project a geocode result into EPSG:25832."""
    try:
        x, y = to_target(geocode.lat, geocode.lon, source_epsg=geocode.source_epsg or 4326)
    except (InvalidCoordinateError, UnsupportedCRSError) as exc:
        poi.data_quality_flags.append("coordinate_rejected")
        poi.notes.append(f"coordinate rejected: {exc}")
        geocode.status = "invalid_coordinates"
        geocode.error = str(exc)
        return
    poi.x_25832 = round(x, 3)
    poi.y_25832 = round(y, 3)
    poi.point_wkt_25832 = Point(poi.x_25832, poi.y_25832).wkt
    if not in_germany(geocode.lat, geocode.lon):
        poi.data_quality_flags.append("geocode_outside_de_envelope")


def run_geocode(settings: Settings, geocoder=None, limit: int | None = None) -> dict[str, Any]:
    stats = StageStats("geocode", utcnow())
    payload = _read_json(settings.data_dir / NORMALIZED_ARTEFACT)
    locations = [
        NormalizedLocation.from_json(item) for item in payload["canonical"]
    ]
    if limit:
        locations = locations[:limit]

    geocoder = geocoder or build_geocoder(settings)
    pois: list[EnrichedPoi] = []
    status_histogram: dict[str, int] = {}

    for location in locations:
        poi = EnrichedPoi(location=location)
        if not (location.street or location.postal_code or location.city):
            poi.geocode = GeocodeResult(
                status="error",
                provider=getattr(geocoder, "name", "unknown"),
                query="",
                error="no address components observed on source page",
                geocoded_at=utcnow(),
            )
            poi.data_quality_flags.append("address_unavailable")
        else:
            poi.geocode = geocoder.geocode(GeocodeQuery.from_location(location))

        geocode = poi.geocode
        status_histogram[geocode.status] = status_histogram.get(geocode.status, 0) + 1

        if geocode.usable:
            _project(poi, geocode)
            if geocode.ambiguous:
                poi.data_quality_flags.append("geocode_ambiguous")
            if geocode.postcode_match is False:
                poi.data_quality_flags.append("geocode_postcode_mismatch")
            if geocode.city_match is False:
                poi.data_quality_flags.append("geocode_city_mismatch")
            if geocode.name_operator_match is False:
                poi.data_quality_flags.append("geocode_name_not_operator")
            if geocode.precision and geocode.precision != "address":
                poi.data_quality_flags.append(
                    f"geocode_precision_{geocode.precision}"
                )
        else:
            poi.data_quality_flags.append(f"geocode_{geocode.status}")

        poi.enrichment_status = "not_attempted"
        poi.geometry_provenance = {
            "point": {
                "kind": "geocoded_address_point",
                "provider": geocode.provider,
                "licence": OSM_ATTRIBUTION if geocode.provider == "nominatim" else None,
                "status": geocode.status,
                "source_epsg": geocode.source_epsg,
                "stored_epsg": settings.target_epsg,
                "geocoded_at": geocode.geocoded_at.isoformat()
                if geocode.geocoded_at
                else None,
            }
        }
        pois.append(poi)

    out = {
        "task_id": TASK_ID,
        "collection_run_id": payload.get("collection_run_id"),
        "generated_at": utcnow().isoformat(),
        "geocoder": getattr(geocoder, "stats", lambda: {})(),
        "pois": [poi.to_json() for poi in pois],
    }
    path = _write_json(settings.data_dir / GEOCODED_ARTEFACT, out)

    projected = sum(1 for poi in pois if poi.point_wkt_25832)
    stats.finish(
        artefact=str(path),
        locations_input=len(locations),
        geocoded_ok=sum(1 for poi in pois if poi.geocode and poi.geocode.usable),
        projected_to_25832=projected,
        geocode_status=status_histogram,
        ambiguous=sum(
            1 for poi in pois if poi.geocode and poi.geocode.ambiguous
        ),
        postcode_mismatch=sum(
            1 for poi in pois if poi.geocode and poi.geocode.postcode_match is False
        ),
        city_mismatch=sum(
            1 for poi in pois if poi.geocode and poi.geocode.city_match is False
        ),
        precision={
            level: sum(
                1 for poi in pois if poi.geocode and poi.geocode.precision == level
            )
            for level in ("address", "street", "locality", "unknown")
        },
        provider_stats=getattr(geocoder, "stats", lambda: {})(),
    )
    return stats.as_dict()


# --------------------------------------------------------------------------
# Stage 4 — geospatial enrichment
# --------------------------------------------------------------------------

def _enrichment_status(poi: EnrichedPoi) -> str:
    if not poi.point_wkt_25832:
        return "failed"
    if poi.building and poi.parking:
        return "enriched"
    if poi.building or poi.parking:
        return "partial"
    return "point_only"


def run_enrich(
    settings: Settings,
    overpass: OverpassClient | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    stats = StageStats("enrich", utcnow())
    payload = _read_json(settings.data_dir / GEOCODED_ARTEFACT)
    pois = [EnrichedPoi.from_json(item) for item in payload["pois"]]
    if limit:
        pois = pois[:limit]

    if overpass is None:
        overpass = OverpassClient(
            session=PoliteSession(
                user_agent=settings.user_agent,
                timeout=max(60.0, settings.http_timeout),
                max_retries=settings.http_max_retries,
                min_interval=settings.overpass_min_interval,
            ),
            url=settings.overpass_url,
            min_interval=settings.overpass_min_interval,
            cache=SqliteCache(settings.overpass_cache),
        )

    config = MatchConfig(
        building_radius_m=settings.building_search_radius_m,
        parking_radius_m=settings.parking_search_radius_m,
        target_epsg=settings.target_epsg,
    )

    errors = 0
    skipped_precision = 0
    for poi in pois:
        if not poi.point_wkt_25832 or poi.geocode is None or poi.geocode.lat is None:
            poi.enrichment_status = "failed"
            poi.data_quality_flags.append("enrichment_skipped_no_point")
            continue
        # A locality-level geocode is a town centroid, potentially kilometres
        # from the site. Matching a footprint to it would manufacture a false
        # association, so enrichment is skipped and the point is kept as-is.
        if poi.geocode.precision == "locality":
            skipped_precision += 1
            poi.enrichment_status = "point_only"
            poi.data_quality_flags.append("enrichment_skipped_locality_precision")
            poi.notes.append(
                "polygon matching skipped: geocode precision is locality-level, "
                "which cannot be attributed to a specific building"
            )
            continue
        try:
            raw = overpass.fetch_site(
                poi.geocode.lat,
                poi.geocode.lon,
                settings.building_search_radius_m,
                settings.parking_search_radius_m,
            )
        except Exception as exc:
            errors += 1
            poi.enrichment_status = "point_only"
            poi.data_quality_flags.append("overpass_request_failed")
            poi.notes.append(f"overpass error: {type(exc).__name__}: {exc}")
            log.error(
                "enrichment_failed",
                extra={"poi_uid": poi.location.poi_uid, "error": str(exc)[:300]},
            )
            continue

        elements = raw.get("elements") or []
        match = match_site(
            elements,
            poi.x_25832,
            poi.y_25832,
            poi.location.operator,
            config=config,
            source_epsg=4326,
        )
        poi.building = match["building"]
        poi.parking = match["parking"]
        poi.parking_features_within_radius = match["parking_candidates"]
        poi.parking_area_total_sqm = match["parking_area_total_sqm"]
        poi.osm_operator_tag_match = match["osm_operator_tag_match"]
        poi.osm_car_wash_feature_id = match["osm_car_wash_feature_id"]
        poi.data_quality_flags.extend(match.get("quality_flags", []))

        # The confidence of a spatial match can never exceed the precision of
        # the point it was matched from.
        if poi.geocode.precision != "address":
            for spatial_match in (poi.building, poi.parking):
                if (
                    spatial_match is not None
                    and spatial_match.geometric_match_confidence in {"high", "medium"}
                ):
                    spatial_match.notes.append(
                        f"confidence_capped_geocode_precision_{poi.geocode.precision}"
                    )
                    spatial_match.geometric_match_confidence = "low"

        if poi.building is None:
            poi.data_quality_flags.append("osm_no_building_match")
        elif poi.building.geometric_match_confidence == "low":
            poi.data_quality_flags.append("building_match_low_confidence")
        if poi.parking is None:
            poi.data_quality_flags.append("osm_no_parking_match")

        provenance = dict(poi.geometry_provenance or {})
        provenance["osm_query"] = {
            "provider": "overpass-api.de",
            "licence": OSM_ATTRIBUTION,
            "building_radius_m": settings.building_search_radius_m,
            "parking_radius_m": settings.parking_search_radius_m,
            "elements_returned": len(elements),
            "building_candidates": match["building_candidates"],
            "parking_candidates": match["parking_candidates"],
            "geometries_rejected": match["rejected_geometries"],
            "queried_at": utcnow().isoformat(),
        }
        provenance["building"] = (
            {
                "kind": "osm_building_footprint",
                "feature_id": poi.building.feature_id,
                "source_url": poi.building.source_url,
                "licence": OSM_ATTRIBUTION,
                "match_method": poi.building.match_method,
                "geometric_match_confidence": poi.building.geometric_match_confidence,
                "operator_attribution_status": poi.building.operator_attribution_status,
                "operator_attribution_evidence": poi.building.operator_attribution_evidence,
                "operator_usable_area_verified": poi.building.operator_usable_area_verified,
                "validity": poi.building.validity_status,
                "area_crs": f"EPSG:{settings.target_epsg}",
                "ownership_verified": False,
            }
            if poi.building
            else {"status": "unavailable", "reason": "no OSM building matched the rules"}
        )
        provenance["parking"] = (
            {
                "kind": "osm_parking_polygon",
                "feature_id": poi.parking.feature_id,
                "source_url": poi.parking.source_url,
                "licence": OSM_ATTRIBUTION,
                "match_method": poi.parking.match_method,
                "geometric_match_confidence": poi.parking.geometric_match_confidence,
                "operator_attribution_status": poi.parking.operator_attribution_status,
                "operator_attribution_evidence": poi.parking.operator_attribution_evidence,
                "operator_usable_area_verified": poi.parking.operator_usable_area_verified,
                "validity": poi.parking.validity_status,
                "area_crs": f"EPSG:{settings.target_epsg}",
                "ownership_verified": False,
            }
            if poi.parking
            else {"status": "unavailable", "reason": "no OSM parking polygon matched"}
        )
        poi.geometry_provenance = provenance
        poi.enrichment_status = _enrichment_status(poi)

    out = {
        "task_id": TASK_ID,
        "collection_run_id": payload.get("collection_run_id"),
        "generated_at": utcnow().isoformat(),
        "overpass": overpass.stats(),
        "attribution": OSM_ATTRIBUTION,
        "pois": [poi.to_json() for poi in pois],
    }
    path = _write_json(settings.data_dir / ENRICHED_ARTEFACT, out)

    status_histogram: dict[str, int] = {}
    for poi in pois:
        status_histogram[poi.enrichment_status] = (
            status_histogram.get(poi.enrichment_status, 0) + 1
        )
    buildings = [poi for poi in pois if poi.building]
    confidence_histogram: dict[str, int] = {}
    attribution_histogram: dict[str, int] = {}
    for poi in buildings:
        key = poi.building.geometric_match_confidence
        confidence_histogram[key] = confidence_histogram.get(key, 0) + 1
        status = poi.building.operator_attribution_status
        attribution_histogram[status] = attribution_histogram.get(status, 0) + 1

    stats.finish(
        artefact=str(path),
        pois_input=len(pois),
        buildings_matched=len(buildings),
        parking_matched=sum(1 for poi in pois if poi.parking),
        polygonized=len(buildings),
        enrichment_status=status_histogram,
        building_geometric_confidence=confidence_histogram,
        building_operator_attribution=attribution_histogram,
        parking_operator_attribution={
            status: sum(
                1
                for poi in pois
                if poi.parking and poi.parking.operator_attribution_status == status
            )
            for status in ("operator_tagged", "third_party_named", "unverified")
        },
        operator_attributable_building_areas=sum(
            1
            for poi in pois
            if poi.building and poi.building.operator_attributable_area_sqm is not None
        ),
        operator_attributable_parking_areas=sum(
            1
            for poi in pois
            if poi.parking and poi.parking.operator_attributable_area_sqm is not None
        ),
        osm_operator_tag_matches=sum(1 for poi in pois if poi.osm_operator_tag_match),
        building_area_outliers=sum(
            1
            for poi in pois
            if "building_area_outlier_gt_10000_sqm" in poi.data_quality_flags
        ),
        buildings_attributed_to_other_occupant=sum(
            1
            for poi in pois
            if "building_attributed_to_other_occupant" in poi.data_quality_flags
        ),
        overpass_errors=errors,
        skipped_locality_precision=skipped_precision,
        building_area_sqm_total=round(
            sum(poi.building.area_sqm or 0 for poi in buildings), 1
        ),
        operator_attributable_building_area_sqm_total=round(
            sum(
                poi.building.operator_attributable_area_sqm or 0
                for poi in buildings
            ),
            1,
        ),
        provider_stats=overpass.stats(),
    )
    return stats.as_dict()


# --------------------------------------------------------------------------
# Stage 5 — load
# --------------------------------------------------------------------------

def run_load(settings: Settings, load_run_id: str | None = None) -> dict[str, Any]:
    from .db import create_staging_engine, create_staging_objects, ensure_postgis
    from .db.loader import load_pois, load_raw_observations, record_run, table_counts

    stats = StageStats("load", utcnow())
    payload = _read_json(settings.data_dir / ENRICHED_ARTEFACT)
    normalized_payload = _read_json(settings.data_dir / NORMALIZED_ARTEFACT)
    pois = [EnrichedPoi.from_json(item) for item in payload["pois"]]
    collection_run_id = payload.get("collection_run_id") or "unknown"
    load_run_id = load_run_id or new_run_id("load")

    engine, guard = create_staging_engine(
        settings.require_db_uri(), settings.staging_schema
    )
    capabilities = ensure_postgis(engine)
    metadata = create_staging_objects(engine, settings.staging_schema)

    load_stats = load_pois(engine, metadata, pois, collection_run_id, load_run_id)

    observations = [
        NormalizedLocation.from_json(item)
        for item in normalized_payload["canonical"] + normalized_payload["duplicates"]
    ]
    raw_payloads = {
        NormalizedLocation.from_json(item).source_uid: item
        for item in normalized_payload["canonical"] + normalized_payload["duplicates"]
    }
    raw_rows = load_raw_observations(
        engine, metadata, observations, raw_payloads, collection_run_id
    )

    counts = table_counts(engine, metadata)
    stats.finish(
        load_run_id=load_run_id,
        collection_run_id=collection_run_id,
        locations_processed=load_stats.processed,
        load=load_stats.as_dict(),
        raw_observations_loaded=raw_rows,
        duplicates_prevented=len(normalized_payload["duplicates"]),
        table_counts=counts,
        database=capabilities,
        schema_guard=guard.stats(),
    )
    record_run(
        engine,
        metadata,
        run_id=load_run_id,
        stage="load",
        started_at=stats.started_at,
        finished_at=stats.finished_at,
        stats=stats.as_dict(),
    )
    engine.dispose()
    return stats.as_dict()


# --------------------------------------------------------------------------
# Full pipeline
# --------------------------------------------------------------------------

def run_all(
    settings: Settings,
    operators: Sequence[str] = ("mrwash", "cleancar"),
    limit: int | None = None,
    skip_load: bool = False,
    fetcher: Fetcher | None = None,
    geocoder=None,
) -> dict[str, Any]:
    started = utcnow()
    results: dict[str, Any] = {
        "task_id": TASK_ID,
        "pipeline_version": __version__,
        "started_at": started.isoformat(),
        "stages": {},
    }
    results["stages"]["collect"] = run_collect(
        settings, operators=operators, limit=limit, fetcher=fetcher
    )
    results["stages"]["normalize"] = run_normalize(settings)
    results["stages"]["geocode"] = run_geocode(settings, geocoder=geocoder)
    results["stages"]["enrich"] = run_enrich(settings)
    if not skip_load:
        results["stages"]["load"] = run_load(settings)
    results["finished_at"] = utcnow().isoformat()
    results["duration_s"] = round((utcnow() - started).total_seconds(), 2)
    _write_json(settings.data_dir / STATS_ARTEFACT, results)
    return results
