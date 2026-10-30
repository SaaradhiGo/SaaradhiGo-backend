"""The planned route of an accepted trip, stored once for route deviation.

WHEN, AND WHY NOT SOONER
------------------------
`schedule_planned_route(trip_id)` is called from the accept path INSIDE its
transaction and only registers a `transaction.on_commit` hook; the hook
enqueues the Celery task `ride.store_planned_route` and nothing else. So the
accept pays no maps latency, cannot be failed by maps, and the response the
rider and the driver get is byte-for-byte what it was. The enqueue itself is
wrapped: a broker that is down is logged, never raised.

WHAT IT CALLS
-------------
Pickup to drop, through the existing `maps_utils.google_directions` (the same
adapter, key, Routes/Directions switch and failure reporting every maps call
uses). No new vendor, no new API product, no new key.

REUSE, NOT A SECOND CALL
------------------------
The booking flow's `/ride/maps/directions/` proxy caches its answer for five
minutes under `maps:directions:<lat>,<lng>:<lat>,<lng>` (four decimals). If
that entry is there for this trip's pickup and drop, its polyline is used and
the source is recorded as `maps_cache`; otherwise one Directions call is made
and the source is `directions`. (The fare estimate caches only distance and
duration, never a polyline, so it cannot be reused for this.)

FAILURE IS A NULL
-----------------
Any maps failure, an empty route or a missing key leaves the three fields
untouched (null) and writes one log line. The task never retries into the
maps quota: a trip without a stored route is simply skipped by the detector.

THROTTLE
--------
The task is rate limited (`PLANNED_ROUTE_RATE`) on top of the existing
adapter, and is idempotent: a trip that already has a route is not fetched
again.
"""

import logging

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

SOURCE_CACHE = 'maps_cache'
SOURCE_DIRECTIONS = 'directions'
#: Celery per-worker rate limit for the fetch. Generous for accept volume,
#: and a hard stop on a runaway loop.
PLANNED_ROUTE_RATE = '60/m'


def directions_cache_key(o_lat, o_lng, d_lat, d_lng):
    """The key `ride.views.proxy_directions` writes (4 decimal places)."""
    o_lat, o_lng, d_lat, d_lng = (round(float(v), 4) for v in (o_lat, o_lng, d_lat, d_lng))
    return f'maps:directions:{o_lat},{o_lng}:{d_lat},{d_lng}'


def polyline_of(result):
    """The encoded polyline in a (legacy-shaped) directions result, or ''."""
    try:
        return (result['routes'][0]['overview_polyline']['points'] or '').strip()
    except (KeyError, IndexError, TypeError):
        return ''


def schedule_planned_route(trip_id):
    """Register the fetch to run after the surrounding transaction commits.

    Never raises: nothing here may fail an accept.
    """
    try:
        def enqueue():
            try:
                from servers.ride.tasks import store_planned_route

                store_planned_route.delay(trip_id)
            except Exception:  # noqa: BLE001 - the accept already succeeded
                logger.warning('planned route: could not enqueue for trip %s', trip_id,
                               exc_info=True)

        transaction.on_commit(enqueue)
    except Exception:  # noqa: BLE001
        logger.warning('planned route: could not register for trip %s', trip_id, exc_info=True)


def fetch_planned_route(trip):
    """`(polyline, source)` for the trip, or `(None, reason)` on any failure."""
    from servers.redis_client import get_cached_map_data

    o_lat, o_lng = trip.pickup_lat, trip.pickup_long
    d_lat, d_lng = trip.destination_lat, trip.destination_long
    try:
        cached = get_cached_map_data(directions_cache_key(o_lat, o_lng, d_lat, d_lng))
    except Exception:  # noqa: BLE001 - the cache is advisory
        cached = None
    line = polyline_of(cached) if isinstance(cached, dict) else ''
    if line:
        return line, SOURCE_CACHE

    from servers.ride import maps_utils

    result = maps_utils.google_directions(
        round(float(o_lat), 4), round(float(o_lng), 4),
        round(float(d_lat), 4), round(float(d_lng), 4))
    if result is None:
        return None, maps_utils.MapsFailure.reason() or 'no_result'
    line = polyline_of(result)
    if not line:
        return None, 'empty_route'
    return line, SOURCE_DIRECTIONS


def store_planned_route_for(trip_id):
    """Fetch and store. Returns the source stored, or None. Never raises."""
    from servers.ride.models import Trip

    try:
        trip = Trip.objects.filter(pk=trip_id).only(
            'id', 'pickup_lat', 'pickup_long', 'destination_lat', 'destination_long',
            'planned_route_polyline').first()
        if trip is None:
            return None
        if trip.planned_route_polyline:
            return None
        line, source = fetch_planned_route(trip)
        if not line:
            logger.warning('planned route: none stored for trip %s (%s)', trip_id, source)
            return None
        # Conditional: never overwrite a route a concurrent run stored.
        Trip.objects.filter(pk=trip_id, planned_route_polyline__isnull=True).update(
            planned_route_polyline=line, planned_route_source=source,
            planned_route_at=timezone.now())
        return source
    except Exception:  # noqa: BLE001 - observation only; never propagates
        logger.warning('planned route: failed for trip %s', trip_id, exc_info=True)
        return None
