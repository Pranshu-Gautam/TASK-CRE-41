"""Shared pytest fixtures.

Unit tests never touch the network or the database. Tests that genuinely need
PostgreSQL are marked ``integration`` and skip (loudly) when ``DB_URI`` is not
reachable, so an unexecuted integration test can never be mistaken for a pass.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from iris_cre41.config import Settings, load_settings

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixture_dir() -> Path:
    return FIXTURE_DIR


def read_fixture(name: str) -> str:
    path = FIXTURE_DIR / name
    if not path.exists():
        pytest.skip(f"fixture {name} missing; run tools/capture_fixtures.py")
    return path.read_text(encoding="utf-8")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointed at a throwaway directory, with no DB and no network."""
    base = load_settings(dotenv=False)
    return Settings(
        db_uri=None,
        staging_schema="iris_staging",
        user_agent="IRIS-CRE41-tests/1.0 (unit tests; no outbound traffic)",
        http_timeout=5.0,
        http_max_retries=1,
        scrape_min_interval=0.0,
        geocoder="static",
        nominatim_url="https://nominatim.invalid/search",
        nominatim_min_interval=1.0,
        geocode_cache=tmp_path / "geocode.sqlite",
        overpass_url="https://overpass.invalid/api/interpreter",
        overpass_min_interval=1.0,
        overpass_cache=tmp_path / "overpass.sqlite",
        building_search_radius_m=80.0,
        parking_search_radius_m=120.0,
        data_dir=tmp_path / "data",
        samples_dir=tmp_path / "samples",
        target_epsg=base.target_epsg,
        source_epsg=base.source_epsg,
    )


@pytest.fixture(scope="session")
def db_uri() -> str:
    """A reachable DB_URI, or skip the test with a clear reason."""
    uri = os.environ.get("DB_URI")
    if not uri:
        pytest.skip("DB_URI not set — integration test NOT executed")
    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(uri, pool_pre_ping=True)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        engine.dispose()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"DB_URI unreachable ({type(exc).__name__}) — NOT executed")
    return uri


@pytest.fixture
def integration_settings(settings: Settings, db_uri: str, tmp_path: Path) -> Settings:
    import dataclasses

    return dataclasses.replace(
        settings, db_uri=db_uri, staging_schema="iris_staging_test"
    )
