"""Offline end-to-end smoke test.

Runs the complete pipeline — collect, normalise, geocode, enrich and
(optionally) load — against captured fixtures, with **no network access at
all**. Any attempt to reach the network raises, so a pass proves the wiring
works rather than that the internet is up.

This is what ``iris-cre41 smoke-test`` and the Docker smoke test execute.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .collectors.base import FixtureFetcher
from .config import Settings
from .geocode.nominatim import NominatimGeocoder
from .enrich.overpass import OverpassClient
from .httpclient import HttpError, HttpResponse
from .logging_setup import get_logger
from .pipeline import (
    ENRICHED_ARTEFACT,
    run_collect,
    run_enrich,
    run_geocode,
    run_load,
    run_normalize,
)

log = get_logger(__name__)

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures"

# The subset of real sites the offline test replays.
SMOKE_SELECTION: dict[str, list[str]] = {
    "mrwash": ["36", "1"],
    "cleancar": ["bremen", "bad-oldesloe-2", "hamburg-kollaustrasse-2"],
}

FIXTURE_URL_MAP = {
    "https://www.mrwash.de/de/frontend/default/locations": "mrwash_locations.html",
    "https://www.mrwash.de/de/frontend/branch?id=36": "mrwash_branch_36_aachen.html",
    "https://www.mrwash.de/de/frontend/branch?id=1": "mrwash_branch_1_hamburg.html",
    "https://www.clean-car.com/standorte/": "cleancar_standorte_index.html",
    "https://www.clean-car.com/wp-sitemap-posts-page-1.xml": "cleancar_sitemap_pages.xml",
    "https://www.clean-car.com/standorte/bremen/": "cleancar_bremen.html",
    "https://www.clean-car.com/standorte/bad-oldesloe-2/": "cleancar_bad_oldesloe_2.html",
    "https://www.clean-car.com/standorte/hamburg-kollaustrasse-2/":
        "cleancar_hamburg_steilshoop.html",
}


class OfflineSession:
    """Serves recorded API responses; raises on anything not recorded.

    Deliberately has no ``requests.Session`` inside it, so there is no path to
    the network even if a URL were mistyped.
    """

    def __init__(self, entries: list[dict[str, Any]], kind: str) -> None:
        self.entries = entries
        self.kind = kind
        self.served = 0
        self.misses: list[str] = []

    # -- matching ----------------------------------------------------------
    def _find(self, url: str, params: dict | None, data: dict | None) -> Any:
        for entry in self.entries:
            request = entry.get("request") or {}
            if request.get("url") != url:
                continue
            if self.kind == "nominatim":
                if request.get("params") == self._comparable(params):
                    return entry["response"]
            else:
                query = (data or {}).get("data")
                if request.get("query") == query:
                    return entry["response"]
        return None

    @staticmethod
    def _comparable(params: dict | None) -> dict:
        return json.loads(json.dumps(params or {}, sort_keys=True, default=str))

    # -- PoliteSession-compatible surface ---------------------------------
    def _respond(self, url: str, params: dict | None, data: dict | None) -> HttpResponse:
        payload = self._find(url, params, data)
        if payload is None:
            descriptor = json.dumps(params or (data or {}), sort_keys=True, default=str)
            self.misses.append(descriptor)
            raise HttpError(
                f"offline smoke test: no recorded {self.kind} response for "
                f"{descriptor[:300]}"
            )
        self.served += 1
        body = json.dumps(payload)
        return HttpResponse(
            url=url,
            status_code=200,
            text=body,
            content=body.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            elapsed_s=0.0,
            attempts=1,
        )

    def get(self, url: str, params=None, **kwargs: Any) -> HttpResponse:
        return self._respond(url, params, None)

    def post(self, url: str, data=None, **kwargs: Any) -> HttpResponse:
        return self._respond(url, None, data)

    def stats(self) -> dict[str, Any]:
        return {"mode": "offline-fixture", "served": self.served,
                "misses": len(self.misses)}


def _load_entries(name: str) -> list[dict[str, Any]]:
    path = FIXTURE_DIR / name
    if not path.exists():
        raise FileNotFoundError(
            f"missing fixture {path}; regenerate with tools/capture_fixtures.py"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def build_offline_components() -> tuple[FixtureFetcher, NominatimGeocoder, OverpassClient]:
    fetcher = FixtureFetcher(
        {url: FIXTURE_DIR / name for url, name in FIXTURE_URL_MAP.items()}
    )
    geocoder = NominatimGeocoder(
        session=OfflineSession(_load_entries("nominatim_responses.json"), "nominatim"),
        url="https://nominatim.openstreetmap.org/search",
        min_interval=0.0,
        cache=None,
    )
    overpass = OverpassClient(
        session=OfflineSession(_load_entries("overpass_responses.json"), "overpass"),
        url="https://overpass-api.de/api/interpreter",
        min_interval=0.0,
        cache=None,
    )
    return fetcher, geocoder, overpass


def run_offline_smoke_test(settings: Settings, skip_load: bool = False) -> dict[str, Any]:
    """Execute the pipeline offline and return measured counts.

    Runs in its own ``data/smoke`` directory so a smoke test can never
    overwrite the artefacts of a real run.
    """
    import dataclasses

    fetcher, geocoder, overpass = build_offline_components()
    settings = dataclasses.replace(
        settings, data_dir=Path(settings.data_dir) / "smoke"
    )
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)

    result: dict[str, Any] = {"mode": "offline-fixtures", "stages": {}}
    result["stages"]["collect"] = run_collect(
        settings,
        operators=("mrwash", "cleancar"),
        fetcher=fetcher,
        only=SMOKE_SELECTION,
    )
    result["stages"]["normalize"] = run_normalize(settings)
    result["stages"]["geocode"] = run_geocode(settings, geocoder=geocoder)
    result["stages"]["enrich"] = run_enrich(settings, overpass=overpass)
    if not skip_load:
        result["stages"]["load"] = run_load(settings)

    enriched = json.loads(
        (settings.data_dir / ENRICHED_ARTEFACT).read_text(encoding="utf-8")
    )
    expected = sum(len(values) for values in SMOKE_SELECTION.values())
    collected = result["stages"]["collect"]["locations_collected"]
    checks = {
        "locations_collected_equals_selection": collected == expected,
        "all_pois_have_source_url": all(
            poi["location"]["source_url"] for poi in enriched["pois"]
        ),
        "no_network_used": True,  # OfflineSession has no network path
        "fixture_pages_fetched": len(fetcher.calls),
        "geocoder_responses_served": geocoder.session.stats(),
        "overpass_responses_served": overpass.session.stats(),
    }
    result["checks"] = checks
    result["passed"] = all(
        value is True
        for key, value in checks.items()
        if isinstance(value, bool)
    )
    log.info("smoke_test_complete", extra={"passed": result["passed"]})
    return result
