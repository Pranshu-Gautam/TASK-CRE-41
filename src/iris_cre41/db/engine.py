"""Engine construction with isolation wired in from the start."""

from __future__ import annotations

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from ..logging_setup import get_logger
from .guard import SchemaWriteGuard, attach_search_path

log = get_logger(__name__)


def create_staging_engine(
    db_uri: str, schema: str = "iris_staging", echo: bool = False
) -> tuple[Engine, SchemaWriteGuard]:
    """Create an engine whose connections can only write to ``schema``."""
    engine = create_engine(db_uri, echo=echo, future=True, pool_pre_ping=True)
    attach_search_path(engine, schema)
    guard = SchemaWriteGuard(schema).attach(engine)
    return engine, guard


def ensure_postgis(engine: Engine) -> dict[str, str]:
    """Verify PostGIS is available. The pipeline never installs it itself.

    Installing an extension is a privileged, cross-schema operation; it is a
    DBA task. If PostGIS is missing we fail loudly with instructions.
    """
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'postgis'")
        ).fetchone()
        if row is None:
            raise RuntimeError(
                "PostGIS is not installed in this database. The pipeline is not "
                "permitted to run CREATE EXTENSION (it would write outside "
                "iris_staging). Ask a DBA for: CREATE EXTENSION postgis;"
            )
        server = connection.execute(text("SHOW server_version")).scalar()
        postgis_full = connection.execute(text("SELECT postgis_full_version()")).scalar()
    info = {
        "postgis_version": row[0],
        "postgres_version": str(server),
        "postgis_full_version": str(postgis_full),
    }
    log.info("database_capabilities", extra=info)
    return info


def current_search_path(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.execute(text("SHOW search_path")).scalar())
