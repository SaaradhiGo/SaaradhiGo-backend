import math
import logging
from decimal import Decimal
from django.utils import timezone
from .models import Trip
from .serializers import TripDetailSerializer
logger = logging.getLogger(__name__)

import requests
from django.conf import settings

from base.countries import get_country

# Fallback fare constants (used when VehicleFarePricing not found in DB)
DEFAULT_BASE_FARE = Decimal('30.00')
DEFAULT_PER_KM_FARE = Decimal('12.00')
DEFAULT_PER_MIN_FARE = Decimal('2.00')
DEFAULT_MIN_FARE = Decimal('50.00')
DEFAULT_NIGHT_SURGE = Decimal('1.50')

# Minimum straight-line distance (km) below which we treat pickup and drop
# as the same point and refuse the trip (rider mis-tap or zero-distance
# fare-mining attempt).
MIN_STRAIGHT_LINE_KM = 0.1

# When Google Distance Matrix is unavailable we approximate road distance
# as straight-line × this factor. Hyderabad's road network is dense; 1.4
# is a conservative empirical multiplier (Manhattan-like grids tend to
# 1.3, dense city centres tend to 1.5).
#
# These two remain as the India/deployment defaults AND as the fallback when
# no zone can be resolved. The per-market values live on the country registry
# and are resolved per trip by `_road_network_factors` below -- 25 km/h is
# Hyderabad traffic, and applying it to a San Francisco trip inflated the
# estimated duration by about 60%, which is a priced component.
ROAD_DISTANCE_FACTOR = 1.4

# Average speed (km/h) we assume for duration estimation when Google fails.
# Hyderabad city traffic averages 20-30 km/h depending on time of day.
FALLBACK_AVG_SPEED_KMH = 25.0

# If client-reported distance deviates more than this ratio from
# server-computed distance, we log it as suspicious — but we never use
# the client distance for the fare computation.
SUSPICIOUS_DEVIATION_RATIO = 0.3


def _haversine_km(lat1, lon1, lat2, lon2):
    """
    Calculate straight-line distance between two lat/lng points using Haversine formula.
    Returns distance in kilometers.
    """
    R = 6371.0  # Earth's radius in km
    lat1, lon1, lat2, lon2 = map(math.radians, [float(lat1), float(lon1), float(lat2), float(lon2)])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def _routing_api_version() -> str:
    """'new' (Routes API, default) or 'legacy' (Distance Matrix / Directions).

    Mirrors GOOGLE_PLACES_API_VERSION. Defaults to NEW for the same reason:
    a Google Cloud project created after Google's legacy cut-off cannot enable
    the legacy routing APIs, so defaulting to them would fail by default on
    every new deployment.
    """
    from django.conf import settings as _s

    value = (getattr(_s, 'GOOGLE_ROUTING_API_VERSION', '') or '').strip().lower()
    return value if value in ('new', 'legacy') else 'new'


def _duration_seconds(value):
    """Routes API durations are strings like '420s'."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text.endswith('s'):
        text = text[:-1]
    try:
        return float(text)
    except ValueError:
        return None


# Distance Matrix answers are cached on the origin/destination pair.
#
# Quoting a ride fans out one request PER VEHICLE CATEGORY -- the website does
# `Promise.all(vehicle_types.map(quoteFare))` -- and each one landed on this
# function uncached. Four categories meant four identical, separately billed
# Distance Matrix calls for the same two points, every time a rider tapped
# "Show fares", including on bookings they abandoned.
#
# The road distance between two fixed points does not change between those
# four calls. Duration does drift with traffic, so the TTL is short: long
# enough to collapse the fan-out and a rider comparing categories, short
# enough that a quote is never built on a stale travel time.
#
# 5 decimal places is ~1.1m, so nudging a pin still reuses the entry.
DISTANCE_MATRIX_CACHE_TTL_SECONDS = 300


def _distance_cache_key(pickup_lat, pickup_long, dest_lat, dest_long):
    return (
        'maps:distancematrix:'
        f'{float(pickup_lat):.5f},{float(pickup_long):.5f}:'
        f'{float(dest_lat):.5f},{float(dest_long):.5f}'
    )


def get_google_maps_distance(pickup_lat, pickup_long, dest_lat, dest_long):
    """
    Fetch distance and duration from Google Maps Distance Matrix API.
    Returns:
        tuple: (distance_km, duration_min) or (None, None) if failed.
    """
    api_key = getattr(settings, 'GOOGLE_MAPS_API_KEY', None)
    if not api_key:
        return None, None

    # Cache lookups must never be able to fail a quote, so every Redis
    # interaction here is advisory: a miss, a decode error or a dead cache
    # just means we ask Google.
    cache_key = None
    try:
        from servers.redis_client import cache_map_data, get_cached_map_data

        cache_key = _distance_cache_key(
            pickup_lat, pickup_long, dest_lat, dest_long,
        )
        cached = get_cached_map_data(cache_key)
        if isinstance(cached, dict):
            km = cached.get('distance_km')
            minutes = cached.get('duration_min')
            if isinstance(km, (int, float)) and isinstance(minutes, (int, float)):
                return float(km), float(minutes)
    except Exception as e:  # noqa: BLE001 -- the cache is never load-bearing
        logger.warning(f"Distance cache read failed: {e}")
        cache_key = None

    try:
        if _routing_api_version() == 'legacy':
            km, minutes = _distance_legacy(
                api_key, pickup_lat, pickup_long, dest_lat, dest_long,
            )
        else:
            km, minutes = _distance_routes(
                api_key, pickup_lat, pickup_long, dest_lat, dest_long,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Google Maps API request failed: {e}")
        return None, None

    if km is None or minutes is None:
        return None, None

    if cache_key:
        try:
            cache_map_data(
                cache_key,
                {'distance_km': km, 'duration_min': minutes},
                ttl_seconds=DISTANCE_MATRIX_CACHE_TTL_SECONDS,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Distance cache write failed: {e}")
    return km, minutes


def _distance_routes(api_key, pickup_lat, pickup_long, dest_lat, dest_long):
    """Road distance and duration from the Routes API.

    Replaces Distance Matrix (Legacy), which a Google Cloud project created
    after its legacy cut-off CANNOT enable. Probing the SaaradhiGo project's
    own key returned, for Distance Matrix and Directions alike:

        "You're calling a legacy API, which is not enabled for your project.
         ... switch to the Places API (New) or Routes API."

    while Geocoding and Places API (New) answered normally. So this is not a
    migration for tidiness; the legacy endpoint cannot be turned on at all,
    and without this every fare would silently fall back to the haversine
    approximation.

    The numbers are the same quantities the legacy API returned -- metres and
    seconds along a driving route -- so the fare computed from them is
    unchanged. `routingPreference` is TRAFFIC_AWARE, matching the legacy
    default of a traffic-adjusted driving duration.

    The field mask is required by the Routes API and is also a cost control:
    asking only for distance and duration keeps the request in the cheapest
    SKU tier rather than paying for geometry nobody reads here.
    """
    response = requests.post(
        'https://routes.googleapis.com/directions/v2:computeRoutes',
        headers={
            'Content-Type': 'application/json',
            'X-Goog-Api-Key': api_key,
            'X-Goog-FieldMask': 'routes.distanceMeters,routes.duration',
        },
        json={
            'origin': {'location': {'latLng': {
                'latitude': float(pickup_lat), 'longitude': float(pickup_long),
            }}},
            'destination': {'location': {'latLng': {
                'latitude': float(dest_lat), 'longitude': float(dest_long),
            }}},
            'travelMode': 'DRIVE',
            'routingPreference': 'TRAFFIC_AWARE',
        },
        timeout=5,
    )
    if response.status_code != 200:
        logger.warning(f"Routes API error {response.status_code}: {response.text[:200]}")
        return None, None

    routes = (response.json() or {}).get('routes') or []
    if not routes:
        return None, None

    distance_m = routes[0].get('distanceMeters')
    duration_s = _duration_seconds(routes[0].get('duration'))
    if distance_m is None or duration_s is None:
        return None, None
    return distance_m / 1000.0, duration_s / 60.0


def _distance_legacy(api_key, pickup_lat, pickup_long, dest_lat, dest_long):
    """Distance Matrix (Legacy).

    Kept for deployments on a Google Cloud project old enough to still have it
    enabled; selected with GOOGLE_ROUTING_API_VERSION=legacy.
    """
    response = requests.get(
        "https://maps.googleapis.com/maps/api/distancematrix/json",
        params={
            "origins": f"{pickup_lat},{pickup_long}",
            "destinations": f"{dest_lat},{dest_long}",
            "key": api_key,
            "mode": "driving",
        },
        timeout=5,
    )
    if response.status_code != 200:
        logger.warning(f"Google Maps API error: {response.text[:200]}")
        return None, None

    data = response.json()
    if data.get("status") != "OK":
        logger.warning(f"Google Maps API error: {data.get('status')}")
        return None, None

    elements = data["rows"][0]["elements"][0]
    if elements.get("status") != "OK":
        return None, None
    return (
        elements["distance"]["value"] / 1000.0,
        elements["duration"]["value"] / 60.0,
    )


def _road_network_factors(pickup_lat, pickup_long):
    """(detour factor, average speed) for the market the pickup is in.

    Only reached on the Google-unavailable fallback path, and deliberately
    tolerant: if the zone lookup fails for any reason the deployment defaults
    are used, because a failed quote is worse than an approximate one.
    """
    try:
        from servers.pricing.services import find_zone_for_point

        zone = find_zone_for_point(pickup_lat, pickup_long)
        country = get_country(getattr(zone, 'country', None))
        return float(country.road_distance_factor), float(country.fallback_avg_speed_kmh)
    except Exception as exc:  # noqa: BLE001
        logger.debug('road-network factor lookup failed, using defaults: %s', exc)
        return ROAD_DISTANCE_FACTOR, FALLBACK_AVG_SPEED_KMH


def validate_distance(distance_km, duration_min, pickup_lat, pickup_long, dest_lat, dest_long):
    """Compute the authoritative server-side distance and duration for a trip.

    The returned (validated_km, validated_min) is what MUST drive the fare
    computation. Callers used to pass the original frontend-supplied
    distance_km into estimate_amount, which let a tampered client inflate
    fare by inflating distance (or under-pay by deflating it). That path
    is no longer trusted: we always recompute server-side.

    Resolution order:
      1. Google Distance Matrix (authoritative for road distance + duration).
      2. Haversine × ROAD_DISTANCE_FACTOR as a fallback when Google fails.

    The frontend-supplied values are kept only for fraud monitoring: a
    significant deviation gets logged.

    Returns:
        tuple: (is_valid: bool, validated_km: float, validated_min: float, message: str)
    """
    try:
        try:
            client_km = float(distance_km) if distance_km is not None else None
        except (ValueError, TypeError):
            client_km = None

        # Try Google Maps first — authoritative if available.
        gm_distance, gm_duration = get_google_maps_distance(
            pickup_lat, pickup_long, dest_lat, dest_long
        )

        if gm_distance is not None and gm_duration is not None:
            # Log significant client/server deviations for fraud monitoring,
            # but compute the fare from the server number regardless.
            if client_km is not None and gm_distance > 0:
                deviation = abs(client_km - gm_distance) / gm_distance
                if deviation > SUSPICIOUS_DEVIATION_RATIO and abs(client_km - gm_distance) > 1.0:
                    logger.warning(
                        f"Suspicious client distance: client={client_km:.2f}km "
                        f"google={gm_distance:.2f}km deviation={deviation:.0%}"
                    )
            return True, round(gm_distance, 2), round(gm_duration, 2), 'OK'

        # Google failed (no key / rate limited / network). Approximate road
        # distance from the great-circle distance plus a road-factor. Never
        # use the client-supplied distance for the fare.
        try:
            straight_line = _haversine_km(pickup_lat, pickup_long, dest_lat, dest_long)
        except (ValueError, TypeError) as e:
            return False, 0, 0, f'Invalid coordinates: {e}'

        if straight_line < MIN_STRAIGHT_LINE_KM:
            return False, 0, 0, 'Pickup and drop locations are too close'

        detour_factor, avg_speed_kmh = _road_network_factors(pickup_lat, pickup_long)
        road_km = round(straight_line * detour_factor, 2)
        road_min = round((road_km / avg_speed_kmh) * 60, 2)

        if client_km is not None and road_km > 0:
            deviation = abs(client_km - road_km) / road_km
            if deviation > SUSPICIOUS_DEVIATION_RATIO and abs(client_km - road_km) > 1.0:
                logger.warning(
                    f"Suspicious client distance (Haversine fallback): "
                    f"client={client_km:.2f}km server={road_km:.2f}km "
                    f"deviation={deviation:.0%}"
                )

        return True, road_km, road_min, 'estimated_from_haversine'
    except (ValueError, TypeError) as e:
        logger.warning(f"Distance validation error: {e}")
        return False, 0, 0, f'Invalid coordinates or distance: {e}'


def _is_night_hours():
    """Check if current time is within night surge hours (11 PM - 5 AM)."""
    current_hour = timezone.localtime(timezone.now()).hour
    return current_hour >= 23 or current_hour < 5


def estimate_amount(distance_km, duration_min, vehicle_type=None, pickup_lat=None,
                    pickup_long=None, rider_id=None, record_demand=False):
    """Estimate fare for a trip.

    Backwards-compatibility wrapper. The fare logic lives in
    `servers.pricing.services.quote_fare` -- this function exists so
    existing callers (estimate_fare view, consumers) keep working
    without per-caller edits.

    Returns the same dict shape callers used to consume from the
    legacy implementation, with two new keys exposed:
        zone_code, rate_card_version
    """
    if not vehicle_type:
        raise ValueError("Vehicle type is required")

    from servers.pricing.services import quote_fare

    fare = quote_fare(
        distance_km=distance_km,
        duration_min=duration_min,
        vehicle_type=vehicle_type,
        pickup_lat=pickup_lat,
        pickup_lon=pickup_long,
        rider_id=rider_id,
        # Only a booking marks demand. See quote_fare(record_demand=...).
        record_demand=record_demand,
    )
    # Preserve the legacy dict shape for existing consumers.
    return {
        'total_fare': fare['total_fare'],
        'base_fare': fare['base_fare'],
        'distance_fare': fare['distance_fare'],
        'time_fare': fare['time_fare'],
        # Named lines that close the gap between the metered components and the
        # amount charged. Without forwarding these the API would keep showing a
        # decomposition that does not add up -- QA trip 45 displayed
        # 60.00 + 55.59 + 19.62 beside a charged total of 135.22.
        'fare_adjustments': fare.get('fare_adjustments', []),
        'surge_multiplier': fare['surge_multiplier'],
        'min_fare_applied': fare['min_fare_applied'],
        'vehicle_type': fare['vehicle_type'],
        'source': fare['source'],
        'zone_code': fare.get('zone_code', ''),
        'rate_card_version': fare.get('rate_card_version'),
        # Market semantics. Forwarded through this wrapper for the same
        # reason `fare_adjustments` had to be: this function rebuilds the
        # payload field by field, so anything quote_fare starts returning is
        # dropped here unless it is named. A client that cannot see `currency`
        # does not know whether 135.22 means rupees or dollars.
        'currency': fare.get('currency'),
        'tax_mode': fare.get('tax_mode'),
        'tax_percent': fare.get('tax_percent'),
        'tax_label': fare.get('tax_label'),
        'tax_amount': fare.get('tax_amount'),
        'per_trip_fee': fare.get('per_trip_fee'),
        'distance_unit': fare.get('distance_unit'),
    }

def get_trip_details(trip_id):
    try:
        obj = Trip.objects.select_related(
            'status_id', 'driver_id', 'driver_id__user_id',
            'vehicle_id', 'vehicle_id__vehicle_type_id'
        ).prefetch_related(
            'fare_pricing', 'ratings', 'ratings__rater_id'
        ).get(id=trip_id)
    except Trip.DoesNotExist:
        return None
    
    data = TripDetailSerializer(obj).data
    
    # We want to format the output for Redis cache as string-based flat fields
    # including driver details
    cache_data = {
        'status': data.get('status'),
        'rider_id': str(obj.user_id_id) if obj.user_id_id else '',
        'driver_id': str(obj.driver_id_id) if obj.driver_id_id else '',
        'pickup_lat': str(obj.pickup_lat),
        'pickup_lng': str(obj.pickup_long),
        'destination_lat': str(obj.destination_lat),
        'destination_lng': str(obj.destination_long),
        'estimated_fare': str(obj.estimated_fare),
        'payment_method': obj.payment_method or 'cash'
    }
    
    driver_name = data.get('driver_name')
    if driver_name:
        cache_data['driver_name'] = str(driver_name)
    
    vehicle_info = data.get('vehicle_info')
    if vehicle_info:
        cache_data['vehicle_model'] = str(vehicle_info.get('model', ''))
        cache_data['vehicle_brand'] = str(vehicle_info.get('brand', ''))
        cache_data['vehicle_number'] = str(vehicle_info.get('vehicle_number', ''))
        cache_data['vehicle_color'] = str(vehicle_info.get('color', ''))
    
    if obj.driver_id:
        cache_data['driver_phone'] = str(obj.driver_id.user_id.phone_number)
        cache_data['driver_rating'] = str(obj.driver_id.ratings)
        
    return cache_data


def process_refund_on_cancel(trip):
    """Process refund if payment was completed online.

    Extracted from TripStatusConsumer._process_refund_on_cancel so both
    the REST endpoint and the WebSocket consumer can share the same logic.
    Callers MUST hold a transaction or ensure the trip row is locked
    (select_for_update) to avoid race conditions with the WebSocket path.

    Returns:
        dict: {'refunded': bool, 'amount': Decimal|None}
    """
    from servers.payments.models import Payment
    from servers.payments.payment_gateways.factory import get_payment_gateway
    from servers.rider.models import Notification

    payment = Payment.objects.filter(
        trip_id=trip, method='online', status='completed'
    ).first()
    if not payment or not payment.gateway_payment_id:
        return {'refunded': False, 'amount': None}

    gateway = get_payment_gateway()
    refund = gateway.create_refund(payment.gateway_payment_id)
    if refund:
        payment.status = 'refunded'
        payment.save(update_fields=['status'])

        trip.payment_status = 'refunded'
        trip.save(update_fields=['payment_status'])

        Notification.objects.create(
            user_id=trip.user_id,
            title='Refund Processed',
            message=(
                f'Your refund of ₹{payment.amount} has been initiated '
                f'due to cancellation.'
            ),
        )
        return {'refunded': True, 'amount': payment.amount}

    return {'refunded': False, 'amount': None}


def create_payment_on_complete(trip):
    """Create a Payment record when trip is completed."""
    from django.conf import settings
    from servers.payments.models import Payment, TransactionHistory

    if trip.payments.exists():
        return

    # What the RIDER pays: the fare less any promo discount. A promo is platform-funded
    # (module 12), so settlement and driver earnings keep reading the full fare.
    from servers.promotions.rider_promos import payable
    amount = payable(trip)
    if not amount:
        logger.warning(f"No fare amount for trip {trip.id}, skipping payment creation")
        return

    payment_method = trip.payment_method or 'deferred'

    if payment_method == 'deferred':
        # Create placeholder payment for deferred selection
        Payment.objects.create(
            trip_id=trip,
            user_id=trip.user_id,
            amount=amount,
            method='deferred',
            status='pending',
        )
        trip.payment_status = 'pending'
        trip.save(update_fields=['payment_status'])
        # No TransactionHistory or driver earnings yet - will be created when payment method is selected
    elif payment_method == 'cash':
        Payment.objects.create(
            trip_id=trip,
            user_id=trip.user_id,
            amount=amount,
            method='cash',
            status='pending',
        )
        trip.payment_status = 'pending'
        trip.save(update_fields=['payment_status'])

        # No TransactionHistory row here: credit_driver_wallet writes
        # exactly one, keyed on an idempotency key. Writing a second one
        # inline meant every cash trip was counted twice in GMV and in
        # the commission report — and a third time if the driver later
        # tapped "confirm cash".
        from servers.driver.utils import settle_or_defer
        settle_or_defer(trip)
    elif payment_method == 'wallet':
        from base.utils import wallet_payment

        wallet_result = wallet_payment(
            user=trip.user_id,
            amount=amount,
            purpose='Trip payment',
            reference_id=f'TRIP_{trip.id}',
            idempotency_key=f'trip_{trip.id}_payment'
        )

        if wallet_result.get('success'):
            Payment.objects.create(
                trip_id=trip,
                user_id=trip.user_id,
                amount=amount,
                method='wallet',
                status='completed',
            )
            trip.payment_status = 'completed'
            trip.save(update_fields=['payment_status'])

            if trip.driver_id:
                TransactionHistory.objects.create(
                    trip_id=trip,
                    user_id=trip.user_id,
                    driver_id=trip.driver_id,
                    amount=amount,
                    method='wallet',
                    user_name=trip.user_id.full_name or trip.user_id.phone_number,
                    status='completed',
                )

            from servers.driver.utils import settle_or_defer
            settle_or_defer(trip)
        else:
            logger.error(f"Wallet payment failed for trip {trip.id}: {wallet_result.get('error')}")
            trip.payment_status = 'failed'
            trip.save(update_fields=['payment_status'])
    else:
        # Online payment: record the intent only. The Cashfree order is
        # created by POST /payments/create-order/, which the app calls
        # from the payment screen. Doing the gateway round-trip here put
        # a third-party HTTP call inside the trip-completion transaction
        # — if it timed out, the trip failed to complete; if the
        # transaction later rolled back, we had orphaned orders at
        # Cashfree with no local row.
        Payment.objects.create(
            trip_id=trip,
            user_id=trip.user_id,
            amount=amount,
            method='online',
            status='pending',
            payment_gateway=getattr(settings, 'PAYMENT_GATEWAY', 'cashfree'),
        )
        trip.payment_status = 'pending'
        trip.save(update_fields=['payment_status'])
