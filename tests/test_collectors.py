"""Collector parsing, exercised against fixtures captured from the live sites."""

from __future__ import annotations

import pytest

from conftest import read_fixture
from iris_cre41.collectors.base import FetchedPage, FixtureFetcher
from iris_cre41.collectors.cleancar import (
    CleanCarCollector,
    extract_location_urls,
    parse_location_page,
    slug_of,
)
from iris_cre41.collectors.mrwash import (
    MrWashCollector,
    branch_url,
    parse_branch_page,
    parse_directory,
)

# -- Mr. Wash --------------------------------------------------------------

def test_mrwash_directory_lists_every_branch():
    rows = parse_directory(read_fixture("mrwash_locations.html"))
    assert len(rows) == 36, "the captured directory lists 36 branches"
    assert all(row["branch_id"].isdigit() for row in rows)
    assert len({row["branch_id"] for row in rows}) == len(rows)
    # Every row carries a postal code and city from the directory table.
    assert all(row["postal_code"] and len(row["postal_code"]) == 5 for row in rows)
    assert all(row["city"] for row in rows)


def test_mrwash_directory_rows_are_sorted_deterministically():
    rows = parse_directory(read_fixture("mrwash_locations.html"))
    ids = [int(row["branch_id"]) for row in rows]
    assert ids == sorted(ids)


def test_mrwash_directory_extracts_services_and_maps_query():
    rows = {row["branch_id"]: row for row in
            parse_directory(read_fixture("mrwash_locations.html"))}
    aachen = rows["36"]
    assert "Autowäsche" in aachen["services"]
    assert "Breslauer" in (aachen["maps_query"] or "")


def test_mrwash_branch_page_extracts_full_address():
    rows = {row["branch_id"]: row for row in
            parse_directory(read_fixture("mrwash_locations.html"))}
    location = parse_branch_page(
        read_fixture("mrwash_branch_36_aachen.html"),
        url=branch_url(36),
        branch_id="36",
        directory_row=rows["36"],
    )
    assert location.operator == "Mr. Wash"
    assert location.source_record_id == "branch-36"
    assert location.postal_code_raw == "52068"
    assert location.city_raw == "Aachen"
    assert "Breslauer Straße 10" in location.address_block_raw
    assert location.email_raw == "awf36@mrwash.de"
    assert location.phone_raw.startswith("0241")
    assert location.attributes["max_vehicle_height_m"] == pytest.approx(1.95)
    assert location.attributes["max_vehicle_width_m"] == pytest.approx(2.20)
    assert location.attributes["poi_category"] == "car_wash"
    assert location.extraction_warnings == []


def test_mrwash_branch_page_second_site_differs():
    location = parse_branch_page(
        read_fixture("mrwash_branch_1_hamburg.html"), url=branch_url(1), branch_id="1"
    )
    assert location.postal_code_raw == "22047"
    assert location.city_raw == "Hamburg"
    assert location.source_record_id == "branch-1"


def test_mrwash_rejects_mismatched_directory_row():
    rows = {row["branch_id"]: row for row in
            parse_directory(read_fixture("mrwash_locations.html"))}
    location = parse_branch_page(
        read_fixture("mrwash_branch_36_aachen.html"),
        url=branch_url(36),
        branch_id="36",
        directory_row=rows["1"],  # wrong branch deliberately
    )
    assert "directory_row_branch_id_mismatch_ignored" in location.extraction_warnings
    assert location.city_raw == "Aachen"  # address still comes from the page


def test_mrwash_collector_records_fetch_failures_without_inventing_data():
    class FailingFetcher(FixtureFetcher):
        def __call__(self, url: str) -> FetchedPage:
            if "branch?id=" in url:
                raise OSError("simulated network failure")
            return super().__call__(url)

    fetcher = FailingFetcher(
        {"https://www.mrwash.de/de/frontend/default/locations":
             _fixture_path("mrwash_locations.html")}
    )
    result = MrWashCollector(fetcher=fetcher, only=["36"]).collect()
    assert result.locations == []
    assert len(result.failed_urls) == 1
    assert result.stats["failures"] == 1


def _fixture_path(name: str):
    from pathlib import Path

    return Path(__file__).parent / "fixtures" / name


# -- Clean Car -------------------------------------------------------------

def test_cleancar_discovery_unions_index_and_sitemap():
    index = read_fixture("cleancar_standorte_index.html")
    sitemap = read_fixture("cleancar_sitemap_pages.xml")
    from_index = set(extract_location_urls(index))
    from_sitemap = set(extract_location_urls(sitemap))
    union = set(extract_location_urls(index, sitemap))
    assert union == from_index | from_sitemap
    assert len(union) >= 47
    # The sitemap contains at least one page absent from the index page.
    assert from_sitemap - from_index


def test_cleancar_discovery_excludes_the_index_itself():
    urls = extract_location_urls(read_fixture("cleancar_standorte_index.html"))
    assert "https://www.clean-car.com/standorte/" not in urls
    assert all(url.count("/standorte/") == 1 for url in urls)


def test_cleancar_parses_br_separated_address():
    location = parse_location_page(
        read_fixture("cleancar_bremen.html"),
        "https://www.clean-car.com/standorte/bremen/",
    )
    assert location.operator == "Clean Car"
    assert location.street_raw == "Neuenlander Straße 93-95"
    assert location.postal_code_raw == "28199"
    assert location.city_raw == "Bremen"
    assert location.source_record_id == "bremen"
    assert location.attributes["max_vehicle_height_m"] == pytest.approx(2.40)
    assert location.attributes["site_type_observed"] == "wash_tunnel"
    assert location.extraction_warnings == []


def test_cleancar_parses_two_paragraph_address_variant():
    location = parse_location_page(
        read_fixture("cleancar_bad_oldesloe_2.html"),
        "https://www.clean-car.com/standorte/bad-oldesloe-2/",
    )
    assert location.street_raw == "Lily-Braun-Straße 36"
    assert location.postal_code_raw == "23843"
    assert location.city_raw == "Bad Oldesloe"
    assert location.extraction_warnings == []


def test_cleancar_parses_self_service_site_variant():
    location = parse_location_page(
        read_fixture("cleancar_hamburg_steilshoop.html"),
        "https://www.clean-car.com/standorte/hamburg-kollaustrasse-2/",
    )
    assert location.street_raw == "Steilshooper Straße 321"
    assert location.postal_code_raw == "22309"
    # The operator publishes the borough, not the postal city: kept verbatim.
    assert location.city_raw == "Wandsbek"
    assert location.attributes["site_type_observed"] == "self_service"
    assert location.attributes["self_service_max_height_m"] == pytest.approx(2.70)


def test_cleancar_never_picks_up_the_corporate_footer_address():
    for name, url in (
        ("cleancar_bremen.html", "https://www.clean-car.com/standorte/bremen/"),
        ("cleancar_bad_oldesloe_2.html",
         "https://www.clean-car.com/standorte/bad-oldesloe-2/"),
    ):
        location = parse_location_page(read_fixture(name), url)
        # Elmshorn/Ramskamp is the company HQ printed in the footer.
        assert location.postal_code_raw != "25337"
        assert "Ramskamp" not in (location.street_raw or "")


def test_cleancar_collector_end_to_end_on_fixtures():
    fetcher = FixtureFetcher(
        {
            "https://www.clean-car.com/standorte/":
                _fixture_path("cleancar_standorte_index.html"),
            "https://www.clean-car.com/wp-sitemap-posts-page-1.xml":
                _fixture_path("cleancar_sitemap_pages.xml"),
            "https://www.clean-car.com/standorte/bremen/":
                _fixture_path("cleancar_bremen.html"),
        }
    )
    result = CleanCarCollector(fetcher=fetcher, only=["bremen"]).collect()
    assert len(result.locations) == 1
    assert result.stats["locations_discovered"] >= 47
    assert result.stats["locations_selected"] == 1
    assert result.failed_urls == []
    assert result.locations[0].html_sha256


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.clean-car.com/standorte/bremen/", "bremen"),
        ("https://www.clean-car.com/standorte/bad-oldesloe-2/", "bad-oldesloe-2"),
    ],
)
def test_slug_of(url, expected):
    assert slug_of(url) == expected
