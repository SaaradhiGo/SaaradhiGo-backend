"""One Google route call per rounded pickup->drop pair, shared by every consumer.

The quote (per vehicle type), /maps/directions, validate_distance and the planned
route stored on accept all read the same cached result. A cache-add lock makes
concurrent first callers wait for the leader instead of each paying Google.
"""

import logging
import time
import uuid

from django.core.cache import cache

logger = logging.getLogger(__name__)

ROUTE_TTL_SECONDS = 900
LOCK_TTL_SECONDS = 10
LOCK_WAIT_SECONDS = 3.0
POLL_SECONDS = 0.1


def route_cache_key(o_lat, o_lng, d_lat, d_lng):
    o_lat, o_lng, d_lat, d_lng = (round(float(v), 4) for v in (o_lat, o_lng, d_lat, d_lng))
    return f'maps:route:v1:{o_lat},{o_lng}:{d_lat},{d_lng}'


def _read(key):
    from servers.redis_client import get_cached_map_data

    try:
        data = get_cached_map_data(key)
    except Exception:  # noqa: BLE001 - the cache is advisory
        return None
    return data if isinstance(data, dict) and data.get('distance_m') is not None else None


def _write(key, route):
    from servers.redis_client import cache_map_data

    try:
        cache_map_data(key, route, ttl_seconds=ROUTE_TTL_SECONDS)
    except Exception:  # noqa: BLE001
        logger.warning('route cache write failed', exc_info=True)


def parse_directions(result):
    """Legacy-shaped directions payload -> {distance_m, duration_s, polyline, route}."""
    try:
        route = result['routes'][0]
        leg = route['legs'][0]
        return {
            'distance_m': float(leg['distance']['value']),
            'duration_s': float(leg['duration']['value']),
            'polyline': (route.get('overview_polyline') or {}).get('points', '') or '',
            'route': route,
        }
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _fetch(o_lat, o_lng, d_lat, d_lng, key):
    from servers.ride import maps_utils

    result = maps_utils.google_directions(o_lat, o_lng, d_lat, d_lng)
    route = parse_directions(result) if result else None
    if route:
        _write(key, route)
    return route


def get_route(o_lat, o_lng, d_lat, d_lng):
    """(route, from_cache); route is None when Google has no answer."""
    o_lat, o_lng, d_lat, d_lng = (round(float(v), 4) for v in (o_lat, o_lng, d_lat, d_lng))
    key = route_cache_key(o_lat, o_lng, d_lat, d_lng)
    hit = _read(key)
    if hit:
        return hit, True

    lock_key, token = f'{key}:lock', uuid.uuid4().hex
    try:
        leader = cache.add(lock_key, token, LOCK_TTL_SECONDS)
    except Exception:  # noqa: BLE001 - no lock store: just make the one call
        return _fetch(o_lat, o_lng, d_lat, d_lng, key), False

    if leader:
        try:
            return _fetch(o_lat, o_lng, d_lat, d_lng, key), False
        finally:
            try:
                if cache.get(lock_key) == token:
                    cache.delete(lock_key)
            except Exception:  # noqa: BLE001
                pass

    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        hit = _read(key)
        if hit:
            return hit, True
        try:
            if cache.get(lock_key) is None:
                break  # leader finished without a result
        except Exception:  # noqa: BLE001
            break
    return _fetch(o_lat, o_lng, d_lat, d_lng, key), False
