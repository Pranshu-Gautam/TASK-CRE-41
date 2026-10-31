"""PostgreSQL/PostGIS persistence, restricted to the staging schema."""

from .engine import create_staging_engine, ensure_postgis
from .guard import SchemaWriteGuard, StagingIsolationError
from .loader import LoadStats, load_pois, record_run
from .schema import (
    COMMERCIAL_POI_TABLE,
    LOAD_RUN_TABLE,
    RAW_OBSERVATION_TABLE,
    build_metadata,
    create_staging_objects,
    sync_staging_columns,
)

__all__ = [
    "create_staging_engine",
    "ensure_postgis",
    "SchemaWriteGuard",
    "StagingIsolationError",
    "build_metadata",
    "create_staging_objects",
    "sync_staging_columns",
    "COMMERCIAL_POI_TABLE",
    "RAW_OBSERVATION_TABLE",
    "LOAD_RUN_TABLE",
    "load_pois",
    "record_run",
    "LoadStats",
]
