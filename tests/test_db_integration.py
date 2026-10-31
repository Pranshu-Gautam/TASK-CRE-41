"""Integration tests against a real PostgreSQL 16 / PostGIS 3.4 instance.

These tests are marked ``integration`` and SKIP (never silently pass) when
``DB_URI`` is absent or unreachable. They run in their own schema
(``iris_staging_test``) so they cannot disturb a real staging load.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine, text

from iris_cre41.db import (
    StagingIsolationError,
    create_staging_engine,
    create_staging_objects,
    ensure_postgis,
)
from iris_cre41.db.loader import load_pois, record_run, table_counts
from iris_cre41.models import EnrichedPoi, GeocodeResult, NormalizedLocation, SpatialMatch

pytestmark = pytest.mark.integration

TEST_SCHEMA = "iris_staging_test"
CANARY_SCHEMA = "iris_core_test_canary"
WHEN = dt.datetime(2026, 10, 31, 9, 0, tzinfo=dt.timezone.utc)

SQUARE = (
    "MULTIPOLYGON (((296560 5627960, 296600 5627960, 296600 5628000, "
    "296560 5628000, 296560 5627960)))"
)


def make_poi(index: int, city: str = "Aachen", with_building: bool = True) -> EnrichedPoi:
    source_uid = f"mrwash:mrwash.de:branch-{index}"
    location = NormalizedLocation(
        poi_uid=f"cre41-test-{index}",
        source_uid=source_uid,
        dedup_key=f"dedup-{index}",
        operator="Mr. Wash",
        operator_slug="mrwash",
        location_name=f"Test {index}",
        location_name_raw=f"Test {index}",
        street="Breslauer Straße",
        house_number=str(index),
        postal_code="52068",
        city=city,
        country="DE",
        address_raw=f"Breslauer Straße {index}\n52068 {city}",
        address_normalized=f"Breslauer Straße {index}, 52068 {city}",
        source_system="mrwash.de",
        source_record_id=f"branch-{index}",
        source_url=f"https://www.mrwash.de/de/frontend/branch?id={index}",
        retrieved_at=WHEN,
        source_attributes={"poi_category": "car_wash"},
    )
    building = (
        SpatialMatch(
            feature_kind="building",
            feature_id=f"way/{index}",
            geometry_type="MultiPolygon",
            match_method="point_in_building",
            geometric_match_confidence="high",
            distance_m=0.0,
            area_sqm=1600.0,
            operator_attribution_status="operator_tagged",
            operator_attribution_evidence="OSM tag names the operator (name=Mr. Wash)",
            operator_attributable_area_sqm=1600.0,
            validity_status="valid",
            wkt_25832=SQUARE,
            source_url=f"https://www.openstreetmap.org/way/{index}",
            tags={"building": "commercial", "name": "Mr. Wash"},
        )
        if with_building
        else None
    )
    return EnrichedPoi(
        location=location,
        geocode=GeocodeResult(
            status="ok", provider="nominatim", query="q", lat=50.7784, lon=6.1188,
            source_epsg=4326, precision="address", postcode_match=True,
            city_match=True, geocoded_at=WHEN,
        ),
        x_25832=296580.0,
        y_25832=5627980.0,
        point_wkt_25832="POINT (296580 5627980)",
        building=building,
        enrichment_status="partial" if with_building else "point_only",
        geometry_provenance={"point": {"provider": "nominatim"}},
    )


@pytest.fixture
def staging(db_uri):
    """A clean test schema plus a canary production-like schema."""
    admin = create_engine(db_uri)
    with admin.begin() as connection:
        connection.execute(text(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE"))
        connection.execute(text(f"CREATE SCHEMA {CANARY_SCHEMA}"))
        connection.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {CANARY_SCHEMA}.protected "
                "(id INT PRIMARY KEY, label TEXT)"
            )
        )
        connection.execute(
            text(f"INSERT INTO {CANARY_SCHEMA}.protected VALUES (1, 'untouched') "
                 "ON CONFLICT (id) DO NOTHING")
        )
    engine, guard = create_staging_engine(db_uri, TEST_SCHEMA)
    metadata = create_staging_objects(engine, TEST_SCHEMA)
    yield engine, guard, metadata, admin
    engine.dispose()
    with admin.begin() as connection:
        connection.execute(text(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE"))
        connection.execute(text(f"DROP SCHEMA IF EXISTS {CANARY_SCHEMA} CASCADE"))
    admin.dispose()


# -- capabilities -----------------------------------------------------------

def test_postgis_and_postgres_versions_meet_the_requirement(staging):
    engine, _, _, _ = staging
    info = ensure_postgis(engine)
    assert info["postgres_version"].startswith("16.")
    assert info["postgis_version"].startswith("3.4")


def test_search_path_is_restricted(staging):
    engine, _, _, _ = staging
    with engine.connect() as connection:
        search_path = connection.execute(text("SHOW search_path")).scalar()
    assert search_path.replace('"', "").startswith(TEST_SCHEMA)
    assert "iris_core" not in search_path


def test_tables_are_created_only_in_the_staging_schema(staging):
    engine, _, _, _ = staging
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_name LIKE 'commercial_poi%'"
            )
        ).fetchall()
    assert rows, "expected the staging tables to exist"
    schemas = {row[0] for row in rows}
    assert TEST_SCHEMA in schemas
    # Nothing the pipeline creates may live outside a staging schema. (A real
    # `iris_staging` from a production-style run may legitimately coexist.)
    assert all(schema.startswith("iris_staging") for schema in schemas), schemas


def test_geometry_columns_are_registered_with_srid_25832(staging):
    engine, _, _, _ = staging
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT f_geometry_column, srid, type FROM geometry_columns "
                "WHERE f_table_schema = :schema AND f_table_name = "
                "'commercial_poi_staging' ORDER BY f_geometry_column"
            ),
            {"schema": TEST_SCHEMA},
        ).fetchall()
    assert {row[1] for row in rows} == {25832}
    assert {row[0] for row in rows} == {"building_geom", "geom", "parking_geom"}
    assert dict((row[0], row[2]) for row in rows)["geom"] == "POINT"


def test_spatial_indexes_exist(staging):
    engine, _, _, _ = staging
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = :schema AND tablename = 'commercial_poi_staging'"
            ),
            {"schema": TEST_SCHEMA},
        ).fetchall()
    gist = [row[0] for row in rows if "gist" in row[1].lower()]
    assert len(gist) >= 3


# -- load + idempotency ----------------------------------------------------

def test_load_inserts_then_is_idempotent(staging):
    engine, _, metadata, _ = staging
    pois = [make_poi(index) for index in (1, 2, 3)]

    first = load_pois(engine, metadata, pois, "collect-1", "load-1")
    assert (first.inserted, first.updated, first.unchanged, first.failed) == (3, 0, 0, 0)

    second = load_pois(engine, metadata, pois, "collect-1", "load-2")
    assert (second.inserted, second.updated, second.failed) == (0, 0, 0)
    assert second.unchanged == 3

    assert table_counts(engine, metadata)["commercial_poi_staging"] == 3


def test_changed_content_produces_an_update_not_a_duplicate(staging):
    engine, _, metadata, _ = staging
    load_pois(engine, metadata, [make_poi(1)], "c", "l1")
    changed = make_poi(1, city="Köln")
    stats = load_pois(engine, metadata, [changed], "c", "l2")
    assert (stats.inserted, stats.updated, stats.unchanged) == (0, 1, 0)
    with engine.connect() as connection:
        rows = connection.execute(
            text(f"SELECT city, count(*) OVER () FROM {TEST_SCHEMA}."
                 "commercial_poi_staging")
        ).fetchall()
    assert rows == [("Köln", 1)]


def test_first_seen_at_is_preserved_across_updates(staging):
    engine, _, metadata, _ = staging
    load_pois(engine, metadata, [make_poi(1)], "c", "l1")
    with engine.connect() as connection:
        original = connection.execute(
            text(f"SELECT first_seen_at FROM {TEST_SCHEMA}.commercial_poi_staging")
        ).scalar()
    load_pois(engine, metadata, [make_poi(1, city="Köln")], "c", "l2")
    with engine.connect() as connection:
        row = connection.execute(
            text(
                f"SELECT first_seen_at, updated_at FROM {TEST_SCHEMA}."
                "commercial_poi_staging"
            )
        ).fetchone()
    assert row[0] == original
    assert row[1] >= original


def test_duplicate_dedup_key_cannot_be_inserted(staging):
    engine, _, metadata, _ = staging
    first = make_poi(1)
    clash = make_poi(2)
    clash.location.dedup_key = first.location.dedup_key  # same address, new source id
    load_pois(engine, metadata, [first], "c", "l1")
    stats = load_pois(engine, metadata, [clash], "c", "l2")
    assert stats.failed == 1
    assert stats.inserted == 0
    assert "uq_commercial_poi_staging_dedup_key" in stats.failures[0]["error"]
    assert table_counts(engine, metadata)["commercial_poi_staging"] == 1


def test_srid_is_25832_in_the_database(staging):
    engine, _, metadata, _ = staging
    load_pois(engine, metadata, [make_poi(1)], "c", "l")
    with engine.connect() as connection:
        row = connection.execute(
            text(
                f"SELECT ST_SRID(geom), ST_SRID(building_geom), "
                f"ST_GeometryType(geom), ST_GeometryType(building_geom) "
                f"FROM {TEST_SCHEMA}.commercial_poi_staging"
            )
        ).fetchone()
    assert row[0] == 25832
    assert row[1] == 25832
    assert row[2] == "ST_Point"
    assert row[3] == "ST_MultiPolygon"


def test_stored_area_matches_postgis_recomputation(staging):
    engine, _, metadata, _ = staging
    load_pois(engine, metadata, [make_poi(1)], "c", "l")
    with engine.connect() as connection:
        stored, recomputed = connection.execute(
            text(
                f"SELECT building_footprint_area_sqm, "
                f"round(ST_Area(building_geom)::numeric, 1) "
                f"FROM {TEST_SCHEMA}.commercial_poi_staging"
            )
        ).fetchone()
    assert float(stored) == pytest.approx(float(recomputed), abs=0.2)
    assert float(stored) == pytest.approx(1600.0, abs=0.5)


def test_point_without_polygon_keeps_null_metrics(staging):
    engine, _, metadata, _ = staging
    load_pois(engine, metadata, [make_poi(9, with_building=False)], "c", "l")
    with engine.connect() as connection:
        row = connection.execute(
            text(
                f"SELECT geom IS NOT NULL, building_geom IS NULL, "
                f"building_footprint_area_sqm IS NULL, enrichment_status "
                f"FROM {TEST_SCHEMA}.commercial_poi_staging"
            )
        ).fetchone()
    assert row[0] is True
    assert row[1] is True
    assert row[2] is True
    assert row[3] == "point_only"


def test_wrong_srid_is_rejected_by_the_check_constraint(staging):
    engine, _, _, _ = staging
    with pytest.raises(Exception) as excinfo:
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO {TEST_SCHEMA}.commercial_poi_staging "
                    "(poi_uid, source_uid, dedup_key, operator, operator_slug, "
                    " country, source_system, source_record_id, source_url, "
                    " retrieved_at, geocode_status, enrichment_status, row_hash, geom) "
                    "VALUES ('bad', 'bad', 'bad', 'X', 'x', 'DE', 's', 'r', 'u', "
                    " now(), 'ok', 'point_only', 'h', "
                    " ST_SetSRID(ST_MakePoint(6.1, 50.7), 4326))"
                )
            )
    assert "srid" in str(excinfo.value).lower() or "geometry" in str(excinfo.value).lower()


def test_malformed_postal_code_is_rejected(staging):
    engine, _, _, _ = staging
    with pytest.raises(Exception) as excinfo:
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO {TEST_SCHEMA}.commercial_poi_staging "
                    "(poi_uid, source_uid, dedup_key, operator, operator_slug, "
                    " postal_code, country, source_system, source_record_id, "
                    " source_url, retrieved_at, geocode_status, enrichment_status, "
                    " row_hash) VALUES ('bad2','bad2','bad2','X','x','ABCDE','DE',"
                    " 's','r','u', now(), 'ok', 'point_only', 'h')"
                )
            )
    assert "ck_poi_postal_code_format" in str(excinfo.value)


def test_run_log_records_execution_evidence(staging):
    engine, _, metadata, _ = staging
    stats = load_pois(engine, metadata, [make_poi(1)], "collect-x", "load-x")
    record_run(
        engine, metadata, run_id="load-x", stage="load", started_at=WHEN,
        finished_at=WHEN, stats={"locations_processed": 1, "load": stats.as_dict()},
    )
    with engine.connect() as connection:
        row = connection.execute(
            text(
                f"SELECT task_id, stage, rows_inserted, locations_processed "
                f"FROM {TEST_SCHEMA}.commercial_poi_load_run WHERE run_id = 'load-x'"
            )
        ).fetchone()
    assert row == ("TASK-CRE-41", "load", 1, 1)


# -- isolation -------------------------------------------------------------

def test_write_to_a_production_like_schema_is_blocked(staging):
    engine, guard, _, admin = staging
    with pytest.raises(StagingIsolationError):
        with engine.begin() as connection:
            connection.execute(
                text(f"UPDATE {CANARY_SCHEMA}.protected SET label = 'tampered'")
            )
    # The canary row is provably untouched.
    with admin.connect() as connection:
        label = connection.execute(
            text(f"SELECT label FROM {CANARY_SCHEMA}.protected WHERE id = 1")
        ).scalar()
    assert label == "untouched"
    assert guard.stats()["violations"] >= 1


def test_insert_into_production_like_schema_is_blocked(staging):
    engine, _, _, admin = staging
    with pytest.raises(StagingIsolationError):
        with engine.begin() as connection:
            connection.execute(
                text(f"INSERT INTO {CANARY_SCHEMA}.protected VALUES (2, 'injected')")
            )
    with admin.connect() as connection:
        count = connection.execute(
            text(f"SELECT count(*) FROM {CANARY_SCHEMA}.protected")
        ).scalar()
    assert count == 1


def test_reading_a_production_like_schema_is_permitted(staging):
    engine, _, _, _ = staging
    with engine.connect() as connection:
        value = connection.execute(
            text(f"SELECT label FROM {CANARY_SCHEMA}.protected WHERE id = 1")
        ).scalar()
    assert value == "untouched"


def test_full_load_creates_nothing_outside_staging(staging):
    engine, _, metadata, admin = staging
    load_pois(engine, metadata, [make_poi(index) for index in (1, 2)], "c", "l")
    with admin.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT table_schema FROM information_schema.tables "
                "WHERE table_name LIKE 'commercial_poi%'"
            )
        ).fetchall()
    schemas = {row[0] for row in rows}
    assert TEST_SCHEMA in schemas
    assert all(schema.startswith("iris_staging") for schema in schemas), schemas
    assert not {s for s in schemas if s.startswith(("iris_core", "public", "iris_prod"))}


# -- operator attribution enforced by the database --------------------------

def test_operator_area_cannot_be_asserted_without_attribution(staging):
    """The database itself refuses an operator area on an unattributed polygon."""
    engine, _, _, _ = staging
    with pytest.raises(Exception) as excinfo:
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO {TEST_SCHEMA}.commercial_poi_staging "
                    "(poi_uid, source_uid, dedup_key, operator, operator_slug, "
                    " country, source_system, source_record_id, source_url, "
                    " retrieved_at, geocode_status, enrichment_status, row_hash, "
                    " building_operator_attribution_status, "
                    " building_operator_attributable_area_sqm) "
                    "VALUES ('attr1','attr1','attr1','X','x','DE','s','r','u',"
                    " now(),'ok','point_only','h','third_party_named', 47136.0)"
                )
            )
    assert "ck_poi_building_attributable_area_requires_attribution" in str(excinfo.value)


def test_parking_operator_area_cannot_be_asserted_without_attribution(staging):
    engine, _, _, _ = staging
    with pytest.raises(Exception) as excinfo:
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO {TEST_SCHEMA}.commercial_poi_staging "
                    "(poi_uid, source_uid, dedup_key, operator, operator_slug, "
                    " country, source_system, source_record_id, source_url, "
                    " retrieved_at, geocode_status, enrichment_status, row_hash, "
                    " parking_operator_attribution_status, "
                    " parking_operator_attributable_area_sqm) "
                    "VALUES ('attr2','attr2','attr2','X','x','DE','s','r','u',"
                    " now(),'ok','point_only','h','unverified', 21901.0)"
                )
            )
    assert "ck_poi_parking_attributable_area_requires_attribution" in str(excinfo.value)


def test_operator_usable_area_verified_cannot_be_set_true(staging):
    """Assertion 3 is not assertable by this pipeline, and the DB enforces it."""
    engine, _, _, _ = staging
    with pytest.raises(Exception) as excinfo:
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO {TEST_SCHEMA}.commercial_poi_staging "
                    "(poi_uid, source_uid, dedup_key, operator, operator_slug, "
                    " country, source_system, source_record_id, source_url, "
                    " retrieved_at, geocode_status, enrichment_status, row_hash, "
                    " operator_usable_area_verified) "
                    "VALUES ('attr3','attr3','attr3','X','x','DE','s','r','u',"
                    " now(),'ok','point_only','h', true)"
                )
            )
    assert "ck_poi_operator_usable_area_unverified" in str(excinfo.value)


def test_invalid_attribution_status_is_rejected(staging):
    engine, _, _, _ = staging
    with pytest.raises(Exception) as excinfo:
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO {TEST_SCHEMA}.commercial_poi_staging "
                    "(poi_uid, source_uid, dedup_key, operator, operator_slug, "
                    " country, source_system, source_record_id, source_url, "
                    " retrieved_at, geocode_status, enrichment_status, row_hash, "
                    " building_operator_attribution_status) "
                    "VALUES ('attr4','attr4','attr4','X','x','DE','s','r','u',"
                    " now(),'ok','point_only','h','definitely_owned')"
                )
            )
    assert "ck_poi_building_attribution_status" in str(excinfo.value)


def test_absent_jsonb_is_sql_null_not_json_null(staging):
    """A row with no parking must have SQL NULL parking_tags, not JSON null."""
    engine, _, metadata, _ = staging
    load_pois(engine, metadata, [make_poi(5, with_building=False)], "c", "l")
    with engine.connect() as connection:
        row = connection.execute(
            text(
                f"SELECT parking_tags IS NULL, building_tags IS NULL, "
                f"count(*) FILTER (WHERE parking_tags = 'null'::jsonb) "
                f"FROM {TEST_SCHEMA}.commercial_poi_staging "
                f"GROUP BY 1, 2"
            )
        ).fetchone()
    assert row[0] is True
    assert row[1] is True
    assert row[2] == 0


def test_column_rename_migration_preserves_data(staging, db_uri):
    """sync_staging_columns renames rather than duplicating a renamed column."""
    from sqlalchemy import inspect

    from iris_cre41.db.schema import COLUMN_RENAMES, build_metadata, sync_staging_columns

    engine, _, metadata, admin = staging
    renames = COLUMN_RENAMES["commercial_poi_staging"]
    old_name, new_name = next(iter(renames.items()))

    # Simulate the pre-rename state and put a value in the old column.
    with admin.begin() as connection:
        connection.execute(
            text(
                f'ALTER TABLE {TEST_SCHEMA}.commercial_poi_staging '
                f'RENAME COLUMN "{new_name}" TO "{old_name}"'
            )
        )
    load_pois(engine, metadata, [], "c", "l")  # no-op, just proves the engine works
    with admin.begin() as connection:
        connection.execute(
            text(
                f"INSERT INTO {TEST_SCHEMA}.commercial_poi_staging "
                "(poi_uid, source_uid, dedup_key, operator, operator_slug, country, "
                " source_system, source_record_id, source_url, retrieved_at, "
                f" geocode_status, enrichment_status, row_hash, \"{old_name}\") "
                "VALUES ('ren1','ren1','ren1','X','x','DE','s','r','u',now(),"
                "'ok','point_only','h','medium')"
            )
        )

    report = sync_staging_columns(engine, build_metadata(TEST_SCHEMA))
    assert f"commercial_poi_staging.{old_name} -> {new_name}" in report["renamed"]

    columns = {
        column["name"]
        for column in inspect(engine).get_columns(
            "commercial_poi_staging", schema=TEST_SCHEMA
        )
    }
    assert new_name in columns
    assert old_name not in columns
    with engine.connect() as connection:
        value = connection.execute(
            text(
                f'SELECT "{new_name}" FROM {TEST_SCHEMA}.commercial_poi_staging '
                "WHERE poi_uid = 'ren1'"
            )
        ).scalar()
    assert value == "medium", "the rename must preserve the stored value"


# -- database-level isolation (least-privilege role) ------------------------

@pytest.fixture
def loader_uri():
    """A DB_URI for the restricted loader role, or skip with a clear reason."""
    import os

    uri = os.environ.get("LOADER_DB_URI")
    if not uri:
        pytest.skip(
            "LOADER_DB_URI not set — database-level isolation test NOT executed "
            "(apply sql/002_least_privilege_role.sql first)"
        )
    try:
        engine = create_engine(uri)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        engine.dispose()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"LOADER_DB_URI unreachable ({type(exc).__name__}) — NOT executed")
    return uri


def test_restricted_role_cannot_write_outside_staging_even_unguarded(loader_uri, db_uri):
    """PostgreSQL itself must refuse the write, with no application guard present."""
    admin = create_engine(db_uri)
    with admin.begin() as connection:
        connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {CANARY_SCHEMA}"))
        connection.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {CANARY_SCHEMA}.protected "
                "(id INT PRIMARY KEY, label TEXT)"
            )
        )
        connection.execute(
            text(
                f"INSERT INTO {CANARY_SCHEMA}.protected VALUES (1, 'untouched') "
                "ON CONFLICT (id) DO NOTHING"
            )
        )
        connection.execute(
            text(f"REVOKE ALL ON SCHEMA {CANARY_SCHEMA} FROM iris_cre41_loader")
        )

    # Raw engine: no attach_search_path, no SchemaWriteGuard.
    raw = create_engine(loader_uri)
    for statement in (
        f"INSERT INTO {CANARY_SCHEMA}.protected VALUES (2, 'injected')",
        f"UPDATE {CANARY_SCHEMA}.protected SET label = 'tampered'",
        f"CREATE TABLE {CANARY_SCHEMA}.sneaky (a int)",
        "CREATE TABLE public.sneaky (a int)",
        "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    ):
        with pytest.raises(Exception) as excinfo:
            with raw.begin() as connection:
                connection.execute(text(statement))
        assert "permission denied" in str(excinfo.value).lower(), statement
    raw.dispose()

    with admin.connect() as connection:
        rows = connection.execute(
            text(f"SELECT id, label FROM {CANARY_SCHEMA}.protected ORDER BY id")
        ).fetchall()
    assert [tuple(row) for row in rows] == [(1, "untouched")]
    with admin.begin() as connection:
        connection.execute(text(f"DROP SCHEMA IF EXISTS {CANARY_SCHEMA} CASCADE"))
    admin.dispose()


def test_restricted_role_can_still_do_its_job(loader_uri):
    raw = create_engine(loader_uri)
    with raw.connect() as connection:
        assert connection.execute(text("SELECT postgis_version()")).scalar()
        search_path = connection.execute(text("SHOW search_path")).scalar()
    assert "iris_staging" in search_path
    assert "iris_core" not in search_path
    raw.dispose()


def test_schema_creation_does_not_require_database_create_privilege(loader_uri):
    """ensure_schema must be a no-op (not a failure) for an existing schema."""
    from iris_cre41.db import create_staging_engine, ensure_schema

    engine, _ = create_staging_engine(loader_uri, "iris_staging")
    assert ensure_schema(engine, "iris_staging") is False
    engine.dispose()


def test_full_rerun_with_fresh_provenance_timestamps_is_a_no_op(staging):
    """Re-deriving the artefacts must not report a change just because it ran.

    This is stronger than re-loading the same artefact: the provenance
    timestamps differ, as they would after a genuine re-run of geocode and
    enrich.
    """
    engine, _, metadata, _ = staging
    first = make_poi(7)
    first.geometry_provenance = {
        "point": {"provider": "nominatim", "geocoded_at": "2026-10-31T05:00:00+00:00"},
        "osm_query": {"provider": "overpass-api.de", "elements_returned": 9,
                      "queried_at": "2026-10-31T06:00:00+00:00"},
    }
    initial = load_pois(engine, metadata, [first], "collect-1", "load-1")
    assert initial.inserted == 1

    second = make_poi(7)
    second.geometry_provenance = {
        "point": {"provider": "nominatim", "geocoded_at": "2026-11-09T21:00:00+00:00"},
        "osm_query": {"provider": "overpass-api.de", "elements_returned": 9,
                      "queried_at": "2026-11-09T22:00:00+00:00"},
    }
    repeat = load_pois(engine, metadata, [second], "collect-2", "load-2")
    assert (repeat.inserted, repeat.updated, repeat.unchanged) == (0, 0, 1)

    # A substantive provenance change still registers.
    third = make_poi(7)
    third.geometry_provenance = {
        "point": {"provider": "nominatim", "geocoded_at": "2026-11-09T21:00:00+00:00"},
        "osm_query": {"provider": "overpass-api.de", "elements_returned": 14,
                      "queried_at": "2026-11-09T22:00:00+00:00"},
    }
    changed = load_pois(engine, metadata, [third], "collect-3", "load-3")
    assert (changed.inserted, changed.updated, changed.unchanged) == (0, 1, 0)
    assert table_counts(engine, metadata)["commercial_poi_staging"] == 1
