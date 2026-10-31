#!/usr/bin/env python3
"""Capture real fixtures from the live sources (reproducible, documented).

Run from the project root:

    python tools/capture_fixtures.py

What it does, and why:

* fetches the exact URLs the collectors use, with the project's polite HTTP
  client (identifying User-Agent, >= 1 req/s);
* strips ``<script>``, ``<style>``, ``<link>``, ``<svg>``, ``<noscript>`` and
  HTML comments before writing. Those tags are already discarded by the
  parsers, and removing them cuts fixture size by ~85% while leaving the DOM
  that the parsers actually read completely untouched;
* records, for every fixture, the source URL, the fetch timestamp and the
  sha256 of the *original* (unmodified) response in ``MANIFEST.json`` so the
  provenance of each file is verifiable.

The JSON fixtures for Nominatim/Overpass are verbatim API responses, taken
straight from the pipeline's on-disk response cache.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from iris_cre41.config import load_settings  # noqa: E402
from iris_cre41.httpclient import PoliteSession  # noqa: E402
from iris_cre41.logging_setup import setup_logging  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"

HTML_TARGETS = {
    "mrwash_locations.html": "https://www.mrwash.de/de/frontend/default/locations",
    "mrwash_branch_36_aachen.html": "https://www.mrwash.de/de/frontend/branch?id=36",
    "mrwash_branch_1_hamburg.html": "https://www.mrwash.de/de/frontend/branch?id=1",
    "mrwash_branch_404.html": "https://www.mrwash.de/de/frontend/branch?id=10",
    "cleancar_standorte_index.html": "https://www.clean-car.com/standorte/",
    "cleancar_sitemap_pages.xml": "https://www.clean-car.com/wp-sitemap-posts-page-1.xml",
    "cleancar_bremen.html": "https://www.clean-car.com/standorte/bremen/",
    "cleancar_bad_oldesloe_2.html": "https://www.clean-car.com/standorte/bad-oldesloe-2/",
    "cleancar_hamburg_steilshoop.html": "https://www.clean-car.com/standorte/hamburg-kollaustrasse-2/",
}

_STRIP_RE = re.compile(
    r"<(script|style|svg|noscript)\b[^>]*>.*?</\1>|<link\b[^>]*/?>|<!--.*?-->",
    re.DOTALL | re.IGNORECASE,
)


def shrink(text: str, is_xml: bool) -> str:
    if is_xml:
        return text
    cleaned = _STRIP_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", cleaned)


def _cache_rows(path: Path, namespace: str) -> list[tuple[str, str]]:
    if not Path(path).exists():
        return []
    connection = sqlite3.connect(str(path))
    rows = connection.execute(
        "SELECT request, response FROM response_cache WHERE namespace = ?",
        (namespace,),
    ).fetchall()
    connection.close()
    return rows


def capture_api_fixtures(settings, manifest: dict) -> dict[str, int]:
    """Write the Nominatim/Overpass fixtures for the smoke-test sites only."""
    from iris_cre41.enrich.overpass import build_site_query
    from iris_cre41.geocode.base import GeocodeQuery
    from iris_cre41.geocode.nominatim import NominatimGeocoder
    from iris_cre41.models import EnrichedPoi
    from iris_cre41.pipeline import GEOCODED_ARTEFACT
    from iris_cre41.smoke import SMOKE_SELECTION

    counts: dict[str, int] = {}
    artefact = Path(settings.data_dir) / GEOCODED_ARTEFACT
    if not artefact.exists():
        print(f"  (skipping API fixtures: {artefact} not present — run the pipeline)")
        return counts

    wanted_ids = {
        f"branch-{value}" for value in SMOKE_SELECTION.get("mrwash", [])
    } | set(SMOKE_SELECTION.get("cleancar", []))

    payload = json.loads(artefact.read_text(encoding="utf-8"))
    pois = [
        EnrichedPoi.from_json(item)
        for item in payload["pois"]
        if item["location"]["source_record_id"] in wanted_ids
    ]
    if len(pois) != len(wanted_ids):
        found = {poi.location.source_record_id for poi in pois}
        print(f"  WARNING: smoke sites missing from the artefact: {wanted_ids - found}")

    # --- Nominatim: every request variant the strategy ladder can issue ----
    probe = NominatimGeocoder(session=None, url=settings.nominatim_url)
    wanted_params: list[dict] = []
    for poi in pois:
        query = GeocodeQuery.from_location(poi.location)
        wanted_params.extend(
            [
                probe._structured_params(query),
                probe._structured_params(query, include_city=False),
                probe._freeform_params(query),
                probe._freeform_params(query, include_city=False),
            ]
        )
    comparable = [json.loads(json.dumps(p, sort_keys=True)) for p in wanted_params]
    nominatim_entries = []
    for request, response in _cache_rows(settings.geocode_cache, "nominatim/search"):
        parsed = json.loads(request)
        if parsed.get("params") in comparable:
            nominatim_entries.append(
                {"request": parsed, "response": json.loads(response)}
            )
    (FIXTURES / "nominatim_responses.json").write_text(
        json.dumps(nominatim_entries, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    manifest["files"]["nominatim_responses.json"] = {
        "source": "nominatim/search",
        "entries": len(nominatim_entries),
        "note": "verbatim API responses for the smoke-test sites",
    }
    counts["nominatim_responses.json"] = len(nominatim_entries)

    # --- Overpass: the exact per-site query the enricher builds ------------
    wanted_queries = {
        build_site_query(
            poi.geocode.lat,
            poi.geocode.lon,
            settings.building_search_radius_m,
            settings.parking_search_radius_m,
        )
        for poi in pois
        if poi.geocode and poi.geocode.lat is not None
    }
    overpass_entries = []
    for request, response in _cache_rows(settings.overpass_cache, "overpass/site"):
        parsed = json.loads(request)
        if parsed.get("query") in wanted_queries:
            overpass_entries.append(
                {"request": parsed, "response": json.loads(response)}
            )
    (FIXTURES / "overpass_responses.json").write_text(
        json.dumps(overpass_entries, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    manifest["files"]["overpass_responses.json"] = {
        "source": "overpass/site",
        "entries": len(overpass_entries),
        "note": "verbatim API responses for the smoke-test sites",
    }
    counts["overpass_responses.json"] = len(overpass_entries)
    return counts


def main() -> int:
    setup_logging("INFO", json_logs=False)
    settings = load_settings()
    FIXTURES.mkdir(parents=True, exist_ok=True)
    session = PoliteSession(
        user_agent=settings.user_agent,
        timeout=settings.http_timeout,
        max_retries=settings.http_max_retries,
        min_interval=max(1.0, settings.scrape_min_interval),
    )

    manifest: dict[str, object] = {
        "note": "Fixtures captured from the live official sources. HTML files have "
                "script/style/link/svg/noscript tags and comments removed (the "
                "parsers discard them anyway); original_sha256 is the hash of the "
                "unmodified response.",
        "files": {},
    }

    for name, url in HTML_TARGETS.items():
        try:
            response = session.get(url)
            status = response.status_code
            original = response.text
        except Exception as exc:  # 404 fixture is captured deliberately
            from iris_cre41.httpclient import HttpError

            if not isinstance(exc, HttpError):
                raise
            print(f"  (expected failure for {name}: {exc})")
            manifest["files"][name] = {"source_url": url, "error": str(exc)}
            continue
        text = shrink(original, is_xml=name.endswith(".xml"))
        (FIXTURES / name).write_text(text, encoding="utf-8")
        manifest["files"][name] = {
            "source_url": url,
            "http_status": status,
            "original_bytes": len(original.encode("utf-8")),
            "original_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
            "stored_bytes": len(text.encode("utf-8")),
        }
        print(f"  wrote {name} ({len(text) // 1024} KiB)")

    # Verbatim API responses straight out of the pipeline's caches, subset to
    # exactly the sites the offline smoke test replays. The subset is computed
    # by rebuilding the request each component would issue for those sites, so
    # the fixtures match byte-for-byte what the code asks for.
    api_counts = capture_api_fixtures(settings, manifest)
    for name, count in api_counts.items():
        print(f"  wrote {name} ({count} entries)")

    (FIXTURES / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"manifest: {FIXTURES / 'MANIFEST.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
