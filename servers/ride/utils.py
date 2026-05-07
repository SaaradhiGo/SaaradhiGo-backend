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

# Distance sanity check: max allowed ratio of reported distance to straight-line distance
MAX_DISTANCE_RATIO = 3.0
# Minimum straight-line distance (km) to apply sanity check (skip for very short trips)
MIN_STRAIGHT_LINE_KM = 0.5


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
    """
    Sanity-check the frontend-reported distance and duration using Google Maps Distance Matrix,
    falling back to straight-line distance if API fails.
    
    Returns:
        tuple: (is_valid: bool, validated_km: float, validated_min: float, message: str)
    """
    try:
        distance_km = float(distance_km)
        duration_min = float(duration_min)
        
        # Try Google Maps API first
        gm_distance, gm_duration = get_google_maps_distance(pickup_lat, pickup_long, dest_lat, dest_long)
        
        if gm_distance is not None and gm_duration is not None:
            distance_diff_ratio = abs(distance_km - gm_distance) / max(gm_distance, 0.1)
            
            if distance_diff_ratio > 0.2 and abs(distance_km - gm_distance) > 1.0:
                return False, round(gm_distance, 2), round(gm_duration, 2), (
                    f'Reported distance ({distance_km:.1f} km) deviates significantly from '
                    f'Google Maps ({gm_distance:.1f} km)'
                )
                
            return True, round(gm_distance, 2), round(gm_duration, 2), 'OK'

        # Fallback to straight-line distance
        straight_line = _haversine_km(pickup_lat, pickup_long, dest_lat, dest_long)

        # Skip check for very short trips
        if straight_line < MIN_STRAIGHT_LINE_KM:
            return True, round(straight_line, 2), duration_min, 'OK'

        # Reported distance must be >= straight-line (can't be shorter than bird-flies)
        if distance_km < straight_line * 0.8:
            return False, round(straight_line, 2), duration_min, (
                f'Reported distance ({distance_km:.1f} km) is less than '
                f'straight-line distance ({straight_line:.1f} km)'
            )

        # Reported distance shouldn't be absurdly more than straight-line
        if distance_km > straight_line * MAX_DISTANCE_RATIO:
            return False, round(straight_line, 2), duration_min, (
                f'Reported distance ({distance_km:.1f} km) is more than '
                f'{MAX_DISTANCE_RATIO}x the straight-line distance ({straight_line:.1f} km)'
            )

        return True, round(straight_line, 2), duration_min, 'OK'
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