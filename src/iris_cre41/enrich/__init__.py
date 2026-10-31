"""Geospatial enrichment from OpenStreetMap (ODbL 1.0)."""

from .geometry import (
    GeometryValidation,
    element_to_geometry,
    stitch_rings,
    validate_and_repair,
)
from .matching import MatchConfig, match_site
from .overpass import OverpassClient, build_site_query

__all__ = [
    "OverpassClient",
    "build_site_query",
    "element_to_geometry",
    "stitch_rings",
    "validate_and_repair",
    "GeometryValidation",
    "match_site",
    "MatchConfig",
]
