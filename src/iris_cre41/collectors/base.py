"""Collector infrastructure: fetch abstraction, result container, HTML helpers.

The fetch boundary is a plain callable so the parsers can be exercised against
captured fixtures in unit tests without any network access.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ..httpclient import PoliteSession
from ..logging_setup import get_logger
from ..models import RawLocation

log = get_logger(__name__)


@dataclass
class FetchedPage:
    url: str
    text: str
    status_code: int = 200
    from_fixture: bool = False

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8", "replace")).hexdigest()


class Fetcher(Protocol):
    def __call__(self, url: str) -> FetchedPage: ...


class HttpFetcher:
    """Live fetcher backed by :class:`PoliteSession`."""

    def __init__(self, session: PoliteSession) -> None:
        self.session = session

    def __call__(self, url: str) -> FetchedPage:
        response = self.session.get(url)
        return FetchedPage(
            url=response.url, text=response.text, status_code=response.status_code
        )


class FixtureFetcher:
    """Offline fetcher mapping URLs onto files captured from the live sites."""

    def __init__(self, mapping: dict[str, Path | str]) -> None:
        self.mapping = {k: Path(v) for k, v in mapping.items()}
        self.calls: list[str] = []

    def __call__(self, url: str) -> FetchedPage:
        self.calls.append(url)
        path = self.mapping.get(url)
        if path is None:  # tolerate trailing-slash variance
            path = self.mapping.get(url.rstrip("/")) or self.mapping.get(url + "/")
        if path is None:
            raise FileNotFoundError(f"no fixture registered for {url}")
        return FetchedPage(
            url=url,
            text=path.read_text(encoding="utf-8", errors="replace"),
            from_fixture=True,
        )


@dataclass
class CollectionResult:
    operator: str
    operator_slug: str
    source_system: str
    locations: list[RawLocation] = field(default_factory=list)
    discovered_urls: list[str] = field(default_factory=list)
    failed_urls: list[dict[str, str]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "operator": self.operator,
            "operator_slug": self.operator_slug,
            "source_system": self.source_system,
            "locations": [loc.to_json() for loc in self.locations],
            "discovered_urls": self.discovered_urls,
            "failed_urls": self.failed_urls,
            "stats": self.stats,
        }


# --------------------------------------------------------------------------
# HTML helpers
# --------------------------------------------------------------------------

_CHROME_TAGS = ("script", "style", "noscript", "svg", "nav", "header", "footer")


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def strip_chrome(soup: BeautifulSoup) -> BeautifulSoup:
    """Drop site chrome so the footer's own company address cannot leak in."""
    for tag_name in _CHROME_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()
    for tag in soup.select("[class*=elementor-location-footer], [class*=site-footer]"):
        tag.decompose()
    return soup


def text_lines(node: Any) -> list[str]:
    """Visible text of ``node`` as a list of non-empty, whitespace-squeezed lines.

    ``<br>`` and block boundaries both become line breaks, which is what makes
    the address parser work across the markup variants observed on both sites.
    """
    if node is None:
        return []
    raw = node.get_text("\n")
    lines = []
    for line in raw.replace("\xa0", " ").split("\n"):
        cleaned = " ".join(line.split())
        if cleaned:
            lines.append(cleaned)
    return lines


def same_host(url: str, host: str) -> bool:
    return urlsplit(url).netloc.lower().endswith(host.lower())
