"""Runtime configuration, sourced exclusively from environment variables.

No credential is ever hard-coded: ``DB_URI`` is mandatory for database
operations and is read from the environment (optionally seeded from a local
``.env`` file that is git-ignored).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Geographic sanity envelope for Germany (WGS84). Used to flag, never to
# invent, coordinates. Generous by design: it is a blunder detector, not a
# border.
DE_BBOX = (5.5, 47.0, 15.5, 55.4)  # min_lon, min_lat, max_lon, max_lat

WGS84_EPSG = 4326
TARGET_EPSG = 25832  # ETRS89 / UTM zone 32N — mandated storage CRS.


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the process configuration."""

    db_uri: str | None
    staging_schema: str
    user_agent: str
    http_timeout: float
    http_max_retries: int
    scrape_min_interval: float
    geocoder: str
    nominatim_url: str
    nominatim_min_interval: float
    geocode_cache: Path
    overpass_url: str
    overpass_min_interval: float
    overpass_cache: Path
    building_search_radius_m: float
    parking_search_radius_m: float
    data_dir: Path
    samples_dir: Path
    target_epsg: int = TARGET_EPSG
    source_epsg: int = WGS84_EPSG
    allowed_source_epsg: tuple[int, ...] = field(default=(WGS84_EPSG, TARGET_EPSG))

    def require_db_uri(self) -> str:
        if not self.db_uri:
            raise RuntimeError(
                "DB_URI is not set. Export it (see .env.example); the pipeline "
                "never embeds credentials."
            )
        return self.db_uri


def load_settings(dotenv: bool = True) -> Settings:
    """Build :class:`Settings` from the environment."""
    if dotenv:
        load_dotenv(override=False)

    root = Path(os.environ.get("IRIS_PROJECT_ROOT", ".")).resolve()
    data_dir = Path(os.environ.get("IRIS_DATA_DIR", root / "data"))
    samples_dir = Path(os.environ.get("IRIS_SAMPLES_DIR", root / "samples"))

    return Settings(
        db_uri=os.environ.get("DB_URI") or None,
        staging_schema=os.environ.get("IRIS_STAGING_SCHEMA", "iris_staging"),
        user_agent=os.environ.get(
            "IRIS_USER_AGENT",
            "IRIS-Wave4-TASK-CRE-41/1.0 (Project IRIS Wave 4 technical assessment; contact via submission repository)",
        ),
        http_timeout=_env_float("IRIS_HTTP_TIMEOUT", 30.0),
        http_max_retries=_env_int("IRIS_HTTP_MAX_RETRIES", 3),
        scrape_min_interval=_env_float("IRIS_SCRAPE_MIN_INTERVAL", 1.0),
        geocoder=os.environ.get("IRIS_GEOCODER", "nominatim"),
        nominatim_url=os.environ.get(
            "IRIS_NOMINATIM_URL", "https://nominatim.openstreetmap.org/search"
        ),
        # Nominatim's published policy is an absolute maximum of 1 req/s.
        nominatim_min_interval=max(
            1.0, _env_float("IRIS_NOMINATIM_MIN_INTERVAL", 1.1)
        ),
        geocode_cache=Path(
            os.environ.get("IRIS_GEOCODE_CACHE", data_dir / "geocode_cache.sqlite")
        ),
        overpass_url=os.environ.get(
            "IRIS_OVERPASS_URL", "https://overpass-api.de/api/interpreter"
        ),
        overpass_min_interval=max(
            1.0, _env_float("IRIS_OVERPASS_MIN_INTERVAL", 2.0)
        ),
        overpass_cache=Path(
            os.environ.get("IRIS_OVERPASS_CACHE", data_dir / "overpass_cache.sqlite")
        ),
        building_search_radius_m=_env_float("IRIS_BUILDING_SEARCH_RADIUS_M", 80.0),
        parking_search_radius_m=_env_float("IRIS_PARKING_SEARCH_RADIUS_M", 120.0),
        data_dir=Path(data_dir),
        samples_dir=Path(samples_dir),
    )
