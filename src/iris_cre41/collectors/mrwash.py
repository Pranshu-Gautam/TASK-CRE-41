"""Mr. Wash collector.

Observed site structure (https://www.mrwash.de, inspected 2026-10-31):

* ``/de/frontend/default/locations`` — server-rendered branch directory. Each
  row carries the operator's own branch id, the branch name, "PLZ City" and a
  Google-Maps ``daddr`` string containing city + street + postal code.
* ``/de/frontend/branch?id=<n>`` — branch detail page with the authoritative
  postal address under the heading "Anschrift", plus phone, e-mail, opening
  hours and vehicle entry limits.

``robots.txt`` was fetched and is empty (no Disallow rules); only plain GETs of
publicly served HTML are issued, at >= 1 request/second.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence
from urllib.parse import urljoin

from ..logging_setup import get_logger
from ..models import RawLocation, utcnow
from .base import CollectionResult, Fetcher, soup_of, strip_chrome, text_lines

log = get_logger(__name__)

OPERATOR = "Mr. Wash"
OPERATOR_SLUG = "mrwash"
SOURCE_SYSTEM = "mrwash.de"
BASE_URL = "https://www.mrwash.de"
LIST_PATH = "/de/frontend/default/locations"
BRANCH_PATH = "/de/frontend/branch?id={branch_id}"

_BRANCH_ID_RE = re.compile(r"branch\?id=(\d+)")
_DADDR_RE = re.compile(r"daddr=([^'\")]+)")
_PLZ_CITY_RE = re.compile(r"^(?P<plz>\d{5})\s+(?P<city>.+)$")
_PHONE_RE = re.compile(r"Tel\.?:\s*(?P<phone>[\d\s/+()-]{6,})")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", re.IGNORECASE)
_LIMIT_RES = {
    "max_vehicle_width_m": re.compile(
        r"Breite[^0-9]{0,40}?(\d+[.,]\d+)\s*m", re.IGNORECASE
    ),
    "max_vehicle_height_m": re.compile(
        r"H(?:ö|oe)he[^0-9]{0,40}?(\d+[.,]\d+)\s*m", re.IGNORECASE
    ),
}
_ADDRESS_HEADING = "anschrift"
_STOP_LINE_RE = re.compile(r"^(Tel\.?:|E-Mail:|Route|Öffnungszeiten)", re.IGNORECASE)


def branch_url(branch_id: str | int) -> str:
    return urljoin(BASE_URL, BRANCH_PATH.format(branch_id=branch_id))


def list_url() -> str:
    return urljoin(BASE_URL, LIST_PATH)


def _float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None


def parse_directory(html: str) -> list[dict[str, Any]]:
    """Parse the branch directory page into one dict per listed branch."""
    soup = soup_of(html)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for anchor in soup.select("a.td-location-link, a[href*='branch?id=']"):
        href = anchor.get("href") or ""
        match = _BRANCH_ID_RE.search(href)
        if not match:
            continue
        branch_id = match.group(1)
        row = anchor.find_parent(class_="tr")
        if row is None:
            continue  # navigation link, not a directory row
        if branch_id in seen:
            continue
        seen.add(branch_id)

        name = " ".join((anchor.get_text() or "").split()) or None

        plz = city = None
        street_cell = row.select_one(".td-street")
        if street_cell is not None:
            cell_text = " ".join(street_cell.get_text(" ").split())
            plz_city = _PLZ_CITY_RE.match(cell_text)
            if plz_city:
                plz = plz_city.group("plz")
                city = plz_city.group("city").strip()

        maps_query = None
        map_link = row.select_one("a.map-link")
        if map_link is not None:
            onclick = map_link.get("onclick") or ""
            daddr = _DADDR_RE.search(onclick)
            if daddr:
                maps_query = " ".join(daddr.group(1).replace("+", " ").split())

        services = [
            " ".join(item.get_text(" ").split())
            for item in row.select(".td-services li")
            if " ".join(item.get_text(" ").split())
        ]

        rows.append(
            {
                "branch_id": branch_id,
                "name": name,
                "postal_code": plz,
                "city": city,
                "maps_query": maps_query,
                "services": services,
            }
        )

    rows.sort(key=lambda r: int(r["branch_id"]))
    return rows


def _address_lines(lines: list[str]) -> list[str]:
    """Lines of the postal address block that follows the "Anschrift" heading."""
    try:
        start = next(
            index
            for index, line in enumerate(lines)
            if line.strip().lower().startswith(_ADDRESS_HEADING)
        )
    except StopIteration:
        return []
    block: list[str] = []
    for line in lines[start + 1 : start + 8]:
        if _STOP_LINE_RE.match(line):
            break
        block.append(line)
        if _PLZ_CITY_RE.match(line):
            break
    return block


def parse_branch_page(
    html: str,
    url: str,
    branch_id: str,
    directory_row: dict[str, Any] | None = None,
) -> RawLocation:
    """Build a :class:`RawLocation` from one branch detail page."""
    soup = strip_chrome(soup_of(html))
    lines = text_lines(soup)
    page_text = "\n".join(lines)
    warnings: list[str] = []

    block = _address_lines(lines)
    if not block:
        warnings.append("address_block_not_found")

    name = None
    title = soup_of(html).find("title")
    if title is not None:
        name = " ".join(title.get_text().split())
        name = re.sub(r"\s*[-–|]\s*Mr\.?\s*Wash.*$", "", name).strip() or None
    # The directory row is only authoritative for *its own* branch.
    if (
        directory_row
        and str(directory_row.get("branch_id")) == str(branch_id)
        and directory_row.get("name")
    ):
        name = directory_row["name"]
    elif directory_row and str(directory_row.get("branch_id")) != str(branch_id):
        warnings.append("directory_row_branch_id_mismatch_ignored")
        directory_row = None

    phone_match = _PHONE_RE.search(page_text)
    email_match = _EMAIL_RE.search(page_text)

    attributes: dict[str, Any] = {
        "branch_id": branch_id,
        "detail_page": url,
        "poi_category": "car_wash",
        "operator_legal_entity": next(
            (line for line in block if "Autoservice AG" in line), None
        ),
    }
    for key, pattern in _LIMIT_RES.items():
        match = pattern.search(page_text)
        value = _float(match.group(1)) if match else None
        if value is not None:
            attributes[key] = value

    manager = None
    for index, line in enumerate(lines):
        if line.strip().lower().startswith("niederlassungsleitung"):
            if index + 1 < len(lines):
                manager = lines[index + 1]
            break
    if manager:
        attributes["branch_manager_published"] = manager

    hours: list[str] = []
    for index, line in enumerate(lines):
        if line.strip().lower().startswith("öffnungszeiten"):
            hours = lines[index : index + 10]
            break
    if directory_row:
        if directory_row.get("services"):
            attributes["services_listed"] = directory_row["services"]
        if directory_row.get("maps_query"):
            attributes["directory_maps_query"] = directory_row["maps_query"]
        if directory_row.get("postal_code"):
            attributes["directory_postal_code"] = directory_row["postal_code"]
        if directory_row.get("city"):
            attributes["directory_city"] = directory_row["city"]

    address_block = "\n".join(block) if block else None
    postal_code = None
    city = None
    for line in block:
        plz_city = _PLZ_CITY_RE.match(line)
        if plz_city:
            postal_code = plz_city.group("plz")
            city = plz_city.group("city").strip()
            break
    if (
        postal_code
        and directory_row
        and directory_row.get("postal_code")
        and postal_code != directory_row["postal_code"]
    ):
        warnings.append("postal_code_mismatch_directory_vs_detail")

    return RawLocation(
        operator=OPERATOR,
        source_system=SOURCE_SYSTEM,
        source_record_id=f"branch-{branch_id}",
        source_url=url,
        retrieved_at=utcnow(),
        location_name_raw=name,
        address_block_raw=address_block,
        postal_code_raw=postal_code,
        city_raw=city,
        country_raw="DE",
        phone_raw=(phone_match.group("phone").strip() if phone_match else None),
        email_raw=(email_match.group(0) if email_match else None),
        opening_hours_raw=("\n".join(hours) if hours else None),
        attributes=attributes,
        extraction_warnings=warnings,
    )


class MrWashCollector:
    """Collects every branch listed in the official Mr. Wash directory."""

    operator = OPERATOR
    operator_slug = OPERATOR_SLUG
    source_system = SOURCE_SYSTEM

    def __init__(
        self,
        fetcher: Fetcher,
        limit: int | None = None,
        only: Sequence[str] | None = None,
    ) -> None:
        self.fetcher = fetcher
        self.limit = limit
        # Restrict to specific branch ids (targeted re-scrape of single sites).
        self.only = {str(value) for value in only} if only else None

    def discover(self) -> tuple[list[dict[str, Any]], str]:
        page = self.fetcher(list_url())
        rows = parse_directory(page.text)
        log.info(
            "mrwash_directory_parsed",
            extra={"branches": len(rows), "url": page.url},
        )
        return rows, page.sha256

    def collect(self) -> CollectionResult:
        result = CollectionResult(
            operator=OPERATOR,
            operator_slug=OPERATOR_SLUG,
            source_system=SOURCE_SYSTEM,
        )
        rows, directory_hash = self.discover()
        result.stats["directory_url"] = list_url()
        result.stats["directory_sha256"] = directory_hash
        result.stats["branches_listed"] = len(rows)

        if self.only is not None:
            rows = [row for row in rows if str(row["branch_id"]) in self.only]
            result.stats["branches_selected"] = len(rows)
        selected: Iterable[dict[str, Any]] = rows[: self.limit] if self.limit else rows
        for row in selected:
            url = branch_url(row["branch_id"])
            result.discovered_urls.append(url)
            try:
                page = self.fetcher(url)
            except Exception as exc:  # network/HTTP failure for one branch only
                log.error(
                    "mrwash_branch_fetch_failed",
                    extra={"url": url, "error": f"{type(exc).__name__}: {exc}"},
                )
                result.failed_urls.append({"url": url, "error": str(exc)})
                continue
            location = parse_branch_page(
                page.text, url=url, branch_id=row["branch_id"], directory_row=row
            )
            location.html_sha256 = page.sha256
            result.locations.append(location)

        result.stats["pages_fetched"] = len(result.locations) + len(result.failed_urls)
        result.stats["locations_extracted"] = len(result.locations)
        result.stats["failures"] = len(result.failed_urls)
        return result
