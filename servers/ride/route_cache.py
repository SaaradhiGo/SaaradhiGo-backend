"""One Google route call per journey, shared by every rider, class and screen (owner, 9 Nov).

The route (distance, duration, polyline) and the price are separate: the route comes from Google
once and is cached here; the price is worked out by our own fare engine from the cached route
every time it is needed, so demand, time of day and night fees stay current at no Maps cost.

KEY: pickup and drop each snapped to a grid of `MAPS_ROUTE_GRID_M` (about 75 m).
Not the rider, not the vehicle class. A miss also looks in the 8 neighbouring cells of each end
and takes an entry whose real endpoints are within one grid step, so dragging a pin a few metres
across a cell edge never pays for a new route.

LIFETIME: `MAPS_ROUTE_TTL_S` (30 min), per market in `MAPS_ROUTE_TTL`, shorter in the peak window
(`MAPS_ROUTE_PEAK_TTL_S` during `MAPS_ROUTE_PEAK_HOURS`, local hours); doubled while a country's
Maps budget is spent (`maps_budget.degrade_level >= 1`). Each entry carries `cached_at`, so an
entry past its lifetime is a miss even if Redis still holds it.

A cache-add lock makes concurrent first callers wait for the leader instead of each paying Google.
Every answer is metered: `route_cache_hit` or `route_cache_miss` (unbilled), and the call itself
under its routing SKU.
"""

import logging
import math
import time
import uuid
from datetime import datetime, timezone

from django.conf import settings
from django.core.cache import cache

from servers.ride import maps_meter

logger = logging.getLogger(__name__)

LOCK_TTL_SECONDS = 10
LOCK_WAIT_SECONDS = 3.0
POLL_SECONDS = 0.1
#: The longest Redis keeps any entry (the real lifetime is checked against `cached_at`).
STORE_TTL_SECONDS = 4 * 3600
#: The clock (a seam for tests).
clock = time.time


def grid_m():
    return float(getattr(settings, 'MAPS_ROUTE_GRID_M', 75))


def _local_hour(country, now):
    try:
        from zoneinfo import ZoneInfo

        from base.countries import get_country

        tz = ZoneInfo(get_country(country or None).default_timezone)
        return datetime.fromtimestamp(now, timezone.utc).astimezone(tz).hour
    except Exception:  # noqa: BLE001
        return datetime.fromtimestamp(now, timezone.utc).hour


def ttl_seconds(country='', now=None):
    """The route lifetime for a market at this moment."""
    now = now if now is not None else clock()
    by_market = getattr(settings, 'MAPS_ROUTE_TTL', {}) or {}
    ttl = float(by_market.get((country or '').upper()) or getattr(settings, 'MAPS_ROUTE_TTL_S', 1800))
    peak, peak_ttl = getattr(settings, 'MAPS_ROUTE_PEAK_HOURS', None), \
        getattr(settings, 'MAPS_ROUTE_PEAK_TTL_S', None)
    if peak and peak_ttl:
        hour = _local_hour(country, now)
        if any(start <= hour < end for start, end in peak):
            ttl = min(ttl, float(peak_ttl))
    try:
        from servers.ride.maps_budget import degrade_level

        if degrade_level(country) >= 1:
            ttl *= 2
    except Exception:  # noqa: BLE001 - the budget guard is advisory
        pass
    return ttl


def _cell(lat, lng):
    step_lat = grid_m() / 111320.0
    step_lng = grid_m() / (111320.0 * max(0.01, math.cos(math.radians(float(lat)))))
    return int(math.floor(float(lat) / step_lat)), int(math.floor(float(lng) / step_lng))


def _cell_key(country, o_cell, d_cell):
    # Coordinates are already global: the key carries no country, so the quote (which does not
    # know its market) and the accept (which does) share one entry. `country` only sets the
    # lifetime and the metering.
    return f'maps:route:v2:{o_cell[0]},{o_cell[1]}:{d_cell[0]},{d_cell[1]}'


def route_cache_key(o_lat, o_lng, d_lat, d_lng, country=''):
    return _cell_key(country, _cell(o_lat, o_lng), _cell(d_lat, d_lng))


def _metres(lat1, lng1, lat2, lng2):
    lat1, lng1, lat2, lng2 = (math.radians(float(v)) for v in (lat1, lng1, lat2, lng2))
    a = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2)
    return 2 * 6371000 * math.asin(math.sqrt(a))


def _read(key, country):
    from servers.redis_client import get_cached_map_data

    try:
        data = get_cached_map_data(key)
    except Exception:  # noqa: BLE001 - the cache is advisory
        return None
    if not isinstance(data, dict) or data.get('distance_m') is None:
        return None
    if clock() - float(data.get('cached_at') or 0) > ttl_seconds(country):
        return None
    return data


def _write(key, route):
    from servers.redis_client import cache_map_data

    try:
        cache_map_data(key, route, ttl_seconds=STORE_TTL_SECONDS)
    except Exception:  # noqa: BLE001
        logger.warning('route cache write failed', exc_info=True)


def _near(o_lat, o_lng, d_lat, d_lng, country):
    """A live entry in a neighbouring cell whose real endpoints are within one grid step."""
    oc, dc = _cell(o_lat, o_lng), _cell(d_lat, d_lng)
    for a in (-1, 0, 1):
        for b in (-1, 0, 1):
            for c in (-1, 0, 1):
                for d in (-1, 0, 1):
                    if a == b == c == d == 0:
                        continue
                    hit = _read(_cell_key(country, (oc[0] + a, oc[1] + b), (dc[0] + c, dc[1] + d)),
                                country)
                    ends = (hit or {}).get('ends')
                    if ends and _metres(o_lat, o_lng, ends[0], ends[1]) <= grid_m() \
                            and _metres(d_lat, d_lng, ends[2], ends[3]) <= grid_m():
                        return hit
    return None


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
        route['cached_at'] = clock()
        route['ends'] = [o_lat, o_lng, d_lat, d_lng]
        _write(key, route)
    return route


def get_route(o_lat, o_lng, d_lat, d_lng, country=''):
    """(route, from_cache); route is None when Google has no answer."""
    o_lat, o_lng, d_lat, d_lng = (float(v) for v in (o_lat, o_lng, d_lat, d_lng))
    key = route_cache_key(o_lat, o_lng, d_lat, d_lng, country)
    hit = _read(key, country) or _near(o_lat, o_lng, d_lat, d_lng, country)
    if hit:
        maps_meter.record('route_cache_hit', country=country)
        return hit, True
    maps_meter.record('route_cache_miss', country=country)

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
        hit = _read(key, country)
        if hit:
            return hit, True
        try:
            if cache.get(lock_key) is None:
                break  # leader finished without a result
        except Exception:  # noqa: BLE001
            break
    return _fetch(o_lat, o_lng, d_lat, d_lng, key), False
