"""Idempotent persistence into the staging tables.

The upsert is keyed on ``poi_uid`` (stable hash of the source natural key) and
only rewrites a row when its content hash changed, which makes a second
identical run a no-op: zero inserts, zero updates, zero duplicates.

``RETURNING (xmax = 0)`` is PostgreSQL's standard way of telling an INSERT
apart from an ON CONFLICT UPDATE, so the counts reported are measured, not
assumed.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from geoalchemy2.elements import WKTElement
from sqlalchemy import MetaData, Table, func, literal_column, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from .. import TASK_ID, __version__
from ..logging_setup import get_logger
from ..models import EnrichedPoi, NormalizedLocation
from .schema import (
    COMMERCIAL_POI_TABLE,
    LOAD_RUN_TABLE,
    RAW_OBSERVATION_TABLE,
    TARGET_SRID,
)

log = get_logger(__name__)

# Columns excluded from the content hash: timestamps and run ids change on
# every execution and would defeat change detection.
_HASH_EXCLUDED = {
    "row_hash",
    "first_seen_at",
    "last_seen_at",
    "updated_at",
    "load_run_id",
    "collection_run_id",
    "retrieved_at",
    "geocoded_at",
    # Excluded on purpose: VERIFIED 2026-10-31 that mrwash.de embeds a
    # per-response widget id, so the page hash differs between two identical
    # requests. Including it would report a content change on every run.
    # It is still stored in source_html_sha256 as provenance.
    "source_html_sha256",
}


@dataclass
class LoadStats:
    processed: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "processed": self.processed,
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "failed": self.failed,
            "failures": self.failures,
        }


def _json_safe(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, WKTElement):
        return value.data
    return value


def compute_row_hash(row: dict[str, Any]) -> str:
    payload = {
        key: _json_safe(value)
        for key, value in sorted(row.items())
        if key not in _HASH_EXCLUDED
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _wkt(value: str | None) -> WKTElement | None:
    if not value:
        return None
    return WKTElement(value, srid=TARGET_SRID)


def build_poi_row(poi: EnrichedPoi, collection_run_id: str, load_run_id: str) -> dict[str, Any]:
    """Project an :class:`EnrichedPoi` onto the staging table columns."""
    location = poi.location
    geocode = poi.geocode
    building = poi.building
    parking = poi.parking
    attributes = dict(location.source_attributes or {})

    row: dict[str, Any] = {
        "poi_uid": location.poi_uid,
        "source_uid": location.source_uid,
        "dedup_key": location.dedup_key,
        "operator": location.operator,
        "operator_slug": location.operator_slug,
        "location_name": location.location_name,
        "location_name_raw": location.location_name_raw,
        "street": location.street,
        "house_number": location.house_number,
        "postal_code": location.postal_code,
        "city": location.city,
        "country": location.country or "DE",
        "address_raw": location.address_raw,
        "address_normalized": location.address_normalized,
        "phone": location.phone,
        "email": location.email,
        "opening_hours_raw": location.opening_hours_raw,
        "max_vehicle_height_m": attributes.get("max_vehicle_height_m"),
        "max_vehicle_width_m": attributes.get("max_vehicle_width_m"),
        "source_attributes": attributes or None,
        "source_system": location.source_system,
        "source_record_id": location.source_record_id,
        "source_url": location.source_url,
        "retrieved_at": location.retrieved_at,
        "source_html_sha256": location.html_sha256,
        # geocoding
        "geocode_provider": geocode.provider if geocode else None,
        "geocode_status": geocode.status if geocode else "not_attempted",
        "geocode_strategy": geocode.strategy if geocode else None,
        "geocode_query": geocode.query if geocode else None,
        "geocode_lat": geocode.lat if geocode else None,
        "geocode_lon": geocode.lon if geocode else None,
        "geocode_srid": geocode.source_epsg if geocode else None,
        "geocode_display_name": geocode.display_name if geocode else None,
        "geocode_match_type": geocode.match_type if geocode else None,
        "geocode_osm_type": geocode.osm_type if geocode else None,
        "geocode_osm_id": geocode.osm_id if geocode else None,
        "geocode_place_rank": geocode.place_rank if geocode else None,
        "geocode_precision": geocode.precision if geocode else None,
        "geocode_importance": geocode.importance if geocode else None,
        "geocode_result_count": geocode.result_count if geocode else None,
        "geocode_ambiguous": geocode.ambiguous if geocode else None,
        "geocode_ambiguity_spread_m": geocode.ambiguity_spread_m if geocode else None,
        "geocode_postcode_match": geocode.postcode_match if geocode else None,
        "geocode_city_match": geocode.city_match if geocode else None,
        "geocode_name_operator_match": geocode.name_operator_match if geocode else None,
        "geocode_returned_postcode": geocode.returned_postcode if geocode else None,
        "geocode_returned_city": geocode.returned_city if geocode else None,
        "geocoded_at": geocode.geocoded_at if geocode else None,
        # projected point
        "geom": _wkt(poi.point_wkt_25832),
        "x_25832": poi.x_25832,
        "y_25832": poi.y_25832,
        # building
        "building_source": building.source if building else None,
        "building_source_url": building.source_url if building else None,
        "building_feature_id": building.feature_id if building else None,
        "building_geometry_type": building.geometry_type if building else None,
        "building_match_method": building.match_method if building else None,
        "building_match_confidence": building.match_confidence if building else "none",
        "building_distance_m": building.distance_m if building else None,
        "building_footprint_area_sqm": building.area_sqm if building else None,
        "building_geometry_validity": building.validity_status if building else None,
        "building_tags": (building.tags or None) if building else None,
        "building_geom": _wkt(building.wkt_25832) if building else None,
        # parking
        "parking_source": parking.source if parking else None,
        "parking_source_url": parking.source_url if parking else None,
        "parking_feature_id": parking.feature_id if parking else None,
        "parking_match_method": parking.match_method if parking else None,
        "parking_match_confidence": parking.match_confidence if parking else "none",
        "parking_distance_m": parking.distance_m if parking else None,
        "parking_area_sqm": parking.area_sqm if parking else None,
        "parking_geometry_validity": parking.validity_status if parking else None,
        "parking_geom": _wkt(parking.wkt_25832) if parking else None,
        "parking_features_within_radius": poi.parking_features_within_radius,
        "parking_area_total_sqm": poi.parking_area_total_sqm,
        "parking_ownership_verified": False,
        # status
        "osm_operator_tag_match": poi.osm_operator_tag_match,
        "osm_car_wash_feature_id": poi.osm_car_wash_feature_id,
        "enrichment_status": poi.enrichment_status,
        "geometry_provenance": poi.geometry_provenance or None,
        "data_quality_flags": sorted(
            set(location.data_quality_flags or []) | set(poi.data_quality_flags or [])
        )
        or None,
        "notes": "; ".join(poi.notes) if poi.notes else None,
        "collection_run_id": collection_run_id,
        "load_run_id": load_run_id,
    }
    row["row_hash"] = compute_row_hash(row)
    return row


def load_pois(
    engine: Engine,
    metadata: MetaData,
    pois: Sequence[EnrichedPoi],
    collection_run_id: str,
    load_run_id: str,
) -> LoadStats:
    """Upsert POIs. One transaction; per-row failures are isolated and counted."""
    table: Table = metadata.tables[
        f"{metadata.schema}.{COMMERCIAL_POI_TABLE}"
    ]
    stats = LoadStats()
    rows = [build_poi_row(poi, collection_run_id, load_run_id) for poi in pois]

    updatable = [
        column.name
        for column in table.columns
        if column.name not in {"poi_uid", "first_seen_at"}
    ]

    for row in rows:
        stats.processed += 1
        statement = pg_insert(table).values(**row)
        statement = statement.on_conflict_do_update(
            index_elements=[table.c.poi_uid],
            set_={
                **{name: statement.excluded[name] for name in updatable},
                "last_seen_at": func.now(),
                "updated_at": func.now(),
            },
            # Only rewrite when the content actually changed.
            where=table.c.row_hash != statement.excluded.row_hash,
        ).returning(
            table.c.poi_uid, literal_column("(xmax = 0)").label("was_insert")
        )
        try:
            with engine.begin() as connection:
                result = connection.execute(statement).fetchone()
        except SQLAlchemyError as exc:
            stats.failed += 1
            stats.failures.append(
                {"poi_uid": row["poi_uid"], "error": f"{type(exc).__name__}: {exc}"}
            )
            log.error(
                "poi_upsert_failed",
                extra={"poi_uid": row["poi_uid"], "error": str(exc)[:500]},
            )
            continue
        if result is None:
            # Conflict matched but the WHERE suppressed the update: unchanged.
            stats.unchanged += 1
        elif result.was_insert:
            stats.inserted += 1
        else:
            stats.updated += 1

    log.info("poi_load_complete", extra=stats.as_dict() | {"load_run_id": load_run_id})
    return stats


def load_raw_observations(
    engine: Engine,
    metadata: MetaData,
    locations: Iterable[NormalizedLocation],
    raw_payloads: dict[str, dict[str, Any]],
    collection_run_id: str,
) -> int:
    """Append raw observations (duplicates included) for provenance."""
    table: Table = metadata.tables[f"{metadata.schema}.{RAW_OBSERVATION_TABLE}"]
    count = 0
    with engine.begin() as connection:
        for location in locations:
            observation_id = hashlib.sha1(
                f"{location.source_uid}|{collection_run_id}".encode("utf-8")
            ).hexdigest()
            payload = raw_payloads.get(location.source_uid) or location.to_json()
            statement = pg_insert(table).values(
                observation_id=observation_id,
                source_uid=location.source_uid,
                operator=location.operator,
                source_system=location.source_system,
                source_record_id=location.source_record_id,
                source_url=location.source_url,
                retrieved_at=location.retrieved_at,
                payload=payload,
                html_sha256=(payload or {}).get("html_sha256"),
                is_duplicate=location.is_duplicate,
                duplicate_of_source_uid=location.duplicate_of_source_uid,
                collection_run_id=collection_run_id,
            )
            statement = statement.on_conflict_do_update(
                index_elements=[table.c.observation_id],
                set_={
                    "payload": statement.excluded.payload,
                    "is_duplicate": statement.excluded.is_duplicate,
                    "duplicate_of_source_uid": statement.excluded.duplicate_of_source_uid,
                },
            )
            connection.execute(statement)
            count += 1
    return count


def record_run(
    engine: Engine,
    metadata: MetaData,
    run_id: str,
    stage: str,
    started_at: dt.datetime,
    finished_at: dt.datetime | None,
    stats: dict[str, Any],
) -> None:
    """Write execution evidence into the staging run log."""
    table: Table = metadata.tables[f"{metadata.schema}.{LOAD_RUN_TABLE}"]
    load = stats.get("load", {}) if isinstance(stats.get("load"), dict) else {}
    values = {
        "run_id": run_id,
        "task_id": TASK_ID,
        "started_at": started_at,
        "finished_at": finished_at,
        "pipeline_version": __version__,
        "stage": stage,
        "locations_processed": stats.get("locations_processed"),
        "rows_inserted": load.get("inserted"),
        "rows_updated": load.get("updated"),
        "rows_unchanged": load.get("unchanged"),
        "rows_failed": load.get("failed"),
        "duplicates_prevented": stats.get("duplicates_prevented"),
        "stats": json.loads(json.dumps(stats, default=str)),
    }
    statement = pg_insert(table).values(**values)
    statement = statement.on_conflict_do_update(
        index_elements=[table.c.run_id],
        set_={key: statement.excluded[key] for key in values if key != "run_id"},
    )
    with engine.begin() as connection:
        connection.execute(statement)


def table_counts(engine: Engine, metadata: MetaData) -> dict[str, int]:
    out: dict[str, int] = {}
    with engine.connect() as connection:
        for name in (COMMERCIAL_POI_TABLE, RAW_OBSERVATION_TABLE, LOAD_RUN_TABLE):
            table = metadata.tables[f"{metadata.schema}.{name}"]
            out[name] = int(
                connection.execute(select(func.count()).select_from(table)).scalar() or 0
            )
    return out
