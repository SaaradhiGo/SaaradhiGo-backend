"""Service-area enforcement (a thin shim over the ServiceZone table).

The authoritative source is the `ServiceZone` table managed by `servers.pricing`
(through maker-checker since module 9). This module keeps the old import path
working for callers that still use it; every function delegates to
`servers.pricing.services`.

NO FALLBACK POLYGON
-------------------
This module used to carry a hard-coded Hyderabad polygon and fall back to it when the
database had no active zone. That fallback is gone (module 9). With no active zone
there is no service: a point is outside the service area, and the REST layer says so
with a clear code (`NO_ACTIVE_ZONE` when no zone is active anywhere,
`OUT_OF_SERVICE_AREA` when zones exist but none contains the point). A fallback that
quietly served Hyderabad after its zone was deactivated would undo the very change an
operator and a checker had just agreed to.

When you add Bangalore (or any other city), propose a ServiceZone. You do NOT need to
edit this file.
"""

from __future__ import annotations

from typing import Tuple

#: Error codes the REST layer returns for a refused pickup or drop.
OUT_OF_SERVICE_AREA = 'OUT_OF_SERVICE_AREA'
NO_ACTIVE_ZONE = 'NO_ACTIVE_ZONE'


def _parse(lat, lon):
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
        return None
    return lat_f, lon_f


def is_inside_service_area(lat, lon) -> bool:
    """True iff (lat, lon) sits in an active ServiceZone."""
    point = _parse(lat, lon)
    if point is None:
        return False
    from servers.pricing.services import find_zone_for_point

    return find_zone_for_point(*point) is not None


def any_active_zone() -> bool:
    from servers.pricing.models import ServiceZone

    return ServiceZone.objects.filter(is_active=True).exists()


def validate_service_area_detail(
    pickup_lat, pickup_lon, dest_lat, dest_lon,
) -> Tuple[bool, str, str]:
    """`(ok, error_code, message)`. `error_code` is '' when ok."""
    if not is_inside_service_area(pickup_lat, pickup_lon):
        side = 'Pickup'
    elif not is_inside_service_area(dest_lat, dest_lon):
        side = 'Drop'
    else:
        return True, '', ''
    if not any_active_zone():
        return False, NO_ACTIVE_ZONE, (
            'SaaradhiGo is not serving any area right now: no service zone is active.')
    return False, OUT_OF_SERVICE_AREA, (
        f'{side} location is outside the SaaradhiGo service area.')


def validate_service_area(
    pickup_lat, pickup_lon, dest_lat, dest_lon,
) -> Tuple[bool, str]:
    ok, _code, message = validate_service_area_detail(
        pickup_lat, pickup_lon, dest_lat, dest_lon)
    return ok, message
