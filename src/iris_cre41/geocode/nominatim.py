"""OSM Nominatim geocoder.

Operated under the public usage policy
(https://operations.osmfoundation.org/policies/nominatim/):

* absolute maximum of 1 request/second — enforced in :class:`PoliteSession`
  with a configurable floor that cannot be set below 1.0s;
* a genuine, identifying User-Agent with a contact address;
* results cached on disk so repeated runs do not re-query the service.

Data: (C) OpenStreetMap contributors, ODbL 1.0.

The geocoder never invents a coordinate. Every outcome is reported with an
explicit status and, when a result is returned, with address-consistency
flags that let a reviewer judge how much to trust it.
"""

from __future__ import annotations

import math
from typing import Any

from ..httpclient import HttpError, PoliteSession
from ..logging_setup import get_logger
from ..models import GeocodeResult, utcnow
from ..normalize import normalize_token
from .base import GeocodeQuery
from .cache import SqliteCache, cache_key

log = get_logger(__name__)

NAMESPACE = "nominatim/search"

# The two best-ranked candidates being further apart than this is treated as a
# genuine ambiguity: the service is offering materially different places for
# the same address, so the chosen one is reported as "ambiguous_resolved".
AMBIGUITY_THRESHOLD_M = 150.0

# Tokens that identify our operators inside a returned display name.
_OPERATOR_TOKENS = {
    "Mr. Wash": ("mr wash", "mrwash"),
    "Clean Car": ("clean car", "cleancar"),
}

# How precisely the returned object pins the address. A street-level match
# means the house number could not be resolved, which matters a great deal for
# a site-level footprint analysis, so it is recorded rather than smoothed over.
_ADDRESS_LEVEL_TYPES = {
    "building", "house", "amenity", "shop", "office", "industrial", "commercial",
    "retail", "craft", "leisure", "tourism", "man_made", "healthcare", "club",
}
_STREET_LEVEL_TYPES = {
    "road", "street", "primary", "secondary", "tertiary", "residential",
    "unclassified", "service", "living_street", "pedestrian", "track",
    "trunk", "motorway", "footway", "path", "cycleway",
}
_LOCALITY_LEVEL_TYPES = {
    "place", "city", "town", "village", "hamlet", "suburb", "neighbourhood",
    "quarter", "postcode", "municipality", "borough", "district", "county",
    "state", "country",
}


def classify_precision(addresstype: str | None, place_rank: int | None) -> str:
    """Map a Nominatim result onto a coarse precision class."""
    key = (addresstype or "").strip().lower()
    if key in _ADDRESS_LEVEL_TYPES:
        return "address"
    if key in _STREET_LEVEL_TYPES:
        return "street"
    if key in _LOCALITY_LEVEL_TYPES:
        return "locality"
    if place_rank is not None:
        # Nominatim place_rank: 30 = house/building, 26-27 = street, <= 20 = area.
        if place_rank >= 30:
            return "address"
        if place_rank >= 26:
            return "street"
        return "locality"
    return "unknown"


_CITY_KEYS = (
    "city",
    "town",
    "village",
    "municipality",
    "city_district",
    "borough",
    "suburb",
    "county",
)


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


class NominatimGeocoder:
    name = "nominatim"

    def __init__(
        self,
        session: PoliteSession,
        url: str = "https://nominatim.openstreetmap.org/search",
        min_interval: float = 1.1,
        cache: SqliteCache | None = None,
    ) -> None:
        self.session = session
        self.url = url
        self.min_interval = max(1.0, min_interval)
        self.cache = cache
        self.calls = 0
        self.cache_hits = 0
        self.errors = 0

    # -- request plumbing --------------------------------------------------
    def _search(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        key = cache_key(NAMESPACE, {"url": self.url, "params": params})
        if self.cache is not None:
            cached = self.cache.get(key)
            if cached is not None:
                self.cache_hits += 1
                return cached
        response = self.session.get(
            self.url, params=params, min_interval=self.min_interval, expect_json=True
        )
        self.calls += 1
        payload = response.json()
        if not isinstance(payload, list):
            raise HttpError(f"unexpected Nominatim payload type: {type(payload)!r}")
        if self.cache is not None:
            self.cache.put(key, NAMESPACE, {"url": self.url, "params": params}, payload)
        return payload

    def _structured_params(
        self, query: GeocodeQuery, include_city: bool = True
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "format": "jsonv2",
            "addressdetails": 1,
            "limit": 5,
            "countrycodes": query.country_code.lower(),
        }
        if query.street_line:
            params["street"] = query.street_line
        if query.postal_code:
            params["postalcode"] = query.postal_code
        if query.city and include_city:
            params["city"] = query.city
        return params

    def _freeform_params(
        self, query: GeocodeQuery, include_city: bool = True
    ) -> dict[str, Any]:
        return {
            "format": "jsonv2",
            "addressdetails": 1,
            "limit": 5,
            "countrycodes": query.country_code.lower(),
            "q": query.freeform(include_city=include_city),
        }

    # -- result interpretation --------------------------------------------
    @staticmethod
    def _consistency(
        query: GeocodeQuery, address: dict[str, Any]
    ) -> tuple[bool | None, bool | None, str | None, str | None]:
        returned_postcode = address.get("postcode")
        postcode_match: bool | None = None
        if query.postal_code and returned_postcode:
            postcode_match = (
                "".join(ch for ch in str(returned_postcode) if ch.isdigit())
                == query.postal_code
            )

        returned_city = next(
            (address[key] for key in _CITY_KEYS if address.get(key)), None
        )
        city_match: bool | None = None
        if query.city:
            wanted = normalize_token(query.city)
            candidates = {
                normalize_token(str(address[key])) for key in _CITY_KEYS if address.get(key)
            }
            if candidates:
                city_match = any(
                    wanted == candidate
                    or wanted in candidate
                    or candidate in wanted
                    for candidate in candidates
                )
        return postcode_match, city_match, returned_postcode, returned_city

    def _build_result(
        self,
        query: GeocodeQuery,
        results: list[dict[str, Any]],
        strategy: str,
        query_string: str,
    ) -> GeocodeResult:
        if not results:
            return GeocodeResult(
                status="not_found",
                provider=self.name,
                query=query_string,
                strategy=strategy,
                result_count=0,
                geocoded_at=utcnow(),
            )

        # Deterministic pick: highest importance, then lowest place_rank,
        # then original service order.
        ordered = sorted(
            enumerate(results),
            key=lambda pair: (
                -float(pair[1].get("importance") or 0.0),
                int(pair[1].get("place_rank") or 99),
                pair[0],
            ),
        )
        best = ordered[0][1]

        spread: float | None = None
        ranked_coords: list[tuple[float, float]] = []
        for _, item in ordered:
            try:
                ranked_coords.append((float(item["lat"]), float(item["lon"])))
            except (KeyError, TypeError, ValueError):
                continue
        if len(ranked_coords) > 1:
            spread = _haversine_m(*ranked_coords[0], *ranked_coords[1])
        ambiguous = bool(spread is not None and spread > AMBIGUITY_THRESHOLD_M)

        name_operator_match: bool | None = None
        tokens = _OPERATOR_TOKENS.get(query.operator or "")
        if tokens:
            haystack = normalize_token(
                f"{best.get('name') or ''} {best.get('display_name') or ''}"
            )
            name_operator_match = any(token in haystack for token in tokens)

        address = best.get("address") or {}
        postcode_match, city_match, returned_postcode, returned_city = self._consistency(
            query, address
        )

        try:
            lat = float(best["lat"])
            lon = float(best["lon"])
        except (KeyError, TypeError, ValueError) as exc:
            self.errors += 1
            return GeocodeResult(
                status="invalid_coordinates",
                provider=self.name,
                query=query_string,
                strategy=strategy,
                result_count=len(results),
                error=f"unparsable coordinate in response: {exc}",
                geocoded_at=utcnow(),
            )

        return GeocodeResult(
            status="ambiguous_resolved" if ambiguous else "ok",
            provider=self.name,
            query=query_string,
            strategy=strategy,
            lat=lat,
            lon=lon,
            source_epsg=4326,
            display_name=best.get("display_name"),
            match_type=f"{best.get('category', '?')}/{best.get('type', '?')}"
            f"@{best.get('addresstype', '?')}",
            osm_type=best.get("osm_type"),
            osm_id=str(best.get("osm_id")) if best.get("osm_id") is not None else None,
            place_rank=(
                int(best["place_rank"]) if best.get("place_rank") is not None else None
            ),
            precision=classify_precision(
                best.get("addresstype"), best.get("place_rank")
            ),
            importance=(
                float(best["importance"]) if best.get("importance") is not None else None
            ),
            result_count=len(results),
            ambiguous=ambiguous,
            ambiguity_spread_m=round(spread, 1) if spread is not None else None,
            postcode_match=postcode_match,
            city_match=city_match,
            name_operator_match=name_operator_match,
            alternatives_rejected=max(0, len(results) - 1),
            returned_postcode=returned_postcode,
            returned_city=returned_city,
            geocoded_at=utcnow(),
        )

    # -- public API --------------------------------------------------------
    def geocode(self, query: GeocodeQuery) -> GeocodeResult:
        if not any([query.street, query.postal_code, query.city]):
            return GeocodeResult(
                status="error",
                provider=self.name,
                query="",
                strategy="structured",
                error="empty address: nothing to geocode",
                geocoded_at=utcnow(),
            )

        # Ordered, bounded strategy ladder. Each step is only attempted when
        # the previous one returned no result at all, so a clean address costs
        # exactly one request. Every attempt is recorded in `strategy`.
        attempts: list[tuple[str, dict[str, Any], str]] = [
            ("structured", self._structured_params(query), query.freeform()),
            (
                "structured_no_city",
                self._structured_params(query, include_city=False),
                query.freeform(include_city=False),
            ),
            ("freeform", self._freeform_params(query), query.freeform()),
            (
                "freeform_no_city",
                self._freeform_params(query, include_city=False),
                query.freeform(include_city=False),
            ),
        ]
        if not query.city:
            attempts = [a for a in attempts if not a[0].endswith("_no_city")]

        last_error: str | None = None
        for strategy, params, query_string in attempts:
            try:
                results = self._search(params)
            except (HttpError, ValueError) as exc:
                self.errors += 1
                last_error = f"{type(exc).__name__}: {exc}"
                log.error(
                    "geocode_request_failed",
                    extra={"strategy": strategy, "error": last_error},
                )
                continue
            result = self._build_result(query, results, strategy, query_string)
            if result.status != "not_found":
                log.info(
                    "geocode_result",
                    extra={
                        "status": result.status,
                        "strategy": strategy,
                        "query": query_string,
                        "postcode_match": result.postcode_match,
                        "city_match": result.city_match,
                        "results": result.result_count,
                    },
                )
                return result

        if last_error:
            return GeocodeResult(
                status="error",
                provider=self.name,
                query=query.freeform(),
                strategy="structured+freeform",
                error=last_error,
                geocoded_at=utcnow(),
            )
        log.warning("geocode_not_found", extra={"query": query.freeform()})
        return GeocodeResult(
            status="not_found",
            provider=self.name,
            query=query.freeform(),
            strategy="all_strategies_exhausted",
            result_count=0,
            geocoded_at=utcnow(),
        )

    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "provider": self.name,
            "requests": self.calls,
            "cache_hits": self.cache_hits,
            "errors": self.errors,
        }
        if self.cache is not None:
            out["cache"] = self.cache.stats()
        out["http"] = self.session.stats()
        return out
