"""Fare quotes the rider can book at, exactly (owner ruling, 2026-11-06).

"Locked when you book" has to be literally true. Every fare the rider is shown
(`/ride/estimate-fare/`) is issued as a quote: an id, the full fare result and
the trip it prices (rider, pickup, drop, class), kept in the cache for the
country's `quote_valid_seconds` (app-config; 5 minutes by default).

Booking with that id, while it is valid and for the same trip, books at exactly
the quoted fare: no re-pricing. An expired quote, or a pickup or drop that moved
more than `MOVE_TOLERANCE_M`, or a different class, is refused with
`QUOTE_EXPIRED` and a fresh quote, and nothing is booked. The rider is shown the
new fare and confirms again; they are never charged a different amount silently.

A booking WITHOUT a quote id (an app build from before this) is priced at
booking as it always was.
"""

import math
import uuid
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

KEY = 'fare_quote:{}'
DEFAULT_VALID_SECONDS = 300
#: How far a pickup or drop may move and still be the same trip.
MOVE_TOLERANCE_M = 50


def valid_seconds(country_code):
    """The country's quote lifetime from app-config, else 5 minutes."""
    from servers.platform_ops.app_config import POLICIES

    value = (POLICIES.get((country_code or '').upper()) or {}).get('quote_valid_seconds')
    return int(value) if value else DEFAULT_VALID_SECONDS


def _country_of(fare):
    from servers.pricing.models import ServiceZone

    code = fare.get('zone_code') or ''
    return (ServiceZone.objects.filter(code=code).values_list('country', flat=True).first()
            if code else None)


def issue(fare, *, rider_id, pickup, drop, validated_km, validated_min):
    """Store `fare` as a quote. Returns `(quote_id, expires_at)`."""
    seconds = valid_seconds(_country_of(fare))
    expires_at = timezone.now() + timedelta(seconds=seconds)
    quote_id = uuid.uuid4().hex
    cache.set(KEY.format(quote_id), {
        'fare': fare,
        'rider_id': rider_id,
        'pickup': (float(pickup[0]), float(pickup[1])),
        'drop': (float(drop[0]), float(drop[1])),
        'vehicle_type': str(fare.get('vehicle_type') or '').lower(),
        'validated_km': validated_km,
        'validated_min': validated_min,
        'expires_at': expires_at.isoformat(),
    }, timeout=seconds)
    return quote_id, expires_at


def _metres(a, b):
    lat1, lng1, lat2, lng2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2)
    return 6371000 * 2 * math.asin(math.sqrt(h))


def check(quote_id, *, rider_id, pickup, drop, vehicle_type):
    """`(quote, None)` when bookable, else `(None, 'expired' | 'moved')`."""
    quote = cache.get(KEY.format(quote_id)) if quote_id else None
    if quote is None:
        return None, 'expired'
    if quote['rider_id'] is not None and quote['rider_id'] != rider_id:
        return None, 'expired'
    try:
        here = (float(pickup[0]), float(pickup[1]))
        there = (float(drop[0]), float(drop[1]))
    except (TypeError, ValueError):
        return None, 'moved'
    if (_metres(here, quote['pickup']) > MOVE_TOLERANCE_M
            or _metres(there, quote['drop']) > MOVE_TOLERANCE_M):
        return None, 'moved'
    if vehicle_type and str(vehicle_type).lower() != quote['vehicle_type']:
        return None, 'moved'
    return quote, None


def _s(value):
    return None if value is None else str(value)


def public(fare, quote_id, expires_at):
    """The quote as the app reads it: the estimate-fare fields it books from."""
    return {
        'quote_id': quote_id,
        'quote_expires_at': expires_at.isoformat(),
        'estimated_fare': str(fare['total_fare']),
        'currency': fare.get('currency'),
        'distance_unit': fare.get('distance_unit'),
        'vehicle_type': fare.get('vehicle_type'),
        'fare_breakdown': {
            'base_fare': _s(fare.get('base_fare')),
            'distance_fare': _s(fare.get('distance_fare')),
            'time_fare': _s(fare.get('time_fare')),
            'surge_multiplier': _s(fare.get('surge_multiplier')),
            'min_fare_applied': fare.get('min_fare_applied'),
            'tax_mode': fare.get('tax_mode'),
            'tax_label': fare.get('tax_label'),
            'tax_percent': _s(fare.get('tax_percent')),
            'tax_amount': _s(fare.get('tax_amount')),
            'per_trip_fee': _s(fare.get('per_trip_fee')),
        },
        'fare_adjustments': [
            {'code': a['code'], 'label': a['label'], 'amount': str(a['amount'])}
            for a in fare.get('fare_adjustments', [])
        ],
    }
