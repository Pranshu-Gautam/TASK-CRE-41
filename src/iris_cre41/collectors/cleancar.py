"""Clean Car collector.

Observed site structure (https://www.clean-car.com, inspected 2026-10-31):

* WordPress site. ``robots.txt`` disallows only ``/wp-admin/`` and advertises
  ``/wp-sitemap.xml``; the location pages are explicitly allowed.
* ``/standorte/`` — index page linking every location page.
* ``/wp-sitemap-posts-page-1.xml`` — canonical page list, occasionally
  containing a location page that is missing from the index (and vice versa);
  both sources are therefore unioned.
* ``/standorte/<slug>/`` — one page per site. The postal address appears either
  as ``<p>Street 12<br/>12345 City</p>`` or as two sibling ``<p>`` elements.
  Parsing therefore works on the visible text line stream, not on CSS classes.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence
from urllib.parse import urljoin

from ..logging_setup import get_logger
from ..models import RawLocation, utcnow
from .base import CollectionResult, Fetcher, soup_of, strip_chrome, text_lines

log = get_logger(__name__)

OPERATOR = "Clean Car"
OPERATOR_SLUG = "cleancar"
SOURCE_SYSTEM = "clean-car.com"
BASE_URL = "https://www.clean-car.com"
INDEX_PATH = "/standorte/"
SITEMAP_PATH = "/wp-sitemap-posts-page-1.xml"

_LOCATION_URL_RE = re.compile(
    r"https://www\.clean-car\.com/standorte/([a-z0-9][a-z0-9\-]*)/"
)
_PLZ_CITY_RE = re.compile(r"^(?P<plz>\d{5})\s+(?P<city>[^\d][^,]*)$")
_HOUSE_NUMBER_TAIL_RE = re.compile(r"\d")
_PHONE_RE = re.compile(r"Tel\.?:\s*(?P<phone>[\d\s/+()–-]{6,})")
_FAX_RE = re.compile(r"Fax:\s*(?P<fax>[\d\s/+()–-]{6,})")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", re.IGNORECASE)
_COMPANY_LINE_RE = re.compile(r"clean\s*car|gmbh|co\.?\s*kg", re.IGNORECASE)
_HOURS_HEAD_RE = re.compile(
    r"^(Sommer|Winter)?[öo]ffnungszeiten", re.IGNORECASE
)
_CLEARANCE_RE = re.compile(
    r"Durchfahrtsh[öo]he/?\s*-?\s*breite:?\s*(?P<h>\d+[.,]\d+)\s*m\s*/\s*"
    r"(?P<w>\d+[.,]\d+)\s*m",
    re.IGNORECASE,
)
_SB_HEIGHT_RE = re.compile(
    r"H[öo]he\s+SB\s+Waschen:?\s*(?P<h>\d+[.,]\d+)\s*m", re.IGNORECASE
)
_PRICE_RE = re.compile(
    r"^(?P<program>[A-ZÄÖÜ][\w\-\*/ ]{2,40}?)\s*\*?\s+(?P<price>\d+[.,]\d{2})\s*Euro$"
)


def index_url() -> str:
    return urljoin(BASE_URL, INDEX_PATH)


def sitemap_url() -> str:
    return urljoin(BASE_URL, SITEMAP_PATH)


def _float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value.replace(",", "."))
    except ValueError:
        return None


def extract_location_urls(*documents: str) -> list[str]:
    """Union of every ``/standorte/<slug>/`` URL found in the given documents."""
    slugs: set[str] = set()
    for document in documents:
        if not document:
            continue
        for match in _LOCATION_URL_RE.finditer(document):
            slugs.add(match.group(1))
        # Relative hrefs on the index page.
        for match in re.finditer(r'href="/standorte/([a-z0-9][a-z0-9\-]*)/"', document):
            slugs.add(match.group(1))
    return [f"{BASE_URL}/standorte/{slug}/" for slug in sorted(slugs)]


def slug_of(url: str) -> str:
    match = _LOCATION_URL_RE.search(url if url.endswith("/") else url + "/")
    return match.group(1) if match else url.rstrip("/").rsplit("/", 1)[-1]


def _find_address(lines: list[str]) -> tuple[str | None, str | None, str | None, list[str]]:
    """Return (street_line, postal_code, city, warnings) from a line stream.

    Rule: locate the first line of the form ``"<5 digits> <city>"`` whose
    immediately preceding line looks like a street line (contains a digit and
    is not a company/legal line). This handles both the ``<br/>`` and the
    two-paragraph markup variants without depending on CSS class names.
    """
    warnings: list[str] = []
    for index, line in enumerate(lines):
        match = _PLZ_CITY_RE.match(line)
        if not match:
            continue
        if index == 0:
            continue
        previous = lines[index - 1]
        if _COMPANY_LINE_RE.search(previous):
            continue  # corporate address block, not a site address
        if not _HOUSE_NUMBER_TAIL_RE.search(previous):
            continue  # no house number -> not a street line
        if len(previous) > 90:
            continue
        return previous, match.group("plz"), match.group("city").strip(), warnings
    warnings.append("address_block_not_found")
    return None, None, None, warnings


def parse_location_page(html: str, url: str) -> RawLocation:
    """Build a :class:`RawLocation` from one Clean Car location page."""
    full_soup = soup_of(html)
    soup = strip_chrome(soup_of(html))
    lines = text_lines(soup)
    page_text = "\n".join(lines)

    street_line, postal_code, city, warnings = _find_address(lines)

    name = None
    title = full_soup.find("title")
    if title is not None:
        name = " ".join(title.get_text().split())
        name = re.sub(r"\s*[–\-|]\s*Clean\s*Car.*$", "", name).strip() or None
    heading = soup.select_one("[class*=standort-heading] p, [class*=standort-heading]")
    if heading is not None:
        heading_text = " ".join(heading.get_text(" ").split())
        if heading_text and len(heading_text) <= 60:
            name = heading_text

    phone = _PHONE_RE.search(page_text)
    fax = _FAX_RE.search(page_text)
    email = _EMAIL_RE.search(page_text)

    hours: list[str] = []
    for index, line in enumerate(lines):
        if _HOURS_HEAD_RE.match(line):
            hours.extend(lines[index : index + 6])
    # De-duplicate while preserving order.
    seen: set[str] = set()
    hours = [h for h in hours if not (h in seen or seen.add(h))]

    attributes: dict[str, Any] = {
        "slug": slug_of(url),
        "poi_category": "car_wash",
    }
    clearance = _CLEARANCE_RE.search(page_text.replace("\n", " "))
    if clearance:
        attributes["max_vehicle_height_m"] = _float(clearance.group("h"))
        attributes["max_vehicle_width_m"] = _float(clearance.group("w"))
    sb_height = _SB_HEIGHT_RE.search(page_text.replace("\n", " "))
    if sb_height:
        attributes["self_service_max_height_m"] = _float(sb_height.group("h"))
        attributes["site_type_observed"] = "self_service"
    if clearance:
        attributes["site_type_observed"] = "wash_tunnel"

    programs: dict[str, float] = {}
    for line in lines:
        price = _PRICE_RE.match(line)
        if price:
            value = _float(price.group("price"))
            if value is not None:
                programs[" ".join(price.group("program").split())] = value
    if programs:
        attributes["published_programs_eur"] = programs

    if fax:
        attributes["fax_published"] = fax.group("fax").strip()

    address_block = None
    if street_line and postal_code:
        address_block = f"{street_line}\n{postal_code} {city}"

    return RawLocation(
        operator=OPERATOR,
        source_system=SOURCE_SYSTEM,
        source_record_id=slug_of(url),
        source_url=url,
        retrieved_at=utcnow(),
        location_name_raw=name,
        address_block_raw=address_block,
        street_raw=street_line,
        postal_code_raw=postal_code,
        city_raw=city,
        country_raw="DE",
        phone_raw=(phone.group("phone").strip() if phone else None),
        email_raw=(email.group(0) if email else None),
        opening_hours_raw=("\n".join(hours) if hours else None),
        attributes=attributes,
        extraction_warnings=warnings,
    )


class CleanCarCollector:
    """Collects every location page discoverable from index + sitemap."""

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
        # Restrict to specific location slugs (targeted re-scrape of single sites).
        self.only = {str(value) for value in only} if only else None

    def discover(self) -> tuple[list[str], dict[str, Any]]:
        documents: list[str] = []
        stats: dict[str, Any] = {}
        for label, url in (("index", index_url()), ("sitemap", sitemap_url())):
            try:
                page = self.fetcher(url)
            except Exception as exc:
                log.error(
                    "cleancar_discovery_source_failed",
                    extra={"source": label, "url": url, "error": str(exc)},
                )
                stats[f"{label}_error"] = str(exc)
                continue
            documents.append(page.text)
            stats[f"{label}_url"] = url
            stats[f"{label}_sha256"] = page.sha256
            stats[f"{label}_locations"] = len(extract_location_urls(page.text))
        urls = extract_location_urls(*documents)
        log.info("cleancar_discovery", extra={"locations": len(urls), **stats})
        return urls, stats

    def collect(self) -> CollectionResult:
        result = CollectionResult(
            operator=OPERATOR,
            operator_slug=OPERATOR_SLUG,
            source_system=SOURCE_SYSTEM,
        )
        urls, stats = self.discover()
        result.stats.update(stats)
        result.stats["locations_discovered"] = len(urls)
        result.discovered_urls = list(urls)

        if self.only is not None:
            urls = [url for url in urls if slug_of(url) in self.only]
            result.stats["locations_selected"] = len(urls)
        selected: Iterable[str] = urls[: self.limit] if self.limit else urls
        for url in selected:
            try:
                page = self.fetcher(url)
            except Exception as exc:
                log.error(
                    "cleancar_page_fetch_failed",
                    extra={"url": url, "error": f"{type(exc).__name__}: {exc}"},
                )
                result.failed_urls.append({"url": url, "error": str(exc)})
                continue
            location = parse_location_page(page.text, url=url)
            location.html_sha256 = page.sha256
            result.locations.append(location)

        result.stats["pages_fetched"] = len(result.locations) + len(result.failed_urls)
        result.stats["locations_extracted"] = len(result.locations)
        result.stats["failures"] = len(result.failed_urls)
        return result
