import math
import logging
from decimal import Decimal
from django.utils import timezone
from .models import Trip
from .serializers import TripDetailSerializer
logger = logging.getLogger(__name__)

import requests
from django.conf import settings

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


def get_google_maps_distance(pickup_lat, pickup_long, dest_lat, dest_long):
    """
    Fetch distance and duration from Google Maps Distance Matrix API.
    Returns:
        tuple: (distance_km, duration_min) or (None, None) if failed.
    """
    api_key = getattr(settings, 'GOOGLE_MAPS_API_KEY', None)
    if not api_key:
        return None, None

    url = "https://maps.googleapis.com/maps/api/distancematrix/json"
    params = {
        "origins": f"{pickup_lat},{pickup_long}",
        "destinations": f"{dest_lat},{dest_long}",
        "key": api_key,
        "mode": "driving",
    }
    try:
        response = requests.get(url, params=params, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "OK":
                elements = data["rows"][0]["elements"][0]
                if elements.get("status") == "OK":
                    distance_m = elements["distance"]["value"]
                    duration_s = elements["duration"]["value"]
                    print(distance_m / 1000.0, duration_s / 60.0)
                    return distance_m / 1000.0, duration_s / 60.0
        logger.warning(f"Google Maps API error: {response.text}")
    except Exception as e:
        logger.warning(f"Google Maps API request failed: {e}")
    return None, None


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

        road_km = round(straight_line * ROAD_DISTANCE_FACTOR, 2)
        road_min = round((road_km / FALLBACK_AVG_SPEED_KMH) * 60, 2)

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


def estimate_amount(distance_km, duration_min, vehicle_type=None, pickup_lat=None, pickup_long=None, rider_id=None):
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
    )
    # Preserve the legacy dict shape for existing consumers.
    return {
        'total_fare': fare['total_fare'],
        'base_fare': fare['base_fare'],
        'distance_fare': fare['distance_fare'],
        'time_fare': fare['time_fare'],
        'surge_multiplier': fare['surge_multiplier'],
        'min_fare_applied': fare['min_fare_applied'],
        'vehicle_type': fare['vehicle_type'],
        'source': fare['source'],
        'zone_code': fare.get('zone_code', ''),
        'rate_card_version': fare.get('rate_card_version'),
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