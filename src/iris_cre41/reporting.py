"""Output samples and database verification evidence."""

from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path
from typing import Any

from sqlalchemy import text

from . import TASK_ID, __version__
from .config import Settings
from .logging_setup import get_logger
from .models import EnrichedPoi
from .pipeline import ENRICHED_ARTEFACT, NORMALIZED_ARTEFACT, _read_json

log = get_logger(__name__)

SAMPLE_CSV_COLUMNS = [
    "poi_uid",
    "operator",
    "location_name",
    "street",
    "house_number",
    "postal_code",
    "city",
    "country",
    "source_url",
    "retrieved_at",
    "geocode_status",
    "geocode_lat",
    "geocode_lon",
    "geocode_precision",
    "x_25832",
    "y_25832",
    "geocode_postcode_match",
    "geocode_city_match",
    "building_feature_id",
    "building_match_method",
    "building_geometric_match_confidence",
    "building_distance_m",
    "building_footprint_area_sqm",
    "building_operator_attribution_status",
    "building_operator_attributable_area_sqm",
    "building_operator_attribution_evidence",
    "building_geometry_validity",
    "parking_feature_id",
    "parking_match_method",
    "parking_geometric_match_confidence",
    "parking_area_sqm",
    "parking_operator_attribution_status",
    "parking_operator_attributable_area_sqm",
    "parking_area_total_sqm",
    "operator_usable_area_verified",
    "osm_operator_tag_match",
    "enrichment_status",
    "data_quality_flags",
]


def _row_for_csv(poi: EnrichedPoi) -> dict[str, Any]:
    location = poi.location
    geocode = poi.geocode
    building = poi.building
    parking = poi.parking
    return {
        "poi_uid": location.poi_uid,
        "operator": location.operator,
        "location_name": location.location_name,
        "street": location.street,
        "house_number": location.house_number,
        "postal_code": location.postal_code,
        "city": location.city,
        "country": location.country,
        "source_url": location.source_url,
        "retrieved_at": location.retrieved_at.isoformat() if location.retrieved_at else None,
        "geocode_status": geocode.status if geocode else None,
        "geocode_lat": geocode.lat if geocode else None,
        "geocode_lon": geocode.lon if geocode else None,
        "geocode_precision": geocode.precision if geocode else None,
        "x_25832": poi.x_25832,
        "y_25832": poi.y_25832,
        "geocode_postcode_match": geocode.postcode_match if geocode else None,
        "geocode_city_match": geocode.city_match if geocode else None,
        "building_feature_id": building.feature_id if building else None,
        "building_match_method": building.match_method if building else None,
        "building_geometric_match_confidence": (
            building.geometric_match_confidence if building else None
        ),
        "building_distance_m": building.distance_m if building else None,
        "building_footprint_area_sqm": building.area_sqm if building else None,
        "building_operator_attribution_status": (
            building.operator_attribution_status if building else "no_match"
        ),
        "building_operator_attributable_area_sqm": (
            building.operator_attributable_area_sqm if building else None
        ),
        "building_operator_attribution_evidence": (
            building.operator_attribution_evidence if building else None
        ),
        "building_geometry_validity": building.validity_status if building else None,
        "parking_feature_id": parking.feature_id if parking else None,
        "parking_match_method": parking.match_method if parking else None,
        "parking_geometric_match_confidence": (
            parking.geometric_match_confidence if parking else None
        ),
        "parking_area_sqm": parking.area_sqm if parking else None,
        "parking_operator_attribution_status": (
            parking.operator_attribution_status if parking else "no_match"
        ),
        "parking_operator_attributable_area_sqm": (
            parking.operator_attributable_area_sqm if parking else None
        ),
        "parking_area_total_sqm": poi.parking_area_total_sqm,
        "operator_usable_area_verified": False,
        "osm_operator_tag_match": poi.osm_operator_tag_match,
        "enrichment_status": poi.enrichment_status,
        "data_quality_flags": ";".join(
            sorted(set(location.data_quality_flags or []) | set(poi.data_quality_flags or []))
        ),
    }


def export_samples(settings: Settings, sample_size: int = 6) -> dict[str, Any]:
    """Write CSV/JSON output samples plus an extraction summary."""
    enriched = _read_json(settings.data_dir / ENRICHED_ARTEFACT)
    normalized = _read_json(settings.data_dir / NORMALIZED_ARTEFACT)
    pois = [EnrichedPoi.from_json(item) for item in enriched["pois"]]
    settings.samples_dir.mkdir(parents=True, exist_ok=True)

    csv_path = settings.samples_dir / "commercial_poi_staging_rows.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SAMPLE_CSV_COLUMNS)
        writer.writeheader()
        for poi in pois:
            writer.writerow(_row_for_csv(poi))

    # A small, fully-detailed JSON sample including geometry WKT.
    sample = []
    chosen = [poi for poi in pois if poi.building][:sample_size]
    if len(chosen) < sample_size:
        chosen += [poi for poi in pois if not poi.building][: sample_size - len(chosen)]
    for poi in chosen:
        sample.append(poi.to_json())
    sample_path = settings.samples_dir / "commercial_poi_sample.json"
    sample_path.write_text(
        json.dumps(
            {
                "task_id": TASK_ID,
                "note": "Representative rows exactly as loaded into "
                        "iris_staging.commercial_poi_staging. Geometry WKT is EPSG:25832.",
                "attribution": enriched.get("attribution"),
                "rows": sample,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    duplicates_path = settings.samples_dir / "duplicates_prevented.json"
    duplicates_path.write_text(
        json.dumps(
            {
                "task_id": TASK_ID,
                "note": "Records collected from the official sites that resolved to an "
                        "address already represented by another record of the same "
                        "operator. They are kept for provenance but not loaded as "
                        "separate POIs.",
                "duplicates": normalized["duplicates"],
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    geocode_report = settings.samples_dir / "geocoding_report.csv"
    with geocode_report.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "poi_uid",
                "operator",
                "address_normalized",
                "geocode_status",
                "strategy",
                "result_count",
                "ambiguous",
                "ambiguity_spread_m",
                "precision",
                "postcode_match",
                "city_match",
                "name_operator_match",
                "returned_display_name",
            ]
        )
        for poi in pois:
            geocode = poi.geocode
            writer.writerow(
                [
                    poi.location.poi_uid,
                    poi.location.operator,
                    poi.location.address_normalized,
                    geocode.status if geocode else None,
                    geocode.strategy if geocode else None,
                    geocode.result_count if geocode else None,
                    geocode.ambiguous if geocode else None,
                    geocode.ambiguity_spread_m if geocode else None,
                    geocode.precision if geocode else None,
                    geocode.postcode_match if geocode else None,
                    geocode.city_match if geocode else None,
                    geocode.name_operator_match if geocode else None,
                    geocode.display_name if geocode else None,
                ]
            )

    return {
        "rows_csv": str(csv_path),
        "sample_json": str(sample_path),
        "duplicates_json": str(duplicates_path),
        "geocoding_report": str(geocode_report),
        "rows": len(pois),
    }


# --------------------------------------------------------------------------
# Database evidence
# --------------------------------------------------------------------------

VERIFICATION_QUERIES: list[tuple[str, str]] = [
    (
        "search_path",
        "SHOW search_path",
    ),
    (
        "postgres_version",
        "SELECT version()",
    ),
    (
        "postgis_version",
        "SELECT postgis_version()",
    ),
    (
        "row_counts",
        "SELECT operator, count(*) AS rows, "
        "count(geom) AS with_point, "
        "count(building_geom) AS with_building, "
        "count(parking_geom) AS with_parking "
        "FROM iris_staging.commercial_poi_staging GROUP BY operator ORDER BY operator",
    ),
    (
        "srid_check",
        "SELECT DISTINCT ST_SRID(geom) AS geom_srid, "
        "ST_SRID(building_geom) AS building_srid, "
        "ST_SRID(parking_geom) AS parking_srid "
        "FROM iris_staging.commercial_poi_staging "
        "WHERE geom IS NOT NULL",
    ),
    (
        "geometry_columns_registered",
        "SELECT f_table_schema, f_table_name, f_geometry_column, srid, type "
        "FROM geometry_columns WHERE f_table_schema = 'iris_staging' "
        "ORDER BY f_table_name, f_geometry_column",
    ),
    (
        "invalid_geometry_count",
        "SELECT count(*) AS invalid_rows FROM iris_staging.commercial_poi_staging "
        "WHERE (building_geom IS NOT NULL AND NOT ST_IsValid(building_geom)) "
        "   OR (parking_geom IS NOT NULL AND NOT ST_IsValid(parking_geom))",
    ),
    (
        "duplicate_key_check",
        "SELECT count(*) AS duplicate_dedup_keys FROM ("
        "  SELECT dedup_key FROM iris_staging.commercial_poi_staging "
        "  GROUP BY dedup_key HAVING count(*) > 1) AS d",
    ),
    (
        "area_sanity",
        "SELECT operator, "
        "count(building_footprint_area_sqm) AS n_areas, "
        "round(min(building_footprint_area_sqm))::text AS min_sqm, "
        "round(avg(building_footprint_area_sqm))::text AS avg_sqm, "
        "round(max(building_footprint_area_sqm))::text AS max_sqm "
        "FROM iris_staging.commercial_poi_staging GROUP BY operator ORDER BY operator",
    ),
    (
        "area_recomputed_in_db",
        "SELECT poi_uid, building_footprint_area_sqm AS stored_sqm, "
        "round(ST_Area(building_geom)::numeric, 1) AS postgis_sqm, "
        "abs(building_footprint_area_sqm - round(ST_Area(building_geom)::numeric, 1)) "
        "AS delta FROM iris_staging.commercial_poi_staging "
        "WHERE building_geom IS NOT NULL ORDER BY delta DESC LIMIT 5",
    ),
    (
        "match_method_distribution",
        "SELECT building_match_method, building_geometric_match_confidence, "
        "count(*) AS rows FROM iris_staging.commercial_poi_staging "
        "GROUP BY 1, 2 ORDER BY rows DESC",
    ),
    (
        "operator_attribution_audit",
        "SELECT building_operator_attribution_status AS attribution, "
        "count(*) AS rows, "
        "count(building_footprint_area_sqm) AS raw_osm_areas, "
        "count(building_operator_attributable_area_sqm) AS operator_areas, "
        "round(max(building_footprint_area_sqm)) AS max_raw_sqm "
        "FROM iris_staging.commercial_poi_staging "
        "GROUP BY 1 ORDER BY rows DESC",
    ),
    (
        "parking_attribution_audit",
        "SELECT parking_operator_attribution_status AS attribution, "
        "count(*) AS rows, count(parking_area_sqm) AS raw_osm_areas, "
        "count(parking_operator_attributable_area_sqm) AS operator_areas, "
        "round(max(parking_area_sqm)) AS max_raw_sqm "
        "FROM iris_staging.commercial_poi_staging GROUP BY 1 ORDER BY rows DESC",
    ),
    (
        "operator_area_never_asserted_without_attribution",
        "SELECT count(*) AS violating_rows FROM iris_staging.commercial_poi_staging "
        "WHERE (building_operator_attributable_area_sqm IS NOT NULL "
        "       AND building_operator_attribution_status <> 'operator_tagged') "
        "   OR (parking_operator_attributable_area_sqm IS NOT NULL "
        "       AND parking_operator_attribution_status <> 'operator_tagged') "
        "   OR operator_usable_area_verified IS TRUE",
    ),
    (
        "objects_in_staging_schema",
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'iris_staging' ORDER BY table_name",
    ),
    (
        "objects_outside_staging_created_by_pipeline",
        "SELECT table_schema, table_name FROM information_schema.tables "
        "WHERE table_name LIKE 'commercial_poi%' AND table_schema <> 'iris_staging'",
    ),
    (
        "load_runs",
        "SELECT run_id, stage, locations_processed, rows_inserted, rows_updated, "
        "rows_unchanged, rows_failed, duplicates_prevented, started_at "
        "FROM iris_staging.commercial_poi_load_run ORDER BY started_at",
    ),
]


def run_verification(settings: Settings) -> dict[str, Any]:
    """Execute the read-only evidence queries and return their results."""
    from .db import create_staging_engine

    engine, guard = create_staging_engine(
        settings.require_db_uri(), settings.staging_schema
    )
    output: dict[str, Any] = {
        "task_id": TASK_ID,
        "pipeline_version": __version__,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "queries": [],
    }
    with engine.connect() as connection:
        for name, sql in VERIFICATION_QUERIES:
            try:
                result = connection.execute(text(sql))
                rows = [dict(row._mapping) for row in result]
            except Exception as exc:  # keep going; report the failure
                output["queries"].append(
                    {"name": name, "sql": sql, "error": f"{type(exc).__name__}: {exc}"}
                )
                continue
            output["queries"].append({"name": name, "sql": sql, "rows": rows})
    output["schema_guard"] = guard.stats()
    engine.dispose()
    return output


def write_verification_report(settings: Settings, payload: dict[str, Any]) -> Path:
    settings.samples_dir.mkdir(parents=True, exist_ok=True)
    json_path = settings.samples_dir / "db_verification.json"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    text_path = settings.samples_dir / "db_verification.txt"
    lines = [
        f"TASK: {payload['task_id']}  pipeline {payload['pipeline_version']}",
        f"generated_at: {payload['generated_at']}",
        "",
    ]
    for entry in payload["queries"]:
        lines.append(f"### {entry['name']}")
        lines.append(f"-- {entry['sql']}")
        if "error" in entry:
            lines.append(f"ERROR: {entry['error']}")
        else:
            rows = entry["rows"]
            if not rows:
                lines.append("(0 rows)")
            else:
                headers = list(rows[0].keys())
                lines.append(" | ".join(headers))
                for row in rows:
                    lines.append(
                        " | ".join("" if row[h] is None else str(row[h]) for h in headers)
                    )
        lines.append("")
    lines.append(f"schema_guard: {payload.get('schema_guard')}")
    text_path.write_text("\n".join(lines), encoding="utf-8")
    return text_path
