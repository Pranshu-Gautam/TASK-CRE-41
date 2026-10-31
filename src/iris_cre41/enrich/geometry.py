"""Turning raw Overpass elements into validated Shapely polygons.

Rules enforced here:

* only polygonal geometry is accepted (Polygon / MultiPolygon);
* invalid geometry is repaired with ``shapely.make_valid()`` and the repair is
  recorded, never hidden;
* empty geometry is rejected outright;
* multipolygon relations are assembled by stitching member ways into rings,
  with inner rings assigned to the outer ring that contains them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from shapely import make_valid
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.geometry.polygon import orient

from ..logging_setup import get_logger

log = get_logger(__name__)

Coord = tuple[float, float]

# Endpoint-matching tolerance when stitching ways into rings (degrees).
# ~1e-7 deg is roughly 1 cm, far below OSM node precision.
STITCH_TOLERANCE = 1e-7

VALID = "valid"
REPAIRED = "repaired"
REJECTED_EMPTY = "rejected_empty"
REJECTED_TYPE = "rejected_non_polygonal"
REJECTED_UNBUILDABLE = "rejected_unbuildable"


@dataclass
class GeometryValidation:
    status: str
    geometry: BaseGeometry | None
    notes: list[str]

    @property
    def ok(self) -> bool:
        return self.geometry is not None and self.status in {VALID, REPAIRED}


def _coords_of(element_geometry: Sequence[dict[str, Any]] | None) -> list[Coord]:
    """Overpass ``geometry`` list -> (lon, lat) tuples, dropping gaps."""
    if not element_geometry:
        return []
    coords: list[Coord] = []
    for node in element_geometry:
        lat = node.get("lat")
        lon = node.get("lon")
        if lat is None or lon is None:
            continue  # incomplete way (clipped at the query bbox)
        coords.append((float(lon), float(lat)))
    return coords


def _close(coords: list[Coord]) -> list[Coord]:
    if len(coords) >= 3 and coords[0] != coords[-1]:
        return coords + [coords[0]]
    return coords


def _near(a: Coord, b: Coord, tolerance: float = STITCH_TOLERANCE) -> bool:
    return abs(a[0] - b[0]) <= tolerance and abs(a[1] - b[1]) <= tolerance


def stitch_rings(segments: Iterable[list[Coord]]) -> list[list[Coord]]:
    """Chain open way segments into closed rings.

    Overpass returns a multipolygon relation as a bag of member ways; a ring
    may be split over several of them, in arbitrary order and direction.
    """
    pending = [list(segment) for segment in segments if len(segment) >= 2]
    rings: list[list[Coord]] = []

    while pending:
        current = pending.pop(0)
        if _near(current[0], current[-1]) and len(current) >= 4:
            rings.append(_close(current))
            continue
        progressed = True
        while progressed and not (_near(current[0], current[-1]) and len(current) >= 4):
            progressed = False
            for index, candidate in enumerate(pending):
                if _near(current[-1], candidate[0]):
                    current = current + candidate[1:]
                elif _near(current[-1], candidate[-1]):
                    current = current + list(reversed(candidate))[1:]
                elif _near(current[0], candidate[-1]):
                    current = candidate[:-1] + current
                elif _near(current[0], candidate[0]):
                    current = list(reversed(candidate))[:-1] + current
                else:
                    continue
                pending.pop(index)
                progressed = True
                break
        if len(current) >= 4 and _near(current[0], current[-1]):
            rings.append(_close(current))
        else:
            log.debug("ring_not_closed", extra={"vertices": len(current)})
    return rings


def _assign_holes(outers: list[Polygon], inners: list[Polygon]) -> list[Polygon]:
    """Attach every inner ring to the outer polygon that contains it."""
    if not inners:
        return outers
    assembled: list[Polygon] = []
    for outer in outers:
        holes = [
            inner.exterior.coords
            for inner in inners
            if outer.contains(inner.representative_point())
        ]
        try:
            assembled.append(Polygon(outer.exterior.coords, holes))
        except (ValueError, TypeError):
            assembled.append(outer)
    return assembled


def element_to_geometry(element: dict[str, Any]) -> BaseGeometry | None:
    """Build an unvalidated Shapely geometry (WGS84 lon/lat) from an element.

    Returns ``None`` for elements that carry no usable areal geometry (nodes,
    open ways, relations whose members never close).
    """
    element_type = element.get("type")

    if element_type == "node":
        return None  # a node has no footprint; handled as a tag-only hint

    if element_type == "way":
        coords = _close(_coords_of(element.get("geometry")))
        if len(coords) < 4:
            return None
        try:
            return Polygon(coords)
        except (ValueError, TypeError):
            return None

    if element_type == "relation":
        outer_segments: list[list[Coord]] = []
        inner_segments: list[list[Coord]] = []
        for member in element.get("members") or []:
            if member.get("type") != "way":
                continue
            coords = _coords_of(member.get("geometry"))
            if len(coords) < 2:
                continue
            role = (member.get("role") or "outer").lower()
            if role == "inner":
                inner_segments.append(coords)
            else:
                outer_segments.append(coords)
        outer_polys: list[Polygon] = []
        for ring in stitch_rings(outer_segments):
            try:
                outer_polys.append(Polygon(ring))
            except (ValueError, TypeError):
                continue
        inner_polys: list[Polygon] = []
        for ring in stitch_rings(inner_segments):
            try:
                inner_polys.append(Polygon(ring))
            except (ValueError, TypeError):
                continue
        if not outer_polys:
            return None
        polygons = _assign_holes(outer_polys, inner_polys)
        if len(polygons) == 1:
            return polygons[0]
        return MultiPolygon(polygons)

    return None


def _polygonal_parts(geometry: BaseGeometry) -> list[Polygon]:
    if geometry.geom_type == "Polygon":
        return [geometry]
    if geometry.geom_type == "MultiPolygon":
        return [part for part in geometry.geoms if part.geom_type == "Polygon"]
    if geometry.geom_type == "GeometryCollection":
        parts: list[Polygon] = []
        for part in geometry.geoms:
            parts.extend(_polygonal_parts(part))
        return parts
    return []


def validate_and_repair(geometry: BaseGeometry | None) -> GeometryValidation:
    """Validate, repair and type-check one geometry."""
    notes: list[str] = []
    if geometry is None:
        return GeometryValidation(REJECTED_UNBUILDABLE, None, ["geometry_not_buildable"])
    if geometry.is_empty:
        return GeometryValidation(REJECTED_EMPTY, None, ["geometry_empty"])
    if geometry.geom_type not in {"Polygon", "MultiPolygon", "GeometryCollection"}:
        return GeometryValidation(
            REJECTED_TYPE, None, [f"geometry_type_{geometry.geom_type.lower()}"]
        )

    status = VALID
    working = geometry
    if not working.is_valid:
        working = make_valid(working)
        status = REPAIRED
        notes.append("make_valid_applied")
        if working.is_empty:
            return GeometryValidation(REJECTED_EMPTY, None, notes + ["empty_after_repair"])

    parts = _polygonal_parts(working)
    if not parts:
        return GeometryValidation(
            REJECTED_TYPE, None, notes + [f"non_polygonal_after_repair_{working.geom_type}"]
        )
    if len(parts) != len(_polygonal_parts(geometry)) and status == REPAIRED:
        notes.append("non_polygonal_fragments_dropped")

    oriented = [orient(part, sign=1.0) for part in parts]
    result: BaseGeometry = oriented[0] if len(oriented) == 1 else MultiPolygon(oriented)
    if result.is_empty:
        return GeometryValidation(REJECTED_EMPTY, None, notes + ["empty_after_assembly"])
    if not result.is_valid:
        result = make_valid(result)
        status = REPAIRED
        notes.append("make_valid_applied_twice")
        if result.is_empty or not _polygonal_parts(result):
            return GeometryValidation(REJECTED_EMPTY, None, notes + ["unrepairable"])
    return GeometryValidation(status, result, notes)


def as_multipolygon(geometry: BaseGeometry) -> MultiPolygon:
    """Normalise to MultiPolygon so one DB column type fits every footprint."""
    if geometry.geom_type == "MultiPolygon":
        return geometry
    if geometry.geom_type == "Polygon":
        return MultiPolygon([geometry])
    parts = _polygonal_parts(geometry)
    if not parts:
        raise ValueError(f"cannot coerce {geometry.geom_type} to MultiPolygon")
    return MultiPolygon(parts)
