"""Geocoding: response interpretation, consistency checks, limits, caching."""

from __future__ import annotations

import json

import pytest

from iris_cre41.geocode import GeocodeQuery, NominatimGeocoder, SqliteCache, StaticGeocoder
from iris_cre41.geocode.nominatim import classify_precision
from iris_cre41.httpclient import HttpError, HttpResponse
from iris_cre41.models import NormalizedLocation


class FakeSession:
    """Returns queued payloads; records the params it was called with."""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, params=None, **kwargs):
        self.calls.append(params)
        if not self.payloads:
            raise AssertionError("geocoder issued more requests than expected")
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        body = json.dumps(payload)
        return HttpResponse(
            url=url, status_code=200, text=body, content=body.encode(),
            headers={"Content-Type": "application/json"}, elapsed_s=0.0, attempts=1,
        )

    def stats(self):
        return {"requests": len(self.calls)}


def result(lat, lon, **overrides):
    payload = {
        "lat": str(lat),
        "lon": str(lon),
        "display_name": "Mr. Wash, 10, Breslauer Straße, Aachen, 52068, Deutschland",
        "name": "Mr. Wash",
        "category": "amenity",
        "type": "car_wash",
        "addresstype": "amenity",
        "osm_type": "way",
        "osm_id": 31786622,
        "place_rank": 30,
        "importance": 0.4,
        "address": {"postcode": "52068", "city": "Aachen"},
    }
    payload.update(overrides)
    return payload


QUERY = GeocodeQuery("Breslauer Straße", "10", "52068", "Aachen", operator="Mr. Wash")


def geocoder(payloads, cache=None):
    return NominatimGeocoder(
        session=FakeSession(payloads), url="https://nominatim.test/search",
        min_interval=0.0, cache=cache,
    )


# -- happy path -------------------------------------------------------------

def test_single_result_is_ok_with_consistency_flags():
    outcome = geocoder([[result(50.7784123, 6.1188465)]]).geocode(QUERY)
    assert outcome.status == "ok"
    assert outcome.usable
    assert outcome.lat == pytest.approx(50.7784123)
    assert outcome.source_epsg == 4326
    assert outcome.postcode_match is True
    assert outcome.city_match is True
    assert outcome.name_operator_match is True
    assert outcome.precision == "address"
    assert outcome.ambiguous is False
    assert outcome.result_count == 1


def test_structured_query_uses_street_with_house_number_first():
    instance = geocoder([[result(50.0, 6.0)]])
    instance.geocode(QUERY)
    params = instance.session.calls[0]
    assert params["street"] == "10 Breslauer Straße"
    assert params["postalcode"] == "52068"
    assert params["city"] == "Aachen"
    assert params["countrycodes"] == "de"


def test_one_request_for_a_clean_address():
    instance = geocoder([[result(50.0, 6.0)]])
    instance.geocode(QUERY)
    assert len(instance.session.calls) == 1


# -- ambiguity --------------------------------------------------------------

def test_two_distant_candidates_are_reported_as_ambiguous():
    outcome = geocoder(
        [[result(50.7784, 6.1188, importance=0.5),
          result(50.9000, 6.5000, importance=0.4)]]
    ).geocode(QUERY)
    assert outcome.status == "ambiguous_resolved"
    assert outcome.ambiguous is True
    assert outcome.ambiguity_spread_m > 150
    assert outcome.alternatives_rejected == 1
    # The highest-importance candidate is still the one returned.
    assert outcome.lat == pytest.approx(50.7784)


def test_nearby_duplicates_are_not_ambiguous():
    outcome = geocoder(
        [[result(53.0556959, 8.7973913, importance=0.5),
          result(53.0558, 8.7974, importance=0.4)]]
    ).geocode(QUERY)
    assert outcome.status == "ok"
    assert outcome.ambiguous is False
    assert outcome.ambiguity_spread_m < 150


def test_best_candidate_selection_is_deterministic():
    payload = [result(50.1, 6.1, importance=0.2), result(50.2, 6.2, importance=0.9)]
    first = geocoder([list(payload)]).geocode(QUERY)
    second = geocoder([list(reversed(payload))]).geocode(QUERY)
    assert first.lat == second.lat == pytest.approx(50.2)


# -- consistency ------------------------------------------------------------

def test_postcode_mismatch_is_flagged_not_hidden():
    outcome = geocoder(
        [[result(50.0, 6.0, address={"postcode": "99999", "city": "Aachen"})]]
    ).geocode(QUERY)
    assert outcome.postcode_match is False
    assert outcome.returned_postcode == "99999"
    assert outcome.status == "ok"  # still returned, but flagged


def test_city_mismatch_is_flagged():
    outcome = geocoder(
        [[result(50.0, 6.0, address={"postcode": "52068", "city": "Köln"})]]
    ).geocode(QUERY)
    assert outcome.city_match is False


def test_city_match_tolerates_district_names():
    query = GeocodeQuery("Steilshooper Straße", "321", "22309", "Wandsbek")
    outcome = geocoder(
        [[result(53.6, 10.05, address={"postcode": "22309", "borough": "Wandsbek",
                                       "city": "Hamburg"})]]
    ).geocode(query)
    assert outcome.city_match is True


def test_operator_name_mismatch_is_detected():
    outcome = geocoder(
        [[result(53.6, 10.05, name="A - Z Autowaschanlagen GmbH",
                 display_name="A - Z Autowaschanlagen GmbH, 321, Steilshooper Straße")]]
    ).geocode(GeocodeQuery("Steilshooper Straße", "321", "22309", "Hamburg",
                           operator="Clean Car"))
    assert outcome.name_operator_match is False


# -- precision --------------------------------------------------------------

@pytest.mark.parametrize(
    "addresstype,rank,expected",
    [("building", 30, "address"), ("amenity", 30, "address"), ("shop", 30, "address"),
     ("tertiary", 26, "street"), ("residential", 26, "street"),
     ("place", 16, "locality"), ("postcode", 21, "locality"),
     (None, 30, "address"), (None, 26, "street"), (None, 10, "locality"),
     (None, None, "unknown")],
)
def test_classify_precision(addresstype, rank, expected):
    assert classify_precision(addresstype, rank) == expected


def test_street_level_result_is_marked_street_precision():
    outcome = geocoder(
        [[result(53.0, 9.0, addresstype="tertiary", place_rank=26, category="highway",
                 type="tertiary")]]
    ).geocode(QUERY)
    assert outcome.precision == "street"


# -- failure modes ----------------------------------------------------------

def test_no_results_walks_the_strategy_ladder_then_reports_not_found():
    instance = geocoder([[], [], [], []])
    outcome = instance.geocode(QUERY)
    assert outcome.status == "not_found"
    assert outcome.lat is None and outcome.lon is None
    assert len(instance.session.calls) == 4
    strategies = [
        "city" in (params or {}) for params in instance.session.calls
    ]
    assert strategies == [True, False, False, False]


def test_fallback_strategy_is_recorded_when_it_succeeds():
    outcome = geocoder([[], [result(45.0, 6.0)]]).geocode(QUERY)
    assert outcome.status == "ok"
    assert outcome.strategy == "structured_no_city"


def test_unparsable_coordinates_are_reported_as_invalid():
    outcome = geocoder([[result("not-a-number", 6.0)]]).geocode(QUERY)
    assert outcome.status == "invalid_coordinates"
    assert outcome.lat is None
    assert outcome.usable is False


def test_transport_errors_are_surfaced_as_error_status():
    outcome = geocoder(
        [HttpError("HTTP 503"), HttpError("HTTP 503"), HttpError("HTTP 503"),
         HttpError("HTTP 503")]
    ).geocode(QUERY)
    assert outcome.status == "error"
    assert "503" in outcome.error
    assert outcome.usable is False


def test_empty_address_is_refused_without_a_request():
    instance = geocoder([])
    outcome = instance.geocode(GeocodeQuery(None, None, None, None))
    assert outcome.status == "error"
    assert instance.session.calls == []


def test_rate_limit_floor_cannot_be_disabled():
    instance = NominatimGeocoder(session=FakeSession([]), min_interval=0.01)
    assert instance.min_interval >= 1.0


# -- caching ----------------------------------------------------------------

def test_second_identical_query_is_served_from_cache(tmp_path):
    cache = SqliteCache(tmp_path / "cache.sqlite")
    instance = geocoder([[result(50.0, 6.0)]], cache=cache)
    first = instance.geocode(QUERY)
    second = instance.geocode(QUERY)
    assert first.lat == second.lat
    assert len(instance.session.calls) == 1
    assert instance.cache_hits == 1
    assert cache.stats()["hits"] == 1


def test_cache_distinguishes_different_addresses(tmp_path):
    cache = SqliteCache(tmp_path / "cache.sqlite")
    instance = geocoder([[result(50.0, 6.0)], [result(53.0, 9.0)]], cache=cache)
    instance.geocode(QUERY)
    other = instance.geocode(GeocodeQuery("Andere Straße", "1", "20095", "Hamburg"))
    assert other.lat == pytest.approx(53.0)
    assert len(instance.session.calls) == 2


# -- static provider --------------------------------------------------------

def test_static_geocoder_never_invents_coordinates():
    provider = StaticGeocoder({})
    outcome = provider.geocode(QUERY)
    assert outcome.status == "not_found"
    assert outcome.lat is None


def test_query_from_location():
    location = NormalizedLocation(
        poi_uid="x", source_uid="y", dedup_key="z", operator="Mr. Wash",
        operator_slug="mrwash", location_name="Aachen", location_name_raw="Aachen",
        street="Breslauer Straße", house_number="10", postal_code="52068",
        city="Aachen", country="DE", address_raw=None, address_normalized=None,
    )
    query = GeocodeQuery.from_location(location)
    assert query.street_line == "10 Breslauer Straße"
    assert query.operator == "Mr. Wash"
    assert query.freeform() == "Breslauer Straße 10, 52068 Aachen, Deutschland"
    assert query.freeform(include_city=False) == (
        "Breslauer Straße 10, 52068, Deutschland"
    )
