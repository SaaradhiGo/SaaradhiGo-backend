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


def get_google_maps_distance(pickup_lat, pickup_long, dest_lat, dest_long):
    """Road distance and duration from the shared per-pair route result.

    One Routes call per rounded pickup/drop pair serves every vehicle-type quote,
    /maps/directions and the planned route (see route_cache). The call is
    TRAFFIC_AWARE, which bills as Routes Pro: the SKU follows routingPreference
    and the requested fields, not the field mask alone.

    Returns:
        tuple: (distance_km, duration_min) or (None, None) if failed.
    """
    if not getattr(settings, 'GOOGLE_MAPS_API_KEY', None):
        return None, None
    try:
        from servers.ride.route_cache import get_route

        route, _ = get_route(pickup_lat, pickup_long, dest_lat, dest_long)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Google Maps API request failed: {e}")
        return None, None
    if not route:
        return None, None
    return route['distance_m'] / 1000.0, route['duration_s'] / 60.0


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

    # Never more than was paid less every refund already issued (AUDIT 27.76).
    from base.money import format_money, from_minor
    from servers.ride.admin_trips import refundable_minor

    refund_minor = min(payment.amount_minor, refundable_minor(trip))
    if refund_minor <= 0:
        return {'refunded': False, 'amount': None}

    # Refund through the provider the payment was taken on (recorded on the row).
    gateway = get_payment_gateway(payment.payment_gateway or 'cashfree')
    refund = gateway.create_refund(payment.gateway_payment_id, amount_minor=refund_minor,
                                   currency=payment.currency)
    if refund:
        payment.status = 'refunded'
        payment.save(update_fields=['status'])

        trip.payment_status = 'refunded'
        trip.save(update_fields=['payment_status'])

        Notification.objects.create(
            user_id=trip.user_id,
            title='Refund Processed',
            message=(
                f'Your refund of {format_money(from_minor(refund_minor, payment.currency), payment.currency)} '
                f'has been initiated due to cancellation.'
            ),
        )
        return {'refunded': True, 'amount': from_minor(refund_minor, payment.currency)}

    return {'refunded': False, 'amount': None}


def create_payment_on_complete(trip):
    """Create a Payment record when trip is completed."""
    from django.conf import settings
    from servers.payments.models import Payment, TransactionHistory

    if trip.payments.exists():
        return

    # What the RIDER pays: the fare less any promo discount. A promo is platform-funded
    # (module 12), so settlement and driver earnings keep reading the full fare.
    from servers.promotions.rider_promos import discount_minor, fare_minor, payable
    amount = payable(trip)
    if not amount and discount_minor(trip) and fare_minor(trip) > 0:
        # A promo covered the whole fare: the rider owes nothing, so there is no payment to wait for
        # and no gateway order. The trip still settles NOW, on the full fare, exactly as with a
        # smaller promo (driver earnings and commission unchanged; the discount is platform-funded,
        # and on a cash trip the driver, who collects 0, is credited it).
        from servers.driver.utils import settle_or_defer
        trip.payment_status = 'completed'
        trip.save(update_fields=['payment_status'])
        settle_or_defer(trip)
        return
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
                    user_name=trip.user_id.full_name or '',
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
        from servers.payments.payment_gateways.factory import country_for_trip, provider_name_for

        Payment.objects.create(
            trip_id=trip,
            user_id=trip.user_id,
            amount=amount,
            method='online',
            status='pending',
            payment_gateway=provider_name_for('payments', country_for_trip(trip)) or '',
        )
        trip.payment_status = 'pending'
        trip.save(update_fields=['payment_status'])
