"""Staging-only write enforcement (no database required)."""

from __future__ import annotations

import pytest

from iris_cre41.db.guard import SchemaWriteGuard, StagingIsolationError, assert_only_staging

ALLOWED = [
    "INSERT INTO iris_staging.commercial_poi_staging (poi_uid) VALUES ('a')",
    "UPDATE iris_staging.commercial_poi_staging SET city = 'Kiel' WHERE poi_uid = 'a'",
    "DELETE FROM iris_staging.commercial_poi_staging WHERE poi_uid = 'a'",
    'CREATE SCHEMA IF NOT EXISTS "iris_staging"',
    "CREATE TABLE IF NOT EXISTS iris_staging.commercial_poi_staging (poi_uid TEXT)",
    "ALTER TABLE iris_staging.commercial_poi_staging ADD COLUMN notes TEXT",
    "CREATE INDEX ix_geom ON iris_staging.commercial_poi_staging USING gist (geom)",
    "CREATE UNIQUE INDEX uq_src ON iris_staging.commercial_poi_staging (source_uid)",
    "INSERT INTO iris_staging.commercial_poi_staging (poi_uid) VALUES ('a') "
    "ON CONFLICT (poi_uid) DO UPDATE SET poi_uid = excluded.poi_uid "
    "RETURNING (xmax = 0)",
    # Reads anywhere are fine: upstream data is read-only by design.
    "SELECT * FROM iris_core.portfolio",
    "SELECT ST_Area(geom) FROM iris_staging.commercial_poi_staging",
    "SHOW search_path",
    "SET search_path TO iris_staging, public",
]

BLOCKED = [
    "INSERT INTO iris_core.commercial_poi (id) VALUES (1)",
    "UPDATE iris_core.assets SET value = 1",
    "DELETE FROM iris_core.assets",
    "TRUNCATE TABLE iris_core.assets",
    "DROP TABLE iris_core.assets",
    "ALTER TABLE iris_core.assets ADD COLUMN x INT",
    "CREATE TABLE iris_core.sneaky (a INT)",
    "CREATE TABLE public.sneaky (a INT)",
    "DELETE FROM public.spatial_ref_sys",
    "CREATE INDEX ix_bad ON iris_core.assets (id)",
    "CREATE SCHEMA iris_core",
    "CREATE EXTENSION postgis",
    "DROP EXTENSION postgis",
    "CREATE ROLE intruder",
    "ALTER DATABASE iris SET x = 1",
    # Unqualified writes are refused rather than trusted to search_path.
    "INSERT INTO commercial_poi_staging (poi_uid) VALUES ('a')",
    "UPDATE commercial_poi_staging SET city = 'Kiel'",
    # Multi-statement smuggling.
    "INSERT INTO iris_staging.t (a) VALUES (1); UPDATE iris_core.y SET a = 1",
]


@pytest.mark.parametrize("statement", ALLOWED)
def test_allowed_statements_pass(statement):
    SchemaWriteGuard().check(statement)


@pytest.mark.parametrize("statement", BLOCKED)
def test_blocked_statements_raise(statement):
    with pytest.raises(StagingIsolationError):
        SchemaWriteGuard().check(statement)


def test_violation_message_names_the_offending_target():
    guard = SchemaWriteGuard()
    with pytest.raises(StagingIsolationError, match="iris_core"):
        guard.check("INSERT INTO iris_core.assets (a) VALUES (1)")
    assert guard.stats()["violations"] == 1


def test_sql_inside_a_string_literal_is_not_treated_as_a_statement():
    SchemaWriteGuard().check(
        "INSERT INTO iris_staging.commercial_poi_staging (notes) "
        "VALUES ('UPDATE iris_core.assets SET a=1')"
    )


def test_sql_inside_a_comment_is_ignored():
    SchemaWriteGuard().check(
        "-- UPDATE iris_core.assets SET a=1\n"
        "SELECT 1 FROM iris_staging.commercial_poi_staging"
    )


def test_guard_counts_what_it_inspected():
    guard = SchemaWriteGuard()
    guard.check("SELECT 1")
    guard.check("INSERT INTO iris_staging.t (a) VALUES (1)")
    stats = guard.stats()
    assert stats["statements_checked"] == 2
    assert stats["write_statements_allowed"] == 1
    assert stats["violations"] == 0


def test_configurable_schema_name():
    guard = SchemaWriteGuard("iris_staging_test")
    guard.check("INSERT INTO iris_staging_test.t (a) VALUES (1)")
    with pytest.raises(StagingIsolationError):
        guard.check("INSERT INTO iris_staging.t (a) VALUES (1)")


def test_assert_only_staging_helper():
    assert_only_staging(["INSERT INTO iris_staging.t (a) VALUES (1)"])
    with pytest.raises(StagingIsolationError):
        assert_only_staging(["INSERT INTO iris_core.t (a) VALUES (1)"])


def test_empty_statements_are_ignored():
    guard = SchemaWriteGuard()
    guard.check("")
    guard.check("   ")
    assert guard.stats()["statements_checked"] == 0


def test_generated_ddl_only_targets_staging():
    """Every statement the pipeline's own DDL emits must pass the guard."""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateIndex, CreateTable

    from iris_cre41.db.schema import build_metadata

    metadata = build_metadata("iris_staging")
    dialect = postgresql.dialect()
    statements = [f'CREATE SCHEMA IF NOT EXISTS "iris_staging"']
    for table in metadata.sorted_tables:
        statements.append(str(CreateTable(table).compile(dialect=dialect)))
        for index in table.indexes:
            statements.append(str(CreateIndex(index).compile(dialect=dialect)))
    assert len(statements) > 5
    assert_only_staging(statements)
