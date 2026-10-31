"""Coordinate reference system handling.

Mandated storage CRS is EPSG:25832 (ETRS89 / UTM zone 32N). Every coordinate
entering the pipeline is validated as WGS84 (EPSG:4326) first, then projected.
``always_xy=True`` is used on all transformers so axis order is unambiguous
(x=easting/longitude, y=northing/latitude) regardless of the CRS definition.
"""

from __future__ import annotations

from functools import lru_cache

from pyproj import CRS, Transformer
from pyproj.exceptions import CRSError
from shapely import transform as shapely_transform
from shapely.geometry.base import BaseGeometry

from .config import DE_BBOX, TARGET_EPSG, WGS84_EPSG

# EPSG:25832 covers 6°E-12°E nominally; Germany is routinely projected in a
# single zone, so a generous but finite envelope is used as a blunder check.
UTM32N_VALID_X = (-1_000_000.0, 2_500_000.0)
UTM32N_VALID_Y = (0.0, 10_000_000.0)

SUPPORTED_SOURCE_EPSG = (WGS84_EPSG, TARGET_EPSG)


class UnsupportedCRSError(ValueError):
    """Raised for a source CRS the pipeline is not allowed to assume."""


class InvalidCoordinateError(ValueError):
    """Raised for coordinates that cannot be a real WGS84/UTM32N position."""


@lru_cache(maxsize=32)
def get_crs(epsg: int) -> CRS:
    try:
        return CRS.from_epsg(epsg)
    except CRSError as exc:  # pragma: no cover - pyproj message passthrough
        raise UnsupportedCRSError(f"unknown EPSG code {epsg}: {exc}") from exc


def validate_source_epsg(epsg: int | None) -> int:
    """Accept only CRS codes the pipeline knows how to handle."""
    if epsg is None:
        raise UnsupportedCRSError(
            "source CRS is unknown; the pipeline refuses to assume one"
        )
    if int(epsg) not in SUPPORTED_SOURCE_EPSG:
        raise UnsupportedCRSError(
            f"unsupported source CRS EPSG:{epsg}; supported: "
            + ", ".join(f"EPSG:{code}" for code in SUPPORTED_SOURCE_EPSG)
        )
    return int(epsg)


@lru_cache(maxsize=32)
def get_transformer(source_epsg: int, target_epsg: int) -> Transformer:
    return Transformer.from_crs(
        get_crs(source_epsg), get_crs(target_epsg), always_xy=True
    )


def validate_wgs84(lat: float | None, lon: float | None) -> tuple[float, float]:
    """Validate a latitude/longitude pair. Raises, never corrects."""
    if lat is None or lon is None:
        raise InvalidCoordinateError("latitude/longitude missing")
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError) as exc:
        raise InvalidCoordinateError(f"non-numeric coordinate: {lat!r}, {lon!r}") from exc
    if lat_f != lat_f or lon_f != lon_f:  # NaN
        raise InvalidCoordinateError("NaN coordinate")
    if not -90.0 <= lat_f <= 90.0:
        raise InvalidCoordinateError(f"latitude out of range: {lat_f}")
    if not -180.0 <= lon_f <= 180.0:
        raise InvalidCoordinateError(f"longitude out of range: {lon_f}")
    if lat_f == 0.0 and lon_f == 0.0:
        raise InvalidCoordinateError("null island (0, 0) rejected")
    return lat_f, lon_f


def in_germany(lat: float, lon: float) -> bool:
    """Plausibility envelope, used to *flag* out-of-area geocodes."""
    min_lon, min_lat, max_lon, max_lat = DE_BBOX
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


def to_target(
    lat: float, lon: float, source_epsg: int = WGS84_EPSG, target_epsg: int = TARGET_EPSG
) -> tuple[float, float]:
    """Project a validated WGS84 pair into ``target_epsg`` (default 25832)."""
    source_epsg = validate_source_epsg(source_epsg)
    lat, lon = validate_wgs84(lat, lon)
    x, y = get_transformer(source_epsg, target_epsg).transform(lon, lat)
    if not (x == x and y == y):
        raise InvalidCoordinateError(
            f"projection of ({lat}, {lon}) to EPSG:{target_epsg} produced NaN"
        )
    if target_epsg == TARGET_EPSG and not (
        UTM32N_VALID_X[0] <= x <= UTM32N_VALID_X[1]
        and UTM32N_VALID_Y[0] <= y <= UTM32N_VALID_Y[1]
    ):
        raise InvalidCoordinateError(
            f"projected coordinate ({x:.1f}, {y:.1f}) outside EPSG:"
            f"{target_epsg} working envelope"
        )
    return x, y


def transform_geometry(
    geometry: BaseGeometry, source_epsg: int, target_epsg: int = TARGET_EPSG
) -> BaseGeometry:
    """Reproject a Shapely geometry with an explicitly validated source CRS."""
    source_epsg = validate_source_epsg(source_epsg)
    if source_epsg == target_epsg:
        return geometry
    transformer = get_transformer(source_epsg, target_epsg)

    def _reproject(coordinates):
        """Vectorised coordinate transform over an (N, 2) coordinate array."""
        xs, ys = transformer.transform(coordinates[:, 0], coordinates[:, 1])
        out = coordinates.copy()
        out[:, 0] = xs
        out[:, 1] = ys
        return out

    return shapely_transform(geometry, _reproject)


def is_metric_crs(epsg: int) -> bool:
    """True when areas/lengths in this CRS are metres (required for areas)."""
    crs = get_crs(epsg)
    units = {
        axis.unit_name for axis in crs.axis_info if axis.unit_name is not None
    }
    return bool(units) and units <= {"metre", "meter", "m"}
