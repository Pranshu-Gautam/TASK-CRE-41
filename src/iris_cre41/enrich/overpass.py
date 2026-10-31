"""Overpass API client.

Source: OpenStreetMap via https://overpass-api.de — data (C) OpenStreetMap
contributors, licensed ODbL 1.0. Attribution is mandatory for any downstream
publication of derived areas.

Usage etiquette: one request per site, >= 2s apart by default, hard timeout,
bounded retries, and an on-disk cache so re-runs do not re-query the service.
``out geom`` is requested so way and relation-member geometries arrive inline
and no second round of node lookups is needed.
"""

from __future__ import annotations

from typing import Any

from ..httpclient import HttpError, PoliteSession
from ..logging_setup import get_logger
from ..geocode.cache import SqliteCache, cache_key

log = get_logger(__name__)

NAMESPACE = "overpass/site"
FEATURE_URL = "https://www.openstreetmap.org/{osm_type}/{osm_id}"


def build_site_query(
    lat: float,
    lon: float,
    building_radius_m: float,
    parking_radius_m: float,
    timeout_s: int = 90,
) -> str:
    """Overpass QL for one site: buildings, parking areas, car-wash features."""
    building_radius = int(round(building_radius_m))
    parking_radius = int(round(parking_radius_m))
    position = f"{lat:.7f},{lon:.7f}"
    return (
        f"[out:json][timeout:{timeout_s}];\n"
        "(\n"
        f'  way(around:{building_radius},{position})["building"];\n'
        f'  relation(around:{building_radius},{position})["building"]["type"="multipolygon"];\n'
        f'  way(around:{parking_radius},{position})["amenity"="parking"];\n'
        f'  relation(around:{parking_radius},{position})["amenity"="parking"]["type"="multipolygon"];\n'
        f'  nwr(around:{parking_radius},{position})["amenity"="car_wash"];\n'
        f'  nwr(around:{parking_radius},{position})["shop"="car_wash"];\n'
        ");\n"
        "out geom tags;"
    )


def feature_id(element: dict[str, Any]) -> str:
    return f"{element.get('type')}/{element.get('id')}"


def feature_url(element: dict[str, Any]) -> str:
    return FEATURE_URL.format(
        osm_type=element.get("type"), osm_id=element.get("id")
    )


class OverpassClient:
    def __init__(
        self,
        session: PoliteSession,
        url: str = "https://overpass-api.de/api/interpreter",
        min_interval: float = 2.0,
        cache: SqliteCache | None = None,
    ) -> None:
        self.session = session
        self.url = url
        self.min_interval = max(1.0, min_interval)
        self.cache = cache
        self.requests = 0
        self.cache_hits = 0
        self.errors = 0

    def fetch_site(
        self,
        lat: float,
        lon: float,
        building_radius_m: float,
        parking_radius_m: float,
    ) -> dict[str, Any]:
        """Return the raw Overpass payload for one site (cached)."""
        query = build_site_query(lat, lon, building_radius_m, parking_radius_m)
        key = cache_key(NAMESPACE, {"url": self.url, "query": query})
        if self.cache is not None:
            cached = self.cache.get(key)
            if cached is not None:
                self.cache_hits += 1
                return cached
        try:
            response = self.session.post(
                self.url,
                data={"data": query},
                min_interval=self.min_interval,
                expect_json=True,
            )
            payload = response.json()
        except (HttpError, ValueError) as exc:
            self.errors += 1
            log.error(
                "overpass_request_failed",
                extra={"error": f"{type(exc).__name__}: {exc}", "lat": lat, "lon": lon},
            )
            raise
        self.requests += 1
        if not isinstance(payload, dict) or "elements" not in payload:
            self.errors += 1
            raise HttpError("unexpected Overpass payload shape")
        if self.cache is not None:
            self.cache.put(key, NAMESPACE, {"url": self.url, "query": query}, payload)
        return payload

    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "provider": "overpass-api.de",
            "requests": self.requests,
            "cache_hits": self.cache_hits,
            "errors": self.errors,
        }
        if self.cache is not None:
            out["cache"] = self.cache.stats()
        out["http"] = self.session.stats()
        return out
