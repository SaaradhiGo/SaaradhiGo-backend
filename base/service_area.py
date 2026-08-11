"""Service-area enforcement (backwards-compatibility shim).

Historically this module owned a hardcoded Hyderabad polygon. As of
the multi-city pricing work, the authoritative source is the
ServiceZone table managed by `servers.pricing`. This module is kept
as a shim so existing callers (estimate_fare view, consumers) do not
need to change all at once -- the functions below now delegate to
`servers.pricing.services`.

Why a shim instead of deletion:
  * Several call sites still import from `base.service_area`. Keeping
    the shim avoids a wide diff in this PR and lets us move callers in
    follow-ups.
  * The legacy in-memory polygon is retained as a last-resort fallback
    when the DB has no active zones (fresh dev DB, test DB without the
    seed migration applied). That way `pytest` does not need to seed
    a zone before every test.

When you add Bangalore (or any other city), insert a ServiceZone row.
You do NOT need to edit this file.
"""

from __future__ import annotations

import logging
from typing import Tuple

from django.conf import settings
from shapely.geometry import Point, Polygon

logger = logging.getLogger(__name__)


# Fallback polygon. Same vertices the original implementation used; the
# 0002 data migration seeds an identical polygon into the ServiceZone
# table. If both the DB AND this fallback are wrong, callers fall
# back to a generous bounding box check below.
_HYDERABAD_POLYGON_LONLAT = [
    (78.205, 17.190),
    (78.155, 17.380),
    (78.230, 17.560),
    (78.420, 17.620),
    (78.580, 17.580),
    (78.700, 17.480),
    (78.730, 17.310),
    (78.680, 17.180),
    (78.470, 17.140),
    (78.330, 17.155),
]


def _fallback_polygon():
    global _CACHED_POLY
    try:
        return _CACHED_POLY  # type: ignore[name-defined]
    except NameError:
        pass
    coords = list(_HYDERABAD_POLYGON_LONLAT)
    raw = getattr(settings, 'SERVICE_AREA_OVERRIDE', '') or ''
    if raw:
        try:
            override = []
            for pair in raw.split(';'):
                lon, lat = pair.split(',')
                override.append((float(lon), float(lat)))
            if len(override) >= 3:
                coords = override
        except Exception:
            pass
    poly = Polygon(coords)
    globals()['_CACHED_POLY'] = poly
    return poly


def _try_db_lookup(lat, lon):
    """Try the multi-city ServiceZone lookup. Returns:
        True  -- point is inside an active zone
        False -- point is outside all active zones
        None  -- DB unavailable / no zones configured (caller should fall back)
    """
    try:
        from servers.pricing.services import find_zone_for_point
        from servers.pricing.models import ServiceZone
    except Exception:  # noqa: BLE001
        return None

    try:
        if not ServiceZone.objects.filter(is_active=True).exists():
            return None
    except Exception as exc:  # noqa: BLE001
        logger.debug('ServiceZone presence check failed: %s', exc)
        return None

    return find_zone_for_point(lat, lon) is not None


def is_inside_service_area(lat, lon) -> bool:
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return False
    if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
        return False

    db_result = _try_db_lookup(lat_f, lon_f)
    if db_result is not None:
        return db_result

    # No active zones in DB -- fall back to the legacy Hyderabad polygon.
    return _fallback_polygon().contains(Point(lon_f, lat_f))


def validate_service_area(
    pickup_lat, pickup_lon, dest_lat, dest_lon,
) -> Tuple[bool, str]:
    if not is_inside_service_area(pickup_lat, pickup_lon):
        return False, 'Pickup location is outside the SaaradhiGo service area.'
    if not is_inside_service_area(dest_lat, dest_lon):
        return False, 'Drop location is outside the SaaradhiGo service area.'
    return True, ''
