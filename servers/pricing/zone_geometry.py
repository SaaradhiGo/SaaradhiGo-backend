"""Zone geometry: parse, validate and measure a polygon. Pure Python plus Shapely.

No GEOS or PostGIS system dependency is added: Shapely is already a requirement
(it is what point-in-zone lookups use) and nothing here needs a database.

WHAT "VALID" MEANS (checked on the server, never only in the console)
---------------------------------------------------------------------
* a GeoJSON `Polygon` or `MultiPolygon` (a bare `[[lon, lat], ...]` ring is
  accepted and normalised to a `Polygon`; what is stored is always GeoJSON);
* every ring is closed and has at least 4 positions (3 distinct plus the repeat);
* longitude in [-180, 180] and latitude in [-90, 90], finite numbers;
* no self-intersection and no overlapping parts (Shapely's `is_valid`);
* a sane area: at least `MIN_AREA_KM2` and at most `MAX_AREA_KM2`. A zone smaller
  than a city block is a typo; one larger than a small country is a mistake that
  would swallow every neighbouring pickup.

AREA
----
Square kilometres, from an equirectangular projection centred on the shape
(x scaled by cos(latitude)). For the sizes allowed here (<= 20,000 km2) that is
within about 1% of the geodesic area, which is more than a sanity bound needs.
"""

import math

from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.validation import explain_validity

#: Smallest and largest area a zone may have, in square kilometres.
MIN_AREA_KM2 = 0.01
MAX_AREA_KM2 = 20_000

#: Containment tolerance, in degrees (about 1 cm): a child drawn on its parent's
#: border must not be refused for floating point noise.
CONTAIN_TOLERANCE_DEG = 1e-7
#: Two city zones "overlap" only if the shared area exceeds this (km2), so zones
#: that share a border do not.
OVERLAP_TOLERANCE_KM2 = 1e-4

KM_PER_DEG_LAT = 110.574
KM_PER_DEG_LON_EQUATOR = 111.320


class GeometryError(ValueError):
    """A polygon the server refuses. `code` is stable; `message` is for people."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _bad(code, message):
    raise GeometryError(code, message)


def _position(p, where):
    if not isinstance(p, (list, tuple)) or len(p) < 2:
        _bad('POLYGON_INVALID', f'{where}: each position must be [lon, lat].')
    try:
        lon, lat = float(p[0]), float(p[1])
    except (TypeError, ValueError):
        _bad('POLYGON_INVALID', f'{where}: position {p!r} is not numeric.')
    if not (math.isfinite(lon) and math.isfinite(lat)):
        _bad('POLYGON_INVALID', f'{where}: position {p!r} is not finite.')
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        _bad('COORDINATE_OUT_OF_RANGE',
             f'{where}: [{lon}, {lat}] is out of range (lon -180..180, lat -90..90).')
    return [lon, lat]


def _ring(ring, where):
    if not isinstance(ring, (list, tuple)):
        _bad('POLYGON_INVALID', f'{where}: a ring must be a list of positions.')
    if len(ring) < 4:
        _bad('RING_TOO_SHORT', f'{where}: a ring needs at least 4 positions '
                               f'(3 corners plus the closing position).')
    out = [_position(p, where) for p in ring]
    if out[0] != out[-1]:
        _bad('RING_NOT_CLOSED', f'{where}: the ring is not closed '
                                f'(the last position must equal the first).')
    if len({tuple(p) for p in out}) < 4:
        _bad('RING_TOO_SHORT', f'{where}: a ring needs at least 3 distinct corners.')
    return out


def _polygon_coords(coords, where):
    if not isinstance(coords, (list, tuple)) or not coords:
        _bad('POLYGON_INVALID', f'{where}: polygon coordinates are missing.')
    return [_ring(r, f'{where} ring {i + 1}') for i, r in enumerate(coords)]


def normalise(value):
    """The cleaned GeoJSON dict for `value`, or raise `GeometryError`."""
    if isinstance(value, (list, tuple)):
        # A bare ring of [lon, lat] pairs.
        return {'type': 'Polygon', 'coordinates': _polygon_coords([value], 'polygon')}
    if not isinstance(value, dict):
        _bad('POLYGON_INVALID', 'polygon_geojson must be a GeoJSON Polygon or MultiPolygon.')
    kind = value.get('type')
    if kind == 'Polygon':
        return {'type': 'Polygon',
                'coordinates': _polygon_coords(value.get('coordinates'), 'polygon')}
    if kind == 'MultiPolygon':
        parts = value.get('coordinates')
        if not isinstance(parts, (list, tuple)) or not parts:
            _bad('POLYGON_INVALID', 'MultiPolygon coordinates are missing.')
        return {'type': 'MultiPolygon',
                'coordinates': [_polygon_coords(p, f'part {i + 1}')
                                for i, p in enumerate(parts)]}
    _bad('POLYGON_INVALID', "Only GeoJSON 'Polygon' and 'MultiPolygon' are supported.")


def to_geometry(geojson):
    """Shapely geometry for already-normalised GeoJSON."""
    return shape(geojson)


def area_km2(geom):
    """Approximate area in km2 (equirectangular, centred on the shape)."""
    if geom.is_empty:
        return 0.0
    lat0 = (geom.bounds[1] + geom.bounds[3]) / 2
    kx = KM_PER_DEG_LON_EQUATOR * math.cos(math.radians(lat0))
    parts = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
    total = 0.0
    for part in parts:
        total += Polygon(
            [(x * kx, y * KM_PER_DEG_LAT) for x, y in part.exterior.coords],
            [[(x * kx, y * KM_PER_DEG_LAT) for x, y in hole.coords] for hole in part.interiors],
        ).area
    return total


def validate(value):
    """Validate a polygon fully. Returns `(geojson, geometry, area_km2)`.

    Raises `GeometryError` with one of: POLYGON_INVALID, COORDINATE_OUT_OF_RANGE,
    RING_TOO_SHORT, RING_NOT_CLOSED, SELF_INTERSECTION, AREA_TOO_SMALL, AREA_TOO_LARGE.
    """
    geojson = normalise(value)
    geom = to_geometry(geojson)
    if not geom.is_valid:
        reason = explain_validity(geom)
        code = 'SELF_INTERSECTION' if 'intersection' in reason.lower() \
            or 'self' in reason.lower() else 'POLYGON_INVALID'
        _bad(code, f'The polygon is not valid: {reason}.')
    area = area_km2(geom)
    if area < MIN_AREA_KM2:
        _bad('AREA_TOO_SMALL', f'The polygon covers {area:.4f} km2; the minimum is '
                               f'{MIN_AREA_KM2} km2.')
    if area > MAX_AREA_KM2:
        _bad('AREA_TOO_LARGE', f'The polygon covers {area:,.0f} km2; the maximum is '
                               f'{MAX_AREA_KM2:,} km2.')
    return geojson, geom, area


def contains(outer, inner):
    """Does `outer` cover `inner`, allowing for floating point on a shared border?"""
    return outer.buffer(CONTAIN_TOLERANCE_DEG).covers(inner)


def overlap_km2(a, b):
    """Shared area of two geometries in km2 (0 when they only touch)."""
    if not a.intersects(b):
        return 0.0
    shared = a.intersection(b)
    return area_km2(shared) if not shared.is_empty and shared.geom_type in (
        'Polygon', 'MultiPolygon') else _collection_area(shared)


def _collection_area(geom):
    total = 0.0
    for g in getattr(geom, 'geoms', []):
        if g.geom_type in ('Polygon', 'MultiPolygon'):
            total += area_km2(g)
    return total


def overlaps(a, b):
    return overlap_km2(a, b) > OVERLAP_TOLERANCE_KM2


def bounds_of(*geoms):
    """`(min_lon, min_lat, max_lon, max_lat)` covering all the geometries."""
    boxes = [g.bounds for g in geoms if g is not None and not g.is_empty]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))
