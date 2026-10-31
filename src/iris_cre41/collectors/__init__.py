"""Per-operator location collectors."""

from .base import CollectionResult, Fetcher, FixtureFetcher, HttpFetcher
from .cleancar import CleanCarCollector
from .mrwash import MrWashCollector

COLLECTORS = {
    "mrwash": MrWashCollector,
    "cleancar": CleanCarCollector,
}

__all__ = [
    "CollectionResult",
    "Fetcher",
    "FixtureFetcher",
    "HttpFetcher",
    "MrWashCollector",
    "CleanCarCollector",
    "COLLECTORS",
]
