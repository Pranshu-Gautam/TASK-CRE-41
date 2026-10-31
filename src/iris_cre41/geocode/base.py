"""Geocoder interface and the trivial providers used by tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..models import GeocodeResult, NormalizedLocation, utcnow


@dataclass(frozen=True)
class GeocodeQuery:
    """Structured address query. Built only from observed source values."""

    street: str | None
    house_number: str | None
    postal_code: str | None
    city: str | None
    country_code: str = "DE"
    operator: str | None = None

    @classmethod
    def from_location(cls, location: NormalizedLocation) -> "GeocodeQuery":
        return cls(
            street=location.street,
            house_number=location.house_number,
            postal_code=location.postal_code,
            city=location.city,
            country_code=location.country or "DE",
            operator=location.operator,
        )

    @property
    def street_line(self) -> str | None:
        parts = [p for p in (self.house_number, self.street) if p]
        if not parts:
            return None
        # Nominatim expects "<house number> <street>" in the `street` field.
        return " ".join(parts) if self.house_number else self.street

    def freeform(self, include_city: bool = True) -> str:
        """Free-text query string. ``include_city=False`` drops the city name.

        Dropping the city is the documented last resort: German postal codes
        are unique, and some sources publish an abbreviated city name
        ("Mülheim a.d.R.") that the geocoder cannot resolve.
        """
        locality = [self.postal_code]
        if include_city:
            locality.append(self.city)
        parts = [
            " ".join(p for p in (self.street, self.house_number) if p),
            " ".join(p for p in locality if p),
            "Deutschland" if self.country_code.upper() == "DE" else self.country_code,
        ]
        return ", ".join(p for p in parts if p)

    def as_dict(self) -> dict[str, Any]:
        return {
            "street": self.street,
            "house_number": self.house_number,
            "postal_code": self.postal_code,
            "city": self.city,
            "country_code": self.country_code,
            "operator": self.operator,
        }


class Geocoder(Protocol):
    name: str

    def geocode(self, query: GeocodeQuery) -> GeocodeResult: ...

    def stats(self) -> dict[str, Any]: ...


class StaticGeocoder:
    """Deterministic test double. Returns only what it was given.

    Any address not present in the mapping yields ``status="not_found"``; it
    never fabricates a coordinate.
    """

    name = "static-fixture"

    def __init__(self, mapping: dict[str, tuple[float, float]] | None = None) -> None:
        self.mapping = mapping or {}
        self.calls = 0

    @staticmethod
    def key(query: GeocodeQuery) -> str:
        return query.freeform().lower()

    def geocode(self, query: GeocodeQuery) -> GeocodeResult:
        self.calls += 1
        coords = self.mapping.get(self.key(query))
        if coords is None:
            return GeocodeResult(
                status="not_found",
                provider=self.name,
                query=query.freeform(),
                strategy="fixture",
                result_count=0,
                geocoded_at=utcnow(),
            )
        lat, lon = coords
        return GeocodeResult(
            status="ok",
            provider=self.name,
            query=query.freeform(),
            strategy="fixture",
            lat=lat,
            lon=lon,
            source_epsg=4326,
            display_name=query.freeform(),
            match_type="fixture",
            result_count=1,
            postcode_match=True,
            city_match=True,
            geocoded_at=utcnow(),
        )

    def stats(self) -> dict[str, Any]:
        return {"provider": self.name, "calls": self.calls}


def build_geocoder(settings, session=None) -> Geocoder:
    """Factory selecting the provider named by ``IRIS_GEOCODER``."""
    from ..httpclient import PoliteSession
    from .cache import SqliteCache
    from .nominatim import NominatimGeocoder

    name = (settings.geocoder or "nominatim").lower()
    if name in {"static", "fixture", "none"}:
        return StaticGeocoder()
    if name != "nominatim":
        raise ValueError(
            f"unknown geocoder {name!r}; supported: nominatim, static"
        )
    session = session or PoliteSession(
        user_agent=settings.user_agent,
        timeout=settings.http_timeout,
        max_retries=settings.http_max_retries,
        min_interval=settings.nominatim_min_interval,
    )
    return NominatimGeocoder(
        session=session,
        url=settings.nominatim_url,
        min_interval=settings.nominatim_min_interval,
        cache=SqliteCache(settings.geocode_cache),
    )
