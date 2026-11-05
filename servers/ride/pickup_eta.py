"""How far away the nearest free driver of a class is, for the fare quote.

Straight-line distance at a city speed, not a routed time: a quote is asked for
on every keystroke of a destination and a Google call each would undo the Maps
budget (M1-M5). Only minutes leave the server, never a driver's position.
"""

import math

from django.conf import settings

from servers.redis_client import nearby_drivers

#: How far to look; beyond this a rider is told no driver is nearby.
SEARCH_RADIUS_M = 5000


def pickup_eta(lat, lng, vehicle_type):
    """`(minutes or None, drivers_nearby)` for the nearest live driver of the class."""
    try:
        found = nearby_drivers(lng, lat, radius=SEARCH_RADIUS_M, count=1,
                               vehicle_type=vehicle_type)
    except Exception:  # noqa: BLE001 - a quote must never fail for want of an ETA
        found = None
    if not found:
        return None, False
    nearest = found[0]
    metres = float(nearest[1]) if isinstance(nearest, (list, tuple)) else 0.0
    kmh = float(getattr(settings, 'PICKUP_ETA_CITY_KMH', 18))
    return max(1, math.ceil(metres / (kmh * 1000 / 60))), True
