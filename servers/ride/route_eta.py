"""The live ETA from a stored route, worked out on our server (9 Nov, maps cost).

A routing API is called ONCE per leg: the pickup leg (driver to pickup) when the trip is accepted,
and the trip leg (pickup to drop) reused from the route cached at quote time. Every 15 seconds the
ETA is the remaining distance along that polyline, from the driver's position projected onto it,
at the route's own average speed (its distance / its duration). No call.

A new route is fetched only when the driver is more than `ETA_OFFROUTE_M` (150 m) off the route
for `ETA_OFFROUTE_S` (30 s), or the ETA has drifted more than `ETA_DRIFT` (30%) from the plan, and
at most once every `ETA_REROUTE_MIN_S` (3 min) per trip. A reroute lives in Redis, never in the
trip's `planned_route_polyline` (the route-deviation check keeps the original plan). While the
country's Maps budget is at degrade level 2 or more, no reroute is made.
"""

import json
import logging
import math
import time

from django.conf import settings
from django.core.cache import cache

from servers.safety.geometry import decode_polyline

logger = logging.getLogger(__name__)

#: The clock (a seam for tests).
clock = time.time
REROUTE_KEY = 'eta:route:{trip}:{leg}'
OFF_KEY = 'eta:off:{trip}:{leg}'
LOCK_KEY = 'eta:reroute:{trip}'


def _cfg(name, default):
    return float(getattr(settings, name, default))


def _flat(lat, lng, lat0, cos0):
    return math.radians(lng) * cos0 * 6371000.0, math.radians(lat - lat0) * 6371000.0


def along(points, lat, lng):
    """(metres off the line, metres left to its end) for a position, or (None, None)."""
    if len(points) < 2:
        return None, None
    cos0 = math.cos(math.radians(lat))
    me = _flat(lat, lng, lat, cos0)
    flat = [_flat(p[0], p[1], lat, cos0) for p in points]
    seg_len = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(flat, flat[1:])]
    best = (float('inf'), 0, 0.0)
    for i, (a, b) in enumerate(zip(flat, flat[1:])):
        dx, dy = b[0] - a[0], b[1] - a[1]
        seg = dx * dx + dy * dy
        t = 0.0 if seg == 0 else max(0.0, min(1.0, ((me[0] - a[0]) * dx + (me[1] - a[1]) * dy) / seg))
        off = math.hypot(a[0] + t * dx - me[0], a[1] + t * dy - me[1])
        if off < best[0]:
            best = (off, i, t)
    off, i, t = best
    left = seg_len[i] * (1 - t) + sum(seg_len[i + 1:])
    return off, left


def leg_of(trip, phase):
    """The stored route for the phase: a Redis reroute first, else the trip's own fields.
    {polyline, distance_m, duration_s, since} or None."""
    leg = 'pickup' if phase == 'to_pickup' else 'drop'
    try:
        raw = cache.get(REROUTE_KEY.format(trip=trip.id, leg=leg))
        if raw:
            return json.loads(raw)
    except Exception:  # noqa: BLE001
        pass
    if leg == 'pickup' and trip.pickup_route_polyline and trip.pickup_route_duration_s:
        return {'polyline': trip.pickup_route_polyline, 'distance_m': trip.pickup_route_distance_m,
                'duration_s': trip.pickup_route_duration_s,
                'since': (trip.pickup_route_at or trip.accepted_at).timestamp()
                if (trip.pickup_route_at or trip.accepted_at) else None}
    if leg == 'drop' and trip.planned_route_polyline and trip.planned_route_duration_s:
        return {'polyline': trip.planned_route_polyline, 'distance_m': trip.planned_route_distance_m,
                'duration_s': trip.planned_route_duration_s,
                'since': trip.started_at.timestamp() if trip.started_at else None}
    return None


def routed_seconds(trip, phase, driver_at, country=''):
    """Seconds left on the stored route, or None (no route, or off it). Asks for a reroute when
    the rules above say so."""
    route = leg_of(trip, phase)
    if not route or not route.get('distance_m') or not route.get('duration_s'):
        return None
    points = decode_polyline(route['polyline'])
    off, left = along(points, *driver_at)
    if off is None:
        return None
    speed = float(route['distance_m']) / float(route['duration_s'])
    seconds = left / speed if speed > 0 else None
    leg = 'pickup' if phase == 'to_pickup' else 'drop'
    now = clock()
    off_key = OFF_KEY.format(trip=trip.id, leg=leg)
    want = False
    if off > _cfg('ETA_OFFROUTE_M', 150):
        try:
            cache.add(off_key, now, 3600)
            since = float(cache.get(off_key) or now)
        except Exception:  # noqa: BLE001
            since = now
        want = now - since >= _cfg('ETA_OFFROUTE_S', 30)
        seconds = None  # off the route: the caller falls back to the straight line meanwhile
    else:
        try:
            cache.delete(off_key)
        except Exception:  # noqa: BLE001
            pass
        if route.get('since') and seconds is not None:
            planned_left = float(route['duration_s']) - (now - float(route['since']))
            if planned_left > 0 and abs(seconds - planned_left) / float(route['duration_s']) \
                    > _cfg('ETA_DRIFT', 0.30):
                want = True
    if want:
        request_reroute(trip, leg, driver_at, country)
    return seconds


def request_reroute(trip, leg, driver_at, country=''):
    """At most once every ETA_REROUTE_MIN_S per trip; never while the budget says no."""
    try:
        from servers.ride.maps_budget import degrade_level

        if degrade_level(country) >= 2:
            return False
        if not cache.add(LOCK_KEY.format(trip=trip.id), 1, int(_cfg('ETA_REROUTE_MIN_S', 180))):
            return False
    except Exception:  # noqa: BLE001
        return False
    target = ((trip.pickup_lat, trip.pickup_long) if leg == 'pickup'
              else (trip.destination_lat, trip.destination_long))
    try:
        from servers.ride.tasks import reroute_trip

        reroute_trip.delay(trip.id, leg, float(driver_at[0]), float(driver_at[1]),
                           float(target[0]), float(target[1]), country)
    except Exception:  # noqa: BLE001 - no broker: the straight line still answers
        logger.warning('eta: could not enqueue a reroute for trip %s', trip.id, exc_info=True)
    return True


def reroute_now(trip_id, leg, lat, lng, t_lat, t_lng, country=''):
    """The reroute itself (the Celery task body): one routing call, stored in Redis."""
    from servers.ride import maps_meter
    from servers.ride.route_cache import get_route

    with maps_meter.context(trip_id=trip_id, country=country):
        route, _ = get_route(lat, lng, t_lat, t_lng, country)
    if not route or not route.get('polyline'):
        return False
    cache.set(REROUTE_KEY.format(trip=trip_id, leg=leg), json.dumps({
        'polyline': route['polyline'], 'distance_m': route['distance_m'],
        'duration_s': route['duration_s'], 'since': clock()}), 6 * 3600)
    cache.delete(OFF_KEY.format(trip=trip_id, leg=leg))
    return True
