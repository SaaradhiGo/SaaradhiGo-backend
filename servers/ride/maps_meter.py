"""Per-SKU count of outbound Google Maps calls, per UTC day, in the Django cache.

Incremented at the single point each request is made (maps_utils), so the
admin integrations page shows what was actually sent, not what was intended.
"""

import logging
from datetime import datetime, timedelta, timezone

from django.core.cache import cache

logger = logging.getLogger(__name__)

TTL_SECONDS = 40 * 24 * 3600
SKUS = (
    'places_autocomplete_session', 'places_autocomplete', 'places_details',
    'geocoding', 'routes_pro', 'routes_essentials', 'directions_legacy',
)


def _key(day, sku):
    return f'maps:meter:{day:%Y%m%d}:{sku}'


def record(sku):
    """Count one outbound call. Never raises: metering must not fail a request."""
    key = _key(datetime.now(timezone.utc), sku)
    try:
        cache.add(key, 0, TTL_SECONDS)
        cache.incr(key)
    except Exception:  # noqa: BLE001
        logger.warning('maps meter: could not count %s', sku, exc_info=True)


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
