"""Multi-city pricing service.

This is the only module the rest of the codebase should call to:

    1. find which ServiceZone a (lat, lon) sits in
    2. look up the effective RateCard for a (zone, vehicle_type, time)
    3. compute the dynamic surge multiplier from Redis demand/supply
    4. produce a full fare quote

Anywhere else that hardcodes a polygon, queries VehicleFarePricing
directly, or computes a fare ad-hoc is a bug -- redirect it here.

The Hyderabad-only `base.service_area` module is kept as a thin
backwards-compatibility shim that delegates to find_zone_for_point().
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, Tuple

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone
from shapely.geometry import Point, Polygon, shape as shapely_shape

from servers.pricing.models import PlatformSettings, RateCard, ServiceZone

logger = logging.getLogger(__name__)


class PricingUnavailable(Exception):
    """Raised when no rate card governs a location and defaults are disabled.

    Callers should surface this to the rider as "we don't price rides here
    yet" rather than quoting a fallback number.
    """


# ---------------------------------------------------------------------------
# Polygon cache
# ---------------------------------------------------------------------------
# Building Shapely polygons from GeoJSON on every request is wasteful and
# breaks our 30ms-per-fare-estimate budget. We cache (zone_id -> Polygon)
# in-process; invalidate when a zone is updated. The cache key is
# (zone_id, updated_at) so a CRUD edit transparently refreshes.

_POLYGON_CACHE: dict[int, Tuple[float, Polygon]] = {}


def _polygon_for_zone(zone: ServiceZone) -> Optional[Polygon]:
    """Return a cached Shapely Polygon for the zone, or None if invalid."""
    cached = _POLYGON_CACHE.get(zone.id)
    updated_ts = zone.updated_at.timestamp() if zone.updated_at else 0.0
    if cached and cached[0] == updated_ts:
        return cached[1]

    try:
        geo = zone.polygon_geojson
        if not geo or not isinstance(geo, dict):
            return None
        if geo.get('type') == 'Polygon' and 'coordinates' in geo:
            poly = shapely_shape(geo)
        else:
            # Tolerate a raw list of [lon, lat] pairs.
            coords = geo if isinstance(geo, list) else geo.get('coordinates')
            if not coords:
                return None
            poly = Polygon(coords)
        if not poly.is_valid or poly.is_empty:
            logger.warning('Invalid polygon for zone %s', zone.code)
            return None
        _POLYGON_CACHE[zone.id] = (updated_ts, poly)
        return poly
    except Exception as exc:  # noqa: BLE001
        logger.exception('Failed to parse polygon for zone %s: %s', zone.code, exc)
        return None


def invalidate_polygon_cache(zone_id: Optional[int] = None) -> None:
    """Drop cached polygons. Called from signals on ServiceZone.save()."""
    if zone_id is None:
        _POLYGON_CACHE.clear()
    else:
        _POLYGON_CACHE.pop(zone_id, None)


# ---------------------------------------------------------------------------
# Zone lookup
# ---------------------------------------------------------------------------

def find_zone_for_point(lat, lon) -> Optional[ServiceZone]:
    """Return the most specific active ServiceZone containing (lat, lon).

    "Most specific" = highest priority. Sub-zones (priority 100) win over
    cities (priority 10) win over states (priority 5). Country zones are
    typically priority 1 and act as a fallback.

    Returns None if no active zone contains the point.

    Inputs may be Decimal, str, or float; unparsable inputs return None
    so callers treat 'could not validate' the same as 'out of service'.
    """
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
        return None

    point = Point(lon_f, lat_f)  # NB: Shapely is (lon, lat)
    candidates = ServiceZone.objects.filter(is_active=True).order_by('-priority')

    for zone in candidates:
        poly = _polygon_for_zone(zone)
        if poly is None:
            continue
        if poly.contains(point):
            return zone
    return None


def is_inside_service_area(lat, lon) -> bool:
    """True iff (lat, lon) sits in any active ServiceZone."""
    return find_zone_for_point(lat, lon) is not None


def validate_service_area(pickup_lat, pickup_lon, dest_lat, dest_lon) -> Tuple[bool, str]:
    """Validate BOTH pickup and drop sit in an active zone."""
    if not is_inside_service_area(pickup_lat, pickup_lon):
        return False, (
            'Pickup location is outside the SaaradhiGo service area.'
        )
    if not is_inside_service_area(dest_lat, dest_lon):
        return False, (
            'Drop location is outside the SaaradhiGo service area.'
        )
    return True, ''


# ---------------------------------------------------------------------------
# Rate card resolution
# ---------------------------------------------------------------------------

def get_active_rate_card(zone: ServiceZone, vehicle_type, at=None) -> Optional[RateCard]:
    """Return the effective RateCard for (zone, vehicle_type) at `at`.

    Resolution walks UP the zone parent chain so an Airport sub-zone with
    no card falls back to the City card. Returns None if no card is
    found anywhere in the chain (caller should fall back to defaults).
    """
    at = at or timezone.now()

    cursor: Optional[ServiceZone] = zone
    seen: set[int] = set()
    while cursor is not None and cursor.id not in seen:
        seen.add(cursor.id)
        card = (
            RateCard.objects
            .filter(
                zone=cursor,
                vehicle_type=vehicle_type,
                is_active=True,
                effective_from__lte=at,
            )
            .filter(Q(effective_to__isnull=True) | Q(effective_to__gt=at))
            .order_by('-effective_from', '-version')
            .first()
        )
        if card is not None:
            return card
        cursor = cursor.parent
    return None


def rate_card_for_trip(at=None, trip=None):
    """Resolve the RateCard that governs a trip.

    Prefers the zone stamped on the trip at booking time; falls back to a
    point-in-polygon lookup on the pickup for legacy rows written before
    `Trip.zone` existed.
    """
    if trip is None:
        return None
    zone = getattr(trip, 'zone', None)
    if zone is None:
        zone = find_zone_for_point(trip.pickup_lat, trip.pickup_long)
    if zone is None:
        return None
    vt = trip.requested_vehicle_type
    if vt is None:
        vehicle = getattr(trip, 'vehicle_id', None)
        vt = getattr(vehicle, 'vehicle_type_id', None)
    if vt is None:
        return None
    # Price the trip on the card that was in force when it was requested,
    # not on whatever card is live at settlement time.
    return get_active_rate_card(zone, vt, at=at or trip.requested_at)


def get_platform_setting(key: str, default, *, cast=None, cache_ttl=300):
    """Read a runtime setting from the database, with a Redis cache bustable on save."""
    cache_key = f'platform_setting:{key}'
    if cast is None:
        cast = lambda value: value

    cached = cache.get(cache_key)
    if cached is not None:
        try:
            return cast(cached)
        except (TypeError, ValueError, ArithmeticError):
            pass

    try:
        setting = PlatformSettings.objects.filter(key=key).first()
        if setting is None:
            return default
        value = setting.get_value()
        cache.set(cache_key, value, timeout=cache_ttl)
        return cast(value)
    except Exception:  # noqa: BLE001
        return default


def commission_percent_for_trip(trip) -> Decimal:
    """Platform commission % for a trip.

    Order of authority:
      1. RateCard.commission_percent for the trip's zone + vehicle type,
         effective at the time the trip was requested
      2. PlatformSettings.PLATFORM_COMMISSION_PERCENT (database-backed, restart-free)
      3. settings.PLATFORM_COMMISSION_PERCENT fallback
      4. default of 18%
    """
    try:
        card = rate_card_for_trip(trip=trip)
        if card is not None and card.commission_percent is not None:
            return Decimal(str(card.commission_percent))
    except Exception as exc:  # noqa: BLE001
        logger.warning('commission lookup failed for trip %s: %s', getattr(trip, 'id', '?'), exc)

    setting_value = get_platform_setting(
        'PLATFORM_COMMISSION_PERCENT',
        getattr(settings, 'PLATFORM_COMMISSION_PERCENT', Decimal('18')),
        cast=lambda v: Decimal(str(v)),
        cache_ttl=60,
    )
    try:
        return Decimal(str(setting_value))
    except (ValueError, ArithmeticError):
        return Decimal('18')


def gst_percent_for_trip(trip) -> Decimal:
    """GST % applicable to a trip, from its zone's rate card."""
    try:
        card = rate_card_for_trip(trip=trip)
        if card is not None and card.gst_percent is not None:
            return Decimal(str(card.gst_percent))
    except Exception:  # noqa: BLE001
        pass
    return Decimal('5.00')


# ---------------------------------------------------------------------------
# Surge engine
# ---------------------------------------------------------------------------

def compute_surge_multiplier(
    pickup_lat,
    pickup_lon,
    cap: Decimal,
    rider_id: Optional[int] = None,
    radius_m: int = 3000,
    record_demand: bool = False,
) -> Decimal:
    """Tiered demand/supply surge multiplier, capped by the rate card.

    Reads from Redis (driver geo + active-rider pings). Returns 1.00 if
    Redis is unavailable or the inputs are missing, so a Redis outage
    never blocks a fare quote.
    """
    if pickup_lat is None or pickup_lon is None:
        return Decimal('1.00')

    try:
        from servers.redis_client import (
            add_rider_ping,
            count_nearby_active_riders,
            nearby_drivers,
        )
    except Exception:  # noqa: BLE001
        return Decimal('1.00')

    try:
        # Only a real booking attempt counts as demand. Recording a ping on
        # every fare *estimate* let a rider inflate the surge they were
        # quoted just by reopening the estimate screen, and made the surge
        # signal trivially manipulable from an unauthenticated-ish client
        # loop. Estimates read demand; bookings write it.
        if record_demand and rider_id is not None:
            try:
                add_rider_ping(rider_id, pickup_lon, pickup_lat)
            except Exception as exc:  # noqa: BLE001
                logger.debug('add_rider_ping failed: %s', exc)

        supply_list = nearby_drivers(pickup_lon, pickup_lat, radius=radius_m, count=100) or []
        supply = len(supply_list)
        demand = count_nearby_active_riders(pickup_lon, pickup_lat, radius=radius_m)

        safe_supply = max(supply, 1)
        ratio = demand / safe_supply

        if ratio >= 5:
            surge = Decimal('1.50')
        elif ratio >= 3:
            surge = Decimal('1.30')
        elif ratio >= 1.5:
            surge = Decimal('1.15')
        elif ratio < 0.5 and supply >= 10:
            surge = Decimal('0.90')  # over-supply discount
        else:
            surge = Decimal('1.00')

        # Honour the per-zone MVA-2020 cap.
        if surge > cap:
            surge = cap
        return surge
    except Exception as exc:  # noqa: BLE001
        logger.warning('compute_surge_multiplier failed: %s', exc)
        return Decimal('1.00')


def _is_night_now(card: RateCard, at=None) -> bool:
    """Is the supplied local hour inside the card's night window?"""
    try:
        check_time = at or timezone.now()
        hour = timezone.localtime(check_time).hour
    except Exception:  # noqa: BLE001
        return False

    start = int(card.night_surge_start_hour)
    end = int(card.night_surge_end_hour)

    if start == end:
        return False

    if start < end:
        return start <= hour < end

    # wraps midnight, e.g. 23 -> 5
    return hour >= start or hour < end

# ---------------------------------------------------------------------------
# Quote
# ---------------------------------------------------------------------------

@dataclass
class FareQuote:
    total_fare: Decimal
    base_fare: Decimal
    distance_fare: Decimal
    time_fare: Decimal
    surge_multiplier: Decimal
    night_surge_applied: bool
    min_fare_applied: bool
    vehicle_type: str
    zone_code: str
    rate_card_version: Optional[int]
    source: str  # 'db' or 'default'
    breakdown: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            'total_fare': self.total_fare,
            'base_fare': self.base_fare,
            'distance_fare': self.distance_fare,
            'time_fare': self.time_fare,
            'surge_multiplier': self.surge_multiplier,
            'night_surge_applied': self.night_surge_applied,
            'min_fare_applied': self.min_fare_applied,
            'vehicle_type': self.vehicle_type,
            'zone_code': self.zone_code,
            'rate_card_version': self.rate_card_version,
            'source': self.source,
            'breakdown': self.breakdown,
        }


# Last-resort defaults if no zone + no rate card found AND defaults are
# enabled via setting. Used so a fresh DB does not break local dev /
# tests. Production should always have at least one active zone +
# rate card seeded -- if not, the fare quote refuses with source='none'.
_DEFAULTS = {
    'base_fare': Decimal('30.00'),
    'per_km_fare': Decimal('12.00'),
    'per_min_fare': Decimal('2.00'),
    'min_fare': Decimal('50.00'),
    'night_surge_multiplier': Decimal('1.00'),
    'surge_cap_multiplier': Decimal('1.50'),
}


def quote_fare(
    distance_km,
    duration_min,
    vehicle_type: str,
    pickup_lat=None,
    pickup_lon=None,
    rider_id: Optional[int] = None,
    at=None,
    record_demand: bool = False,
) -> dict:
    """Produce a complete fare quote.

    This is the single entry point used by /estimate-fare/, the trip
    creation flow, and any future quote endpoint (split fares,
    scheduled rides, etc.). Always returns a dict; never raises on
    missing data -- falls back to defaults so the rider sees a price.

    Returns the dict shape consumed by the existing utils.estimate_amount
    callers so we can swap implementations without touching every caller.
    """
    if not vehicle_type:
        raise ValueError('vehicle_type is required')

    try:
        distance_km = max(Decimal(str(distance_km)), Decimal('0'))
        duration_min = max(Decimal(str(duration_min)), Decimal('0'))
    except (ValueError, TypeError, ArithmeticError):
        distance_km = Decimal('0')
        duration_min = Decimal('0')

    from servers.driver.models import VehicleType
    vt = VehicleType.objects.filter(type__iexact=vehicle_type).first()

    zone = None
    if pickup_lat is not None and pickup_lon is not None:
        zone = find_zone_for_point(pickup_lat, pickup_lon)

    card = None
    if zone is not None and vt is not None:
        card = get_active_rate_card(zone, vt, at=at)

    if card is None:
        # No rate card anywhere in the zone's parent chain. In production
        # that is a configuration error (a city was seeded without cards),
        # and quoting the hardcoded Hyderabad-era defaults below would
        # silently sell rides at the wrong price. Fail loudly instead;
        # local dev and the test suite keep the defaults.
        from django.conf import settings as _settings
        allow_default = getattr(_settings, 'PRICING_ALLOW_DEFAULT_FARE', False)
        if not allow_default:
            logger.error(
                'No RateCard for zone=%s vehicle_type=%s — refusing to quote',
                zone.code if zone else None, vehicle_type,
            )
            raise PricingUnavailable(
                'Pricing is not configured for this area yet. Please try again later.'
            )

    if card is not None:
        base_fare = card.base_fare
        per_km = card.per_km_fare
        per_min = card.per_min_fare
        min_fare = card.min_fare
        night_surge = card.night_surge_multiplier
        surge_cap = card.surge_cap_multiplier
        source = 'db'
        rate_card_version = card.version
    else:
        base_fare = _DEFAULTS['base_fare']
        per_km = _DEFAULTS['per_km_fare']
        per_min = _DEFAULTS['per_min_fare']
        min_fare = _DEFAULTS['min_fare']
        night_surge = _DEFAULTS['night_surge_multiplier']
        surge_cap = _DEFAULTS['surge_cap_multiplier']
        source = 'default'
        rate_card_version = None

    distance_fare = per_km * distance_km
    time_fare = per_min * duration_min
    subtotal = base_fare + distance_fare + time_fare

    surge_multiplier = Decimal('1.00')

    night_applied = False
    if card is not None and night_surge > Decimal('1.00') and _is_night_now(card, at=at):
        subtotal = subtotal * night_surge
        surge_multiplier = surge_multiplier * night_surge
        night_applied = True

    dyn = compute_surge_multiplier(
        pickup_lat, pickup_lon, cap=surge_cap, rider_id=rider_id,
        record_demand=record_demand,
    )
    if dyn != Decimal('1.00'):
        subtotal = subtotal * dyn
        surge_multiplier = surge_multiplier * dyn

    # Cap total surge effect at the zone's surge_cap_multiplier. Without
    # this, night_surge * dynamic_surge could exceed the MVA cap.
    if surge_multiplier > surge_cap:
        # Re-derive subtotal from the cap rather than compounding.
        pre_surge = base_fare + distance_fare + time_fare
        subtotal = pre_surge * surge_cap
        surge_multiplier = surge_cap

    min_fare_applied = False
    if subtotal < min_fare:
        subtotal = min_fare
        min_fare_applied = True

    total_fare = round(subtotal, 2)

    return {
        'total_fare': total_fare,
        'base_fare': round(base_fare, 2),
        'distance_fare': round(distance_fare, 2),
        'time_fare': round(time_fare, 2),
        'surge_multiplier': round(surge_multiplier, 2),
        'night_surge_applied': night_applied,
        'min_fare_applied': min_fare_applied,
        'vehicle_type': vehicle_type,
        'zone_code': zone.code if zone else '',
        'rate_card_version': rate_card_version,
        'source': source,
    }
