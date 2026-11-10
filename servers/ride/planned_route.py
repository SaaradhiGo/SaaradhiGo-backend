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
Pickup to drop, through `route_cache.get_route` (which wraps `maps_utils.google_directions`) (the same
adapter, key, Routes/Directions switch and failure reporting every maps call
uses). No new vendor, no new API product, no new key.

REUSE, NOT A SECOND CALL
------------------------
The quote, `/ride/maps/directions/` and this task share one cached route per
rounded pickup/drop pair (`route_cache`). A hit records source `maps_cache`;
a miss makes the one call and records `directions`.

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


def _country(trip):
    return str(getattr(getattr(trip, 'zone', None), 'country', '') or '').upper()


def fetch_route(trip):
    """`(route, source)` pickup to drop (normally the route cached at quote time), or
    `(None, reason)` on any failure."""
    from servers.ride import maps_meter, maps_utils
    from servers.ride.route_cache import get_route

    try:
        with maps_meter.context(trip_id=trip.id, country=_country(trip)):
            route, from_cache = get_route(trip.pickup_lat, trip.pickup_long, trip.destination_lat,
                                          trip.destination_long, _country(trip))
    except Exception:  # noqa: BLE001
        return None, 'no_result'
    if route is None:
        return None, maps_utils.MapsFailure.reason() or 'no_result'
    if not (route.get('polyline') or '').strip():
        return None, 'empty_route'
    return route, SOURCE_CACHE if from_cache else SOURCE_DIRECTIONS


def fetch_planned_route(trip):
    """`(polyline, source)` for the trip, or `(None, reason)` on any failure."""
    route, source = fetch_route(trip)
    return ((route['polyline'].strip(), source) if route else (None, source))


def store_pickup_leg(trip):
    """The one routing call of the pickup leg: the accepting driver's position to the pickup
    (9 Nov, maps cost: dispatch itself makes no paid call). Never raises; returns True if stored."""
    from servers.ride import maps_meter
    from servers.ride.eta import driver_position
    from servers.ride.models import Trip
    from servers.ride.route_cache import get_route

    try:
        if trip.pickup_route_polyline or not trip.driver_id_id:
            return False
        at = driver_position(trip.driver_id_id)
        if at is None:
            return False
        with maps_meter.context(trip_id=trip.id, country=_country(trip)):
            route, _ = get_route(at[0], at[1], trip.pickup_lat, trip.pickup_long, _country(trip))
        if not route or not route.get('polyline'):
            return False
        Trip.objects.filter(pk=trip.id, pickup_route_polyline__isnull=True).update(
            pickup_route_polyline=route['polyline'], pickup_route_distance_m=int(route['distance_m']),
            pickup_route_duration_s=int(route['duration_s']), pickup_route_at=timezone.now())
        return True
    except Exception:  # noqa: BLE001 - observation only
        logger.warning('pickup route: failed for trip %s', trip.id, exc_info=True)
        return False


def store_planned_route_for(trip_id):
    """Fetch and store. Returns the source stored, or None. Never raises."""
    from servers.ride.models import Trip

    try:
        trip = Trip.objects.select_related('zone').filter(pk=trip_id).first()
        if trip is None:
            return None
        store_pickup_leg(trip)
        if trip.planned_route_polyline:
            return None
        route, source = fetch_route(trip)
        if not route:
            logger.warning('planned route: none stored for trip %s (%s)', trip_id, source)
            return None
        # Conditional: never overwrite a route a concurrent run stored.
        Trip.objects.filter(pk=trip_id, planned_route_polyline__isnull=True).update(
            planned_route_polyline=route['polyline'].strip(), planned_route_source=source,
            planned_route_at=timezone.now(), planned_route_distance_m=int(route['distance_m']),
            planned_route_duration_s=int(route['duration_s']))
        return source
    except Exception:  # noqa: BLE001 - observation only; never propagates
        logger.warning('planned route: failed for trip %s', trip_id, exc_info=True)
        return None
