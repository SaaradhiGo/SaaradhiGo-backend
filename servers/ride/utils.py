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
    """
    Estimate fare using VehicleFarePricing from DB.
    Falls back to default constants if vehicle type pricing not found.
    Calculates dynamic surge based on ratio of nearby demand to supply.
    
    Args:
        distance_km: Distance in kilometers (from frontend/Google Maps)
        duration_min: Duration in minutes (from frontend/Google Maps)
        vehicle_type: Vehicle type name (e.g. 'bike', 'auto', 'car', 'suv', 'luxury') or None for defaults
        pickup_lat: Latitude where ride starts
        pickup_long: Longitude where ride starts
        rider_id: The ID of the authenticated rider
    
    Returns:
        dict: {
            'total_fare': Decimal,
            'base_fare': Decimal,
            'distance_fare': Decimal,
            'time_fare': Decimal,
            'surge_multiplier': Decimal,
            'min_fare_applied': bool,
            'vehicle_type': str,
            'source': str  ('db' or 'default')
        }
    """
    try:
        distance_km = max(Decimal(str(distance_km)), Decimal('0'))
        duration_min = max(Decimal(str(duration_min)), Decimal('0'))
    except (ValueError, TypeError, ArithmeticError) as e:
        logger.warning(f"Invalid fare input: {e}, returning defaults with zero distance")
        distance_km = Decimal('0')
        duration_min = Decimal('0')

    if vehicle_type:
        try:
            from servers.ride.models import VehicleFarePricing
            from servers.driver.models import VehicleType

            vt = VehicleType.objects.filter(type__iexact=vehicle_type).first()
            if vt:
                pricing = VehicleFarePricing.objects.filter(vehicle_type_id=vt).first()
                if pricing:
                    base_fare = pricing.base_fare
                    per_km = pricing.per_km_fare
                    per_min = pricing.per_min_fare
                    min_fare = pricing.min_fare
                    night_surge = pricing.night_surge_multiplier
                    source = 'db'
                else:
                    logger.info(f"No fare pricing found for vehicle type '{vehicle_type}', using defaults")
            else:
                logger.info(f"Vehicle type '{vehicle_type}' not found, using defaults")
        except Exception as e:
            logger.warning(f"DB lookup failed for vehicle type '{vehicle_type}': {e}, using defaults")
    else:
        raise ValueError("Vehicle type is required")

    # Calculate fare components
    distance_fare = per_km * distance_km
    time_fare = per_min * duration_min
    subtotal = base_fare + distance_fare + time_fare

    # Track overall surge 
    surge_multiplier = Decimal('1.00')
    
    # 1. Apply night surge rules
    # if _is_night_hours():
    #     surge_multiplier = night_surge
    #     subtotal = subtotal * surge_multiplier

    # 2. Dynamic Micro-Surge based on real-time Density
    dynamic_surge = Decimal('1.00')
    if pickup_lat and pickup_long:
        from servers.redis_client import nearby_drivers, add_rider_ping, count_nearby_active_riders
        
        # Ping the rider's presence if id is available
        if rider_id:
            add_rider_ping(rider_id, pickup_long, pickup_lat)
            
        try:
            # Get supply (drivers nearby in 3km)
            nearby_supply_list = nearby_drivers(pickup_long, pickup_lat, radius=3000, count=100) or []
            supply = len(nearby_supply_list)
            
            # Get demand (riders nearby pinging in last 3 mins)
            demand = count_nearby_active_riders(pickup_long, pickup_lat, radius=3000)
            
            safe_supply = max(supply, 1) # Prevent div-zero
            ratio = demand / safe_supply
            
            # Apply tiered surge limits
            if ratio >= 5:
                dynamic_surge = Decimal('1.50')
            elif ratio >= 3:
                dynamic_surge = Decimal('1.30')
            elif ratio >= 1.5:
                dynamic_surge = Decimal('1.15')
            elif ratio < 0.5 and supply >= 10:
                # Give a minor discount if supply heavily outweighs demand
                dynamic_surge = Decimal('0.90')
                
            subtotal = subtotal * dynamic_surge
            surge_multiplier = surge_multiplier * dynamic_surge
        except Exception as e:
            logger.error(f"Failed to calculate dynamic surge: {e}")

    # Apply minimum fare constraint after all surges applied
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
        'min_fare_applied': min_fare_applied,
        'vehicle_type': vehicle_type or 'default',
        'source': source,
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