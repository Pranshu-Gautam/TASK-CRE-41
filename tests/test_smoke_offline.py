"""The offline end-to-end smoke test, as executed by CI and by Docker.

No network, no database: proves collect -> normalise -> geocode -> enrich
wiring end to end against captured fixtures.
"""

from __future__ import annotations

import json

import pytest

from iris_cre41.httpclient import HttpError
from iris_cre41.pipeline import ENRICHED_ARTEFACT
from iris_cre41.smoke import (
    SMOKE_SELECTION,
    build_offline_components,
    run_offline_smoke_test,
)


@pytest.fixture
def smoke(settings):
    return run_offline_smoke_test(settings, skip_load=True)


def test_smoke_test_passes_offline(smoke):
    assert smoke["passed"] is True
    assert smoke["checks"]["locations_collected_equals_selection"] is True


def test_smoke_run_does_not_touch_the_real_artefacts(smoke, settings):
    """A smoke test must never overwrite a real run's output."""
    assert (settings.data_dir / "smoke" / ENRICHED_ARTEFACT).exists()
    assert not (settings.data_dir / ENRICHED_ARTEFACT).exists()


def test_every_selected_site_is_collected(smoke, settings):
    expected = sum(len(v) for v in SMOKE_SELECTION.values())
    assert smoke["stages"]["collect"]["locations_collected"] == expected
    assert smoke["stages"]["collect"]["failures"] == 0
    assert smoke["stages"]["collect"]["per_operator"] == {
        "mrwash": len(SMOKE_SELECTION["mrwash"]),
        "cleancar": len(SMOKE_SELECTION["cleancar"]),
    }


def test_all_sites_normalise_and_geocode(smoke):
    normalize = smoke["stages"]["normalize"]
    geocode = smoke["stages"]["geocode"]
    expected = sum(len(v) for v in SMOKE_SELECTION.values())
    assert normalize["canonical"] == expected
    assert geocode["geocoded_ok"] == expected
    assert geocode["projected_to_25832"] == expected


def test_enrichment_produces_metric_areas(smoke, settings):
    payload = json.loads(
        (settings.data_dir / "smoke" / ENRICHED_ARTEFACT).read_text(encoding="utf-8")
    )
    with_building = [poi for poi in payload["pois"] if poi["building"]]
    assert with_building, "expected at least one building match in the fixtures"
    for poi in with_building:
        building = poi["building"]
        assert building["area_sqm"] > 0
        assert building["geometry_type"] in {"Polygon", "MultiPolygon"}
        assert building["wkt_25832"].startswith(("POLYGON", "MULTIPOLYGON"))
        assert building["source"] == "openstreetmap"
        assert poi["geometry_provenance"]["building"]["ownership_verified"] is False
    assert payload["attribution"].startswith("© OpenStreetMap")


def test_sites_without_a_polygon_keep_their_point_and_null_metrics(smoke, settings):
    payload = json.loads(
        (settings.data_dir / "smoke" / ENRICHED_ARTEFACT).read_text(encoding="utf-8")
    )
    for poi in payload["pois"]:
        if poi["building"] is None:
            assert poi["point_wkt_25832"], "point geometry must be preserved"
            assert poi["enrichment_status"] in {"point_only", "partial", "failed"}


def test_offline_components_cannot_reach_the_network():
    _, geocoder, overpass = build_offline_components()
    assert not hasattr(geocoder.session, "session")
    assert not hasattr(overpass.session, "session")
    with pytest.raises(HttpError, match="no recorded"):
        geocoder.session.get("https://nominatim.openstreetmap.org/search",
                             params={"q": "nowhere"})
    with pytest.raises(HttpError, match="no recorded"):
        overpass.session.post("https://overpass-api.de/api/interpreter",
                              data={"data": "[out:json];out;"})


def test_fixture_fetcher_refuses_unregistered_urls():
    fetcher, _, _ = build_offline_components()
    with pytest.raises(FileNotFoundError):
        fetcher("https://www.mrwash.de/de/frontend/branch?id=999")


def test_smoke_run_is_reproducible(settings):
    first = run_offline_smoke_test(settings, skip_load=True)
    second = run_offline_smoke_test(settings, skip_load=True)
    for stage in ("collect", "normalize", "geocode", "enrich"):
        keys = [
            key
            for key in first["stages"][stage]
            if key not in {"started_at", "finished_at", "duration_s",
                           "collection_run_id", "artefact", "provider_stats"}
        ]
        assert {k: first["stages"][stage][k] for k in keys} == {
            k: second["stages"][stage][k] for k in keys
        }
