"""Arrival times for an active trip: driver to pickup, and pickup (or here) to drop.

From the stored route when the trip has one (9 Nov, `route_eta`): the remaining distance along
the polyline at the route's own speed, worked out on our server every 15 seconds with no
routing call (`source: 'route'`). Otherwise, or off the route, or while the Maps budget is at
degrade level 2: straight-line distance x a road factor / the market's average speed
(`source: 'straight_line'`). Whole minutes, always marked `estimated`.

Never a guess: with no live driver position, or a trip that is not active, there is no ETA
(None), and the screens hide the line rather than show 0.
"""

import json
import math
import time
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

#: The trip states an ETA is computed for.
TO_PICKUP = ('accepted',)
AT_PICKUP = ('reached',)
TO_DROP = ('in_progress',)
ACTIVE = TO_PICKUP + AT_PICKUP + TO_DROP
#: How often the live channel carries a fresh estimate.
PUSH_INTERVAL_SECONDS = 15
#: The throttle's clock (a seam for tests).
clock = time.monotonic


def haversine_m(lat1, lng1, lat2, lng2):
    lat1, lng1, lat2, lng2 = (math.radians(float(v)) for v in (lat1, lng1, lat2, lng2))
    a = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2)
    return 2 * 6371000 * math.asin(math.sqrt(a))


def market_kmh(country):
    by_market = getattr(settings, 'ETA_MARKET_KMH', {}) or {}
    return float(by_market.get((country or '').upper())
                 or getattr(settings, 'PICKUP_ETA_CITY_KMH', 18))


def road_metres(straight_m):
    return straight_m * float(getattr(settings, 'ETA_ROAD_FACTOR', 1.3))


def _minutes(metres, country):
    """Whole minutes for `metres` of road, at least 1."""
    return max(1, math.ceil(metres / (market_kmh(country) * 1000 / 60)))


def _secs_to_min(seconds):
    return max(1, math.ceil(float(seconds) / 60))


def _routed(trip, phase, driver_at, country):
    """Seconds left on the stored route, or None (straight line then). Never raises."""
    try:
        from servers.ride.maps_budget import degrade_level
        from servers.ride.route_eta import routed_seconds

        if degrade_level(country) >= 2:
            return None
        return routed_seconds(trip, phase, driver_at, country)
    except Exception:  # noqa: BLE001 - the straight line always answers
        return None


def driver_position(driver_id):
    """(lat, lng) of the driver's last ping in the last two minutes, or None."""
    if not driver_id:
        return None
    try:
        from servers.live_ops.config import LIVEPOS_PREFIX
        from servers.redis_client import redis_client

        raw = redis_client.get(f'{LIVEPOS_PREFIX}{driver_id}')
        if not raw:
            return None
        lat, lng = json.loads(raw)[:2]
        return float(lat), float(lng)
    except Exception:  # noqa: BLE001 - no position means no ETA, never an error
        return None


def _country(trip):
    zone = getattr(trip, 'zone', None)
    return getattr(zone, 'country', None) or 'IN'


def compute(trip, driver_at=None, now=None):
    """The ETA payload for `trip`, or None when there is nothing honest to say.

    {phase: to_pickup|at_pickup|to_drop, pickup_eta_min, pickup_distance_km,
     drop_eta_min, arrive_at (ISO, the trip's local time, whole minute), estimated: True,
     source: 'straight_line'}
    """
    status = getattr(getattr(trip, 'status_id', None), 'status_code', None)
    if status not in ACTIVE:
        return None
    driver_at = driver_at or driver_position(trip.driver_id_id)
    if driver_at is None:
        return None
    now = now or timezone.now()
    country = _country(trip)
    pickup = (trip.pickup_lat, trip.pickup_long)
    drop = (trip.destination_lat, trip.destination_long)
    out = {'phase': None, 'pickup_eta_min': None, 'pickup_distance_km': None,
           'drop_eta_min': None, 'arrive_at': None, 'estimated': True,
           'source': 'straight_line'}
    if status in TO_DROP:
        out['phase'] = 'to_drop'
        routed = _routed(trip, 'to_drop', driver_at, country)
        out['drop_eta_min'] = (_secs_to_min(routed) if routed is not None
                               else _minutes(road_metres(haversine_m(*driver_at, *drop)), country))
        out['source'] = 'route' if routed is not None else 'straight_line'
    else:
        planned = getattr(trip, 'planned_route_duration_s', None)
        ride_min = (_secs_to_min(planned) if planned
                    else _minutes(road_metres(haversine_m(*pickup, *drop)), country))
        if status in TO_PICKUP:
            to_pickup = road_metres(haversine_m(*driver_at, *pickup))
            routed = _routed(trip, 'to_pickup', driver_at, country)
            out['phase'] = 'to_pickup'
            out['pickup_eta_min'] = (_secs_to_min(routed) if routed is not None
                                     else _minutes(to_pickup, country))
            out['source'] = 'route' if routed is not None else 'straight_line'
            out['pickup_distance_km'] = f'{to_pickup / 1000:.1f}'
            out['drop_eta_min'] = out['pickup_eta_min'] + ride_min
        else:
            out['phase'] = 'at_pickup'
            out['drop_eta_min'] = ride_min
    arrive = now + timedelta(minutes=out['drop_eta_min'])
    try:
        from base.market_time import trip_timezone

        arrive = arrive.astimezone(trip_timezone(trip))
    except Exception:  # noqa: BLE001 - UTC is still a correct instant
        pass
    out['arrive_at'] = arrive.replace(second=0, microsecond=0).isoformat()
    return out


def for_trip_id(trip_id, driver_at=None):
    from .models import Trip

    trip = (Trip.objects.select_related('status_id', 'zone')
            .filter(pk=trip_id).first())
    return compute(trip, driver_at=driver_at) if trip else None
