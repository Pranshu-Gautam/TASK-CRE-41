"""Staging schema definition.

!! ASSUMPTION REQUIRING CONFIRMATION FROM TARIQ !!
The official DDL for ``iris_staging.commercial_poi_staging`` was not supplied
with the task. This module therefore defines a *local* staging schema designed
to be a superset of the fields the task asks for, with explicit provenance
columns. It is NOT the production schema and must be reconciled before any
load into a shared environment. See docs/SCHEMA_MAPPING.md.

Design rules applied:
* every table lives in ``iris_staging`` (schema taken from configuration);
* ``poi_uid`` is the stable primary key, ``source_uid`` and ``dedup_key`` are
  additionally unique so duplicates cannot be inserted even by accident;
* geometries are ``EPSG:25832`` with CHECK constraints on SRID and emptiness;
* geometry columns are nullable on purpose: a location whose geocoding failed
  is still recorded, with NULL geometry and an explicit status, rather than
  being dropped or given an invented coordinate.
"""

from __future__ import annotations

from geoalchemy2 import Geometry
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

COMMERCIAL_POI_TABLE = "commercial_poi_staging"
RAW_OBSERVATION_TABLE = "commercial_poi_raw_staging"
LOAD_RUN_TABLE = "commercial_poi_load_run"

TARGET_SRID = 25832

GEOCODE_STATUSES = (
    "ok",
    "ambiguous_resolved",
    "not_found",
    "invalid_coordinates",
    "error",
    "not_attempted",
)
ENRICHMENT_STATUSES = ("enriched", "partial", "point_only", "failed", "not_attempted")
CONFIDENCES = ("high", "medium", "low", "none")


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def build_metadata(schema: str = "iris_staging") -> MetaData:
    """Build the staging MetaData bound to ``schema``."""
    metadata = MetaData(schema=schema)

    Table(
        COMMERCIAL_POI_TABLE,
        metadata,
        # -- identity ------------------------------------------------------
        Column("poi_uid", Text, primary_key=True,
               comment="Stable primary id: 'cre41-' + sha1(source_uid)[:20]"),
        Column("source_uid", Text, nullable=False,
               comment="Natural key: <operator>:<source system>:<source record id>"),
        Column("dedup_key", Text, nullable=False,
               comment="sha1 of operator + postcode + normalised street + house no."),
        # -- operator / name ----------------------------------------------
        Column("operator", Text, nullable=False),
        Column("operator_slug", Text, nullable=False),
        Column("location_name", Text),
        Column("location_name_raw", Text),
        # -- address (observed on the official site) -----------------------
        Column("street", Text),
        Column("house_number", Text),
        Column("postal_code", String(5)),
        Column("city", Text),
        Column("country", String(2), nullable=False, server_default=text("'DE'")),
        Column("address_raw", Text, comment="Verbatim address block as published"),
        Column("address_normalized", Text),
        # -- verified source metadata --------------------------------------
        Column("phone", Text),
        Column("email", Text),
        Column("opening_hours_raw", Text),
        Column("max_vehicle_height_m", Numeric(4, 2),
               comment="Published entry clearance (observed), not derived"),
        Column("max_vehicle_width_m", Numeric(4, 2)),
        Column("source_attributes", JSONB,
               comment="All other verbatim attributes observed on the source page"),
        # -- provenance ----------------------------------------------------
        Column("source_system", Text, nullable=False),
        Column("source_record_id", Text, nullable=False),
        Column("source_url", Text, nullable=False),
        Column("retrieved_at", DateTime(timezone=True), nullable=False),
        Column("source_html_sha256", Text),
        # -- geocoding -----------------------------------------------------
        Column("geocode_provider", Text),
        Column("geocode_status", Text, nullable=False,
               server_default=text("'not_attempted'")),
        Column("geocode_strategy", Text),
        Column("geocode_query", Text),
        Column("geocode_lat", Float, comment="Original WGS84 latitude as returned"),
        Column("geocode_lon", Float, comment="Original WGS84 longitude as returned"),
        Column("geocode_srid", Integer, comment="SRID of geocode_lat/lon (4326)"),
        Column("geocode_display_name", Text),
        Column("geocode_match_type", Text),
        Column("geocode_osm_type", Text),
        Column("geocode_osm_id", Text),
        Column("geocode_place_rank", Integer),
        Column("geocode_precision", Text,
               comment="address | street | locality | unknown — how precisely the "
                       "geocoder pinned the address"),
        Column("geocode_importance", Float),
        Column("geocode_result_count", Integer),
        Column("geocode_ambiguous", Boolean),
        Column("geocode_ambiguity_spread_m", Float),
        Column("geocode_postcode_match", Boolean),
        Column("geocode_city_match", Boolean),
        Column("geocode_name_operator_match", Boolean),
        Column("geocode_returned_postcode", Text),
        Column("geocode_returned_city", Text),
        Column("geocoded_at", DateTime(timezone=True)),
        # -- projected point (mandated CRS) --------------------------------
        Column("geom", Geometry("POINT", srid=TARGET_SRID, spatial_index=False),
               comment="Geocoded address point projected to EPSG:25832"),
        Column("x_25832", Float),
        Column("y_25832", Float),
        # -- building footprint (OSM, spatially inferred) -------------------
        Column("building_source", Text),
        Column("building_source_url", Text),
        Column("building_feature_id", Text),
        Column("building_geometry_type", Text),
        Column("building_match_method", Text),
        Column("building_match_confidence", Text, server_default=text("'none'")),
        Column("building_distance_m", Float),
        Column("building_footprint_area_sqm", Numeric(14, 1),
               comment="ST_Area in EPSG:25832. Gross footprint, NOT usable PV area"),
        Column("building_geometry_validity", Text),
        Column("building_tags", JSONB),
        Column("building_geom",
               Geometry("MULTIPOLYGON", srid=TARGET_SRID, spatial_index=False)),
        # -- parking (OSM, spatially inferred) ------------------------------
        Column("parking_source", Text),
        Column("parking_source_url", Text),
        Column("parking_feature_id", Text),
        Column("parking_match_method", Text),
        Column("parking_match_confidence", Text, server_default=text("'none'")),
        Column("parking_distance_m", Float),
        Column("parking_area_sqm", Numeric(14, 1)),
        Column("parking_geometry_validity", Text),
        Column("parking_geom",
               Geometry("MULTIPOLYGON", srid=TARGET_SRID, spatial_index=False)),
        Column("parking_features_within_radius", Integer),
        Column("parking_area_total_sqm", Numeric(14, 1),
               comment="Sum of all parking polygons in the search radius; "
                       "vicinity supply, NOT operator-owned"),
        Column("parking_ownership_verified", Boolean, nullable=False,
               server_default=text("false"),
               comment="Always false: no ownership source was available"),
        # -- status / audit ------------------------------------------------
        Column("osm_operator_tag_match", Boolean, server_default=text("false")),
        Column("osm_car_wash_feature_id", Text),
        Column("enrichment_status", Text, nullable=False,
               server_default=text("'not_attempted'")),
        Column("geometry_provenance", JSONB),
        Column("data_quality_flags", JSONB),
        Column("notes", Text),
        Column("row_hash", Text, nullable=False,
               comment="Content hash; drives change detection on upsert"),
        Column("collection_run_id", Text),
        Column("load_run_id", Text),
        Column("first_seen_at", DateTime(timezone=True), nullable=False,
               server_default=func.now()),
        Column("last_seen_at", DateTime(timezone=True), nullable=False,
               server_default=func.now()),
        Column("updated_at", DateTime(timezone=True), nullable=False,
               server_default=func.now()),
        # -- constraints ---------------------------------------------------
        UniqueConstraint("source_uid", name=f"uq_{COMMERCIAL_POI_TABLE}_source_uid"),
        UniqueConstraint("dedup_key", name=f"uq_{COMMERCIAL_POI_TABLE}_dedup_key"),
        CheckConstraint(
            "postal_code IS NULL OR postal_code ~ '^[0-9]{5}$'",
            name="ck_poi_postal_code_format",
        ),
        CheckConstraint("country = 'DE'", name="ck_poi_country_de"),
        CheckConstraint(
            f"geocode_status IN ({_quoted(GEOCODE_STATUSES)})",
            name="ck_poi_geocode_status",
        ),
        CheckConstraint(
            f"enrichment_status IN ({_quoted(ENRICHMENT_STATUSES)})",
            name="ck_poi_enrichment_status",
        ),
        CheckConstraint(
            f"building_match_confidence IN ({_quoted(CONFIDENCES)})",
            name="ck_poi_building_confidence",
        ),
        CheckConstraint(
            f"parking_match_confidence IN ({_quoted(CONFIDENCES)})",
            name="ck_poi_parking_confidence",
        ),
        CheckConstraint(
            f"geom IS NULL OR ST_SRID(geom) = {TARGET_SRID}",
            name="ck_poi_geom_srid_25832",
        ),
        CheckConstraint(
            "geom IS NULL OR NOT ST_IsEmpty(geom)", name="ck_poi_geom_not_empty"
        ),
        CheckConstraint(
            f"building_geom IS NULL OR ST_SRID(building_geom) = {TARGET_SRID}",
            name="ck_poi_building_geom_srid_25832",
        ),
        CheckConstraint(
            "building_geom IS NULL OR "
            "(NOT ST_IsEmpty(building_geom) AND ST_IsValid(building_geom))",
            name="ck_poi_building_geom_valid",
        ),
        CheckConstraint(
            f"parking_geom IS NULL OR ST_SRID(parking_geom) = {TARGET_SRID}",
            name="ck_poi_parking_geom_srid_25832",
        ),
        CheckConstraint(
            "parking_geom IS NULL OR "
            "(NOT ST_IsEmpty(parking_geom) AND ST_IsValid(parking_geom))",
            name="ck_poi_parking_geom_valid",
        ),
        CheckConstraint(
            "building_footprint_area_sqm IS NULL OR building_footprint_area_sqm >= 0",
            name="ck_poi_building_area_non_negative",
        ),
        CheckConstraint(
            "parking_area_sqm IS NULL OR parking_area_sqm >= 0",
            name="ck_poi_parking_area_non_negative",
        ),
        CheckConstraint(
            "(building_geom IS NULL) = (building_footprint_area_sqm IS NULL)",
            name="ck_poi_building_area_requires_geometry",
        ),
        CheckConstraint(
            "geocode_precision IS NULL OR geocode_precision IN "
            "('address', 'street', 'locality', 'unknown')",
            name="ck_poi_geocode_precision",
        ),
        CheckConstraint(
            "geocode_lat IS NULL OR (geocode_lat BETWEEN -90 AND 90)",
            name="ck_poi_lat_range",
        ),
        CheckConstraint(
            "geocode_lon IS NULL OR (geocode_lon BETWEEN -180 AND 180)",
            name="ck_poi_lon_range",
        ),
        comment="TASK-CRE-41 staging table for car-wash commercial POIs "
                "(LOCAL schema; official DDL to be confirmed with Tariq)",
        schema=schema,
    )

    Table(
        RAW_OBSERVATION_TABLE,
        metadata,
        Column("observation_id", Text, primary_key=True,
               comment="sha1 of source_uid + retrieval batch"),
        Column("source_uid", Text, nullable=False),
        Column("operator", Text, nullable=False),
        Column("source_system", Text, nullable=False),
        Column("source_record_id", Text, nullable=False),
        Column("source_url", Text, nullable=False),
        Column("retrieved_at", DateTime(timezone=True), nullable=False),
        Column("payload", JSONB, nullable=False,
               comment="Verbatim extracted record, pre-normalisation"),
        Column("html_sha256", Text),
        Column("is_duplicate", Boolean, nullable=False, server_default=text("false")),
        Column("duplicate_of_source_uid", Text),
        Column("collection_run_id", Text, nullable=False),
        Column("loaded_at", DateTime(timezone=True), nullable=False,
               server_default=func.now()),
        comment="Immutable raw observations, including records dropped as duplicates",
        schema=schema,
    )

    Table(
        LOAD_RUN_TABLE,
        metadata,
        Column("run_id", Text, primary_key=True),
        Column("task_id", Text, nullable=False, server_default=text("'TASK-CRE-41'")),
        Column("started_at", DateTime(timezone=True), nullable=False),
        Column("finished_at", DateTime(timezone=True)),
        Column("pipeline_version", Text),
        Column("stage", Text, nullable=False),
        Column("locations_processed", Integer),
        Column("rows_inserted", Integer),
        Column("rows_updated", Integer),
        Column("rows_unchanged", Integer),
        Column("rows_failed", Integer),
        Column("duplicates_prevented", Integer),
        Column("stats", JSONB),
        Column("id_seq", BigInteger, autoincrement=False),
        comment="Execution evidence: one row per load run",
        schema=schema,
    )

    # Spatial + lookup indexes (created explicitly so names are stable).
    poi = metadata.tables[f"{schema}.{COMMERCIAL_POI_TABLE}"]
    Index(f"ix_{COMMERCIAL_POI_TABLE}_geom", poi.c.geom, postgresql_using="gist")
    Index(
        f"ix_{COMMERCIAL_POI_TABLE}_building_geom",
        poi.c.building_geom,
        postgresql_using="gist",
    )
    Index(
        f"ix_{COMMERCIAL_POI_TABLE}_parking_geom",
        poi.c.parking_geom,
        postgresql_using="gist",
    )
    Index(f"ix_{COMMERCIAL_POI_TABLE}_operator", poi.c.operator_slug)
    Index(f"ix_{COMMERCIAL_POI_TABLE}_postal_code", poi.c.postal_code)
    Index(f"ix_{COMMERCIAL_POI_TABLE}_geocode_status", poi.c.geocode_status)
    return metadata


def sync_staging_columns(engine, metadata: MetaData) -> dict[str, list[str]]:
    """Additively reconcile existing staging tables with the current model.

    A newer pipeline version may define columns that an older run already
    created the table without. Only ``ADD COLUMN`` is ever issued, only inside
    the staging schema: nothing is dropped, retyped or back-filled, so no data
    can be lost. Columns present in the database but unknown to the model are
    reported, never removed.
    """
    from sqlalchemy import inspect
    from sqlalchemy import text as sql_text
    from sqlalchemy.dialects import postgresql

    from ..logging_setup import get_logger

    log = get_logger(__name__)
    dialect = postgresql.dialect()
    inspector = inspect(engine)
    report: dict[str, list[str]] = {"added": [], "unknown_in_database": []}

    for table in metadata.sorted_tables:
        if not inspector.has_table(table.name, schema=metadata.schema):
            continue
        existing = {
            column["name"]
            for column in inspector.get_columns(table.name, schema=metadata.schema)
        }
        for column in table.columns:
            if column.name in existing:
                continue
            column_type = column.type.compile(dialect=dialect)
            statement = (
                f'ALTER TABLE "{metadata.schema}"."{table.name}" '
                f'ADD COLUMN IF NOT EXISTS "{column.name}" {column_type}'
            )
            with engine.begin() as connection:
                connection.execute(sql_text(statement))
            report["added"].append(f"{table.name}.{column.name}")
            log.warning(
                "staging_column_added",
                extra={
                    "table": f"{metadata.schema}.{table.name}",
                    "column": column.name,
                    "type": column_type,
                },
            )
        for name in sorted(existing - {c.name for c in table.columns}):
            report["unknown_in_database"].append(f"{table.name}.{name}")
            log.warning(
                "staging_column_unknown_to_model",
                extra={"table": f"{metadata.schema}.{table.name}", "column": name},
            )
    return report


def create_staging_objects(engine, schema: str = "iris_staging") -> MetaData:
    """Create the schema and all staging tables, then additively sync columns.

    Idempotent: safe to run on every start.
    """
    from sqlalchemy import text as sql_text

    metadata = build_metadata(schema)
    with engine.begin() as connection:
        connection.execute(sql_text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    metadata.create_all(engine, checkfirst=True)
    sync_staging_columns(engine, metadata)
    return metadata
