"""Count every outbound Google Maps call: per SKU, per surface, per country, per trip, per day.

Incremented at the single point each request is made (maps_utils), so the console shows what was
actually sent, not what was intended. Two stores:

* the cache counter per UTC day and SKU (unchanged; the integrations page's 7-day view reads it);
* `MapsUsage` rows (9 Nov), the durable record the cost report, the budget guard and the
  per-trip figures read. A client that loads a map (the web's Maps JS) reports it through
  `POST /ride/maps/meter/`, which lands here too.

Who is calling, and for which trip, comes from `context(...)` (a context variable the trip code
sets around its calls) or the request's `X-SG-Surface` header; the default is `backend`.
Never raises: metering must not fail a request.
"""

import contextlib
import contextvars
import logging
from datetime import datetime, timedelta, timezone

from django.core.cache import cache

logger = logging.getLogger(__name__)

TTL_SECONDS = 40 * 24 * 3600
SKUS = (
    'places_autocomplete_session', 'places_autocomplete', 'places_details',
    'geocoding', 'routes_pro', 'routes_essentials', 'directions_legacy',
)
#: Billed on the client but counted here (the web's map loads).
CLIENT_SKUS = ('dynamic_maps',)
#: Counted, never billed: the route cache's answers.
UNBILLED = ('route_cache_hit', 'route_cache_miss')
#: The SKUs that are a routing call (A2/A4 tests count these).
ROUTING = ('routes_pro', 'routes_essentials', 'directions_legacy')
SURFACES = ('rider_web', 'rider_app', 'driver_app', 'backend')

_context = contextvars.ContextVar('maps_meter_context', default=None)


@contextlib.contextmanager
def context(*, surface=None, trip_id=None, country=None):
    """Attribute the calls made inside the block to a surface, a trip and a country."""
    current = dict(_context.get() or {})
    current.update({k: v for k, v in (('surface', surface), ('trip_id', trip_id),
                                      ('country', country)) if v is not None})
    token = _context.set(current)
    try:
        yield
    finally:
        _context.reset(token)


def _key(day, sku):
    return f'maps:meter:{day:%Y%m%d}:{sku}'


def record(sku, *, surface=None, trip_id=None, country=None, count=1):
    """Count `count` calls of `sku`. Never raises."""
    ctx = _context.get() or {}
    surface = surface or ctx.get('surface') or 'backend'
    if surface not in SURFACES:
        surface = 'backend'
    trip_id = int(trip_id or ctx.get('trip_id') or 0)
    country = str(country or ctx.get('country') or '').upper()[:2]
    now = datetime.now(timezone.utc)
    if sku in SKUS:
        key = _key(now, sku)
        try:
            cache.add(key, 0, TTL_SECONDS)
            cache.incr(key) if count == 1 else cache.incr(key, count)
        except Exception:  # noqa: BLE001
            logger.warning('maps meter: could not count %s', sku, exc_info=True)
    try:
        from django.db import IntegrityError, transaction
        from django.db.models import F

        from servers.ride.maps_models import MapsUsage

        match = dict(day=now.date(), sku=sku, surface=surface, country=country, trip_id=trip_id)
        if not MapsUsage.objects.filter(**match).update(count=F('count') + count):
            try:
                with transaction.atomic():
                    MapsUsage.objects.create(count=count, **match)
            except IntegrityError:
                MapsUsage.objects.filter(**match).update(count=F('count') + count)
    except Exception:  # noqa: BLE001
        logger.warning('maps meter: could not store %s', sku, exc_info=True)


def snapshot(days=7):
    """{'today': {sku: n}, 'last_7_days': {YYYY-MM-DD: {sku: n}}}; today is in both."""
    today = datetime.now(timezone.utc)
    by_day = {}
    for back in range(days):
        day = today - timedelta(days=back)
        counts = {}
        for sku in SKUS:
            try:
                counts[sku] = int(cache.get(_key(day, sku)) or 0)
            except Exception:  # noqa: BLE001
                counts[sku] = 0
        by_day[f'{day:%Y-%m-%d}'] = counts
    return {'today': by_day[f'{today:%Y-%m-%d}'], 'last_7_days': by_day}


def trip_counts(trip_id):
    """{sku: n} for one trip, from the durable rows."""
    from django.db.models import Sum

    from servers.ride.maps_models import MapsUsage

    rows = (MapsUsage.objects.filter(trip_id=trip_id).values('sku')
            .annotate(n=Sum('count')).order_by('sku'))
    return {r['sku']: r['n'] for r in rows}
