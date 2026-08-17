import redis
import logging
from django.conf import settings
from servers.driver.utils import update_driver_location
from servers.driver.models import Driver
from servers.ride.utils import get_trip_details
logger = logging.getLogger(__name__)

# Initialize Redis client with connection pool and error handling
try:
    redis_client = redis.Redis.from_url(
        settings.REDIS_URL + '/2',
        decode_responses=True,
        socket_connect_timeout=5,
        socket_keepalive=True,
        socket_keepalive_options={} if hasattr(redis, 'SOCKET_KEEPALIVE_OPTIONS') else None
    )
    # Test the connection
    redis_client.ping()
    logger.info("Geo connection established successfully")
except (redis.ConnectionError, redis.TimeoutError, Exception) as e:
    logger.error(f"Failed to connect to Redis: {str(e)}")
    redis_client = None

# Legacy single-key geo index. Kept only so a rolling deploy can still
# evict members written by the previous build; nothing writes to it now.
GEO_KEY = 'drivers:geo'

# One sorted set per vehicle type: drivers:geo:sedan, drivers:geo:bike, ...
# Filtering by vehicle type has to happen at the Redis query layer. The old
# code did GEOSEARCH ... COUNT 50 against a single global key and then
# filtered in Python, so a sedan request in a bike-dense area matched zero
# drivers even with sedans a few hundred metres away.
GEO_KEY_PREFIX = 'drivers:geo:'

# Heartbeat key per driver. The geo entry is only authoritative while the
# heartbeat is alive; a driver whose process died without a clean
# disconnect used to stay in the index forever ("ghost drivers").
HEARTBEAT_PREFIX = 'driver:heartbeat:'
HEARTBEAT_TTL_SECONDS = 45

# Cached vehicle type per driver, so the location hot path never touches
# Postgres. At the design target of 1k pings/sec the old
# `Driver.objects.get(...)` per ping was 1k QPS for a value that changes
# once a month.
VEHICLE_TYPE_PREFIX = 'driver:vehicle_type:'
VEHICLE_TYPE_TTL_SECONDS = 3600

# Drivers who were offered a given trip, so losers can be told to dismiss
# the request card the moment somebody accepts.
OFFERED_PREFIX = 'trip:offered:'
OFFERED_TTL_SECONDS = 900

ACTIVE_TRIP_PREFIX = 'driver:active_trip:'
RIDE_CACHE_PREFIX = 'ride:'
RIDE_CACHE_TTL_SECONDS = 86400  # 24 hours safety net for orphaned trips


def geo_key_for(vehicle_type):
    """Redis key holding the geo index for one vehicle type."""
    return f'{GEO_KEY_PREFIX}{str(vehicle_type or "unknown").strip().lower()}'


def get_driver_vehicle_type(driver_id, refresh=False):
    """Vehicle type for a driver, cached in Redis.

    Falls back to a single DB read on a cache miss and re-caches the
    result. Returns None if the driver has no active vehicle.
    """
    key = f'{VEHICLE_TYPE_PREFIX}{driver_id}'
    if redis_client is not None and not refresh:
        try:
            cached = redis_client.get(key)
            if cached:
                return None if cached == '-' else cached
        except Exception as exc:  # noqa: BLE001
            logger.debug('vehicle-type cache read failed: %s', exc)

    vehicle_type = None
    try:
        vehicle = (
            Driver.objects.select_related(
                'active_vehicle__vehicle_type_id'
            ).only('id', 'active_vehicle').get(id=driver_id).active_vehicle
        )
        if vehicle and vehicle.vehicle_type_id:
            vehicle_type = str(vehicle.vehicle_type_id.type).strip().lower()
    except Driver.DoesNotExist:
        vehicle_type = None
    except Exception as exc:  # noqa: BLE001
        logger.warning('vehicle-type lookup failed for driver %s: %s', driver_id, exc)
        return None

    if redis_client is not None:
        try:
            if vehicle_type:
                redis_client.setex(key, VEHICLE_TYPE_TTL_SECONDS, vehicle_type)
        except Exception as exc:  # noqa: BLE001
            logger.debug('vehicle-type cache write failed: %s', exc)
    return vehicle_type


def invalidate_driver_vehicle_type(driver_id):
    """Call when a driver's active vehicle changes."""
    if redis_client is None:
        return False
    try:
        redis_client.delete(f'{VEHICLE_TYPE_PREFIX}{driver_id}')
        return True
    except Exception:  # noqa: BLE001
        return False


def add_offered_drivers(trip_id, driver_ids):
    """Record which drivers were offered a trip (for `trip_taken` fanout)."""
    if redis_client is None or not driver_ids:
        return False
    try:
        key = f'{OFFERED_PREFIX}{trip_id}'
        redis_client.sadd(key, *[str(d) for d in driver_ids])
        redis_client.expire(key, OFFERED_TTL_SECONDS)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning('add_offered_drivers failed for trip %s: %s', trip_id, exc)
        return False


def pop_offered_drivers(trip_id):
    """Read and delete the offer set. Returns a list of driver id strings."""
    if redis_client is None:
        return []
    try:
        key = f'{OFFERED_PREFIX}{trip_id}'
        members = redis_client.smembers(key) or set()
        redis_client.delete(key)
        return list(members)
    except Exception as exc:  # noqa: BLE001
        logger.warning('pop_offered_drivers failed for trip %s: %s', trip_id, exc)
        return []


def clear_offered_drivers(trip_id):
    if redis_client is None:
        return False
    try:
        redis_client.delete(f'{OFFERED_PREFIX}{trip_id}')
        return True
    except Exception:  # noqa: BLE001
        return False


def sweep_stale_drivers():
    """Remove geo entries whose heartbeat has expired.

    Runs on Celery beat. Without it, a driver whose app was killed (or
    whose Daphne worker crashed) stays in the geo index and keeps getting
    matched — riders wait out the full accept timeout for a driver who is
    not there.

    Returns the number of members evicted.
    """
    if redis_client is None:
        return 0
    evicted = 0
    try:
        for key in redis_client.scan_iter(match=f'{GEO_KEY_PREFIX}*', count=100):
            members = redis_client.zrange(key, 0, -1) or []
            stale = []
            for member in members:
                driver_id = member.split(':')[1] if ':' in member else member
                if not redis_client.exists(f'{HEARTBEAT_PREFIX}{driver_id}'):
                    stale.append(member)
            if stale:
                redis_client.zrem(key, *stale)
                evicted += len(stale)
                logger.info('Swept %s stale driver(s) from %s', len(stale), key)
    except Exception as exc:  # noqa: BLE001
        logger.error('sweep_stale_drivers failed: %s', exc)
    return evicted


def set_driver_active_trip(driver_id, trip_id):
    """
    Set the active trip for a driver to mark them as busy.
    """
    if redis_client is None:
        return False
    try:
        key = f"{ACTIVE_TRIP_PREFIX}{driver_id}"
        redis_client.set(key, str(trip_id))
        return True
    except Exception as e:
        logger.error(f"Failed to set active trip for driver {driver_id}: {str(e)}")
        return False


def get_driver_active_trip(driver_id):
    """
    Get the active trip for a driver. Returns trip_id if busy, None if available.
    """
    if redis_client is None:
        return None
    try:
        key = f"{ACTIVE_TRIP_PREFIX}{driver_id}"
        trip_id = redis_client.get(key)
        return int(trip_id) if trip_id else None
    except Exception as e:
        logger.error(f"Failed to get active trip for driver {driver_id}: {str(e)}")
        return None


def clear_driver_active_trip(driver_id):
    """
    Clear the active trip for a driver, marking them as available.
    """
    if redis_client is None:
        return False
    try:
        key = f"{ACTIVE_TRIP_PREFIX}{driver_id}"
        redis_client.delete(key)
        return True
    except Exception as e:
        logger.error(f"Failed to clear active trip for driver {driver_id}: {str(e)}")
        return False


def cache_trip(trip_id, **fields):
    """
    Cache active trip state as a Redis Hash.

    Args:
        trip_id: The trip's database ID
        **fields: Key-value pairs to store. Common fields:
            status (str): Trip status (e.g., 'requested', 'accepted', 'in_progress')
            rider_id (str): Rider's user ID
            driver (dict): Driver's profile (optional until accepted)
            pickup_lat (str): Pickup latitude
            pickup_lng (str): Pickup longitude
            destination_lat (str): Destination latitude
            destination_lng (str): Destination longitude
            estimated_fare (str): String-serialized Decimal fare
            payment_method (str): Payment method (cash/online)
            otp (str): Otp for the trip
    Returns:
        bool: True on success, False on failure
    """
    if redis_client is None:
        logger.error("Redis client not available for cache_trip")
        return False
    try:
        key = f"{RIDE_CACHE_PREFIX}{trip_id}"
        # Convert all values to strings for Redis compatibility
        string_fields = {k: str(v) for k, v in fields.items() if v is not None}
        if not string_fields:
            return False
        redis_client.hset(key, mapping=string_fields)
        redis_client.expire(key, RIDE_CACHE_TTL_SECONDS)
        logger.info(f"Trip {trip_id} cached with fields: {list(string_fields.keys())}")
        return True
    except redis.RedisError as e:
        logger.error(f"Redis error caching trip {trip_id}: {str(e)}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error caching trip {trip_id}: {str(e)}")
        return False


def get_cached_trip(trip_id):
    """
    Retrieve cached trip state from Redis.

    Args:
        trip_id: The trip's database ID

    Returns:
        dict: All cached fields as strings, or None if not found or on error
    """
    if redis_client is None:
        logger.error("Redis client not available for get_cached_trip")
        return None
    try:
        key = f"{RIDE_CACHE_PREFIX}{trip_id}"
        data = redis_client.hgetall(key)
        if not data:
            logger.debug(f"Trip {trip_id} not found in cache")
            data = get_trip_details(trip_id)
            if data:
                cache_trip(trip_id, **data)
            else:
                logger.error(f"Trip {trip_id} not found in database")
                return {}
        return data
    except redis.RedisError as e:
        logger.error(f"Redis error fetching cached trip {trip_id}: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error fetching cached trip {trip_id}: {str(e)}")
        return None


def invalidate_trip(trip_id):
    """
    Remove a trip from the cache. Call this when a trip is completed or cancelled.

    Args:
        trip_id: The trip's database ID

    Returns:
        bool: True if key existed and was deleted, False otherwise
    """
    if redis_client is None:
        logger.error("Redis client not available for invalidate_trip")
        return False
    try:
        key = f"{RIDE_CACHE_PREFIX}{trip_id}"
        deleted = redis_client.delete(key)
        if deleted:
            logger.info(f"Trip {trip_id} invalidated from cache")
            return True
        else:
            logger.debug(f"Trip {trip_id} was not in cache (already expired or never cached)")
            return False
    except redis.RedisError as e:
        logger.error(f"Redis error invalidating trip {trip_id}: {str(e)}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error invalidating trip {trip_id}: {str(e)}")
        return False

def _validate_coordinates(lng, lat):
    """
    Validate geographic coordinates.
    
    Args:
        lng: Longitude value (-180 to 180)
        lat: Latitude value (-90 to 90)
    
    Returns:
        tuple: (is_valid, error_message)
    """
    try:
        lng = float(lng)
        lat = float(lat)
    except (TypeError, ValueError):
        return False, "Longitude and latitude must be valid numbers"
    
    if not (-180 <= lng <= 180):
        return False, "Longitude must be between -180 and 180"
    if not (-90 <= lat <= 90):
        return False, "Latitude must be between -90 and 90"
    
    return True, None


def add_driver_location(driver_id, lng, lat):
    """
    Add or update driver location in Redis geospatial index.
    
    Args:
        driver_id: Unique driver identifier
        lng: Longitude coordinate
        lat: Latitude coordinate

    Returns:
        dict: Status and message
    
    Raises:
        ValueError: If coordinates are invalid
        redis.RedisError: If Redis operation fails
    """
    if redis_client is None:
        logger.error("Redis client not available")
        return {"success": False, "error": "Redis connection unavailable"}
    
    try:
        # Validate inputs
        if not driver_id:
            raise ValueError("driver_id cannot be empty")
        
        is_valid, error_msg = _validate_coordinates(lng, lat)
        if not is_valid:
            raise ValueError(error_msg)
        
        lng = float(lng)
        lat = float(lat)

        # Vehicle type comes from the Redis cache, not a per-ping DB read.
        vehicle_type = get_driver_vehicle_type(driver_id)
        if not vehicle_type:
            return {"success": False, "error": "Driver has no active vehicle"}

        member = f'driver:{driver_id}:{vehicle_type}'
        geo_key = geo_key_for(vehicle_type)

        # Refresh the presence heartbeat on every ping. The geo entry is
        # only trusted while this key is alive (see sweep_stale_drivers).
        redis_client.setex(f'{HEARTBEAT_PREFIX}{driver_id}', HEARTBEAT_TTL_SECONDS, '1')

        update_driver_location(driver_id=driver_id, lat=lat, lng=lng)

        # Check if driver is on an active trip
        active_trip_id = get_driver_active_trip(driver_id)
        if active_trip_id:
            # Driver is busy, remove from geo index but return trip_id for streaming
            redis_client.zrem(geo_key, member)
            logger.debug(
                "Driver %s is on trip %s; streaming location but omitted from nearby.",
                driver_id, active_trip_id,
            )
            return {
                "success": True,
                "message": "Location updated for active trip",
                "active_trip_id": active_trip_id,
                "lng": lng,
                "lat": lat
            }

        # Driver is available, add to the per-vehicle-type geo index
        result = redis_client.geoadd(geo_key, [lng, lat, member])
        if result is not None:
            logger.debug(
                "Driver %s location updated in %s: lng=%s lat=%s",
                driver_id, geo_key, lng, lat,
            )
            return {"success": True, "message": "Location added successfully"}
        else:
            logger.warning(f"Failed to add driver {driver_id} location to geo index")
            return {"success": False, "error": "Failed to add location"}

    except ValueError as e:
        logger.warning(f"Validation error for driver {driver_id}: {str(e)}")
        return {"success": False, "error": str(e)}
    except redis.RedisError as e:
        logger.error(f"Redis error while adding driver {driver_id} location: {str(e)}")
        return {"success": False, "error": "Database operation failed"}
    except Exception as e:
        logger.error(f"Unexpected error adding driver {driver_id} location: {str(e)}")
        return {"success": False, "error": "An unexpected error occurred"}


def nearby_drivers(lng, lat, radius=5000, count=50, vehicle_type=None):
    """
    Search for nearby drivers within a specified radius.
    
    Args:
        lng: Longitude coordinate
        lat: Latitude coordinate
        radius: Search radius in meters (default: 1000)
        count: Maximum number of results (default: 10)
    
    Returns:
        list: List of nearby drivers with distance and coordinates
        None: If operation fails
    """
    if redis_client is None:
        logger.error("Redis client not available for nearby_drivers query")
        return None
    
    try:
        # Validate inputs
        is_valid, error_msg = _validate_coordinates(lng, lat)
        if not is_valid:
            raise ValueError(error_msg)
        
        if radius <= 0:
            raise ValueError("Radius must be greater than 0")
        
        if count <= 0:
            raise ValueError("Count must be greater than 0")

        # Query only the requested vehicle type's index. When no type is
        # given (ops/live-map style queries) we union across all of them.
        if vehicle_type:
            keys = [geo_key_for(vehicle_type)]
        else:
            keys = list(redis_client.scan_iter(match=f'{GEO_KEY_PREFIX}*', count=100))

        drivers = []
        for key in keys:
            found = redis_client.geosearch(
                key,
                longitude=lng,
                latitude=lat,
                radius=radius,
                count=count,
                unit='m',
                sort='ASC',
                withdist=True,
                withcoord=True,
            ) or []
            drivers.extend(found)

        # Drop anything whose heartbeat has lapsed but that the sweeper has
        # not evicted yet — never hand a dispatcher a ghost.
        live = []
        for entry in drivers:
            member = entry[0] if isinstance(entry, (list, tuple)) else entry
            driver_id = member.split(':')[1] if ':' in member else member
            if redis_client.exists(f'{HEARTBEAT_PREFIX}{driver_id}'):
                live.append(entry)

        if len(keys) > 1:
            # Multi-key union is not distance-sorted; restore ASC by distance.
            live.sort(key=lambda e: float(e[1]) if isinstance(e, (list, tuple)) else 0.0)

        logger.debug(
            'nearby_drivers: %s live of %s indexed at lng=%s lat=%s (types=%s)',
            len(live), len(drivers), lng, lat, vehicle_type or 'all',
        )
        return live[:count]

    except ValueError as e:
        logger.warning(f"Validation error in nearby_drivers: {str(e)}")
        return None
    except redis.RedisError as e:
        logger.error(f"Redis error during nearby drivers search: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error during nearby drivers search: {str(e)}")
        return None


def remove_driver(driver_id):
    """
    Remove driver from geospatial index.
    
    Args:
        driver_id: Unique driver identifier
    
    Returns:
        dict: Status and message
    """
    if redis_client is None:
        logger.error("Redis client not available")
        return {"success": False, "error": "Redis connection unavailable"}
    
    try:
        if not driver_id:
            raise ValueError("driver_id cannot be empty")

        # Sweep every vehicle-type index, not just the driver's current one.
        # Keying the removal off the *current* active vehicle orphaned the
        # old member forever whenever a driver switched vehicles.
        removed = 0
        for key in redis_client.scan_iter(match=f'{GEO_KEY_PREFIX}*', count=100):
            for member in (redis_client.zrange(key, 0, -1) or []):
                if member.startswith(f'driver:{driver_id}:'):
                    removed += redis_client.zrem(key, member)
        # Legacy single-key index, for members written by an older build.
        for member in (redis_client.zrange(GEO_KEY, 0, -1) or []):
            if member.startswith(f'driver:{driver_id}:'):
                removed += redis_client.zrem(GEO_KEY, member)

        redis_client.delete(f'{HEARTBEAT_PREFIX}{driver_id}')

        if removed > 0:
            logger.info(f"Driver {driver_id} removed from geo index")
            return {"success": True, "message": "Driver removed successfully"}
        else:
            logger.debug(f"Driver {driver_id} not present in geo index")
            return {"success": False, "error": "Driver not found"}

    except ValueError as e:
        logger.warning(f"Validation error for driver {driver_id}: {str(e)}")
        return {"success": False, "error": str(e)}
    except redis.RedisError as e:
        logger.error(f"Redis error while removing driver {driver_id}: {str(e)}")
        return {"success": False, "error": "Database operation failed"}
    except Exception as e:
        logger.error(f"Unexpected error removing driver {driver_id}: {str(e)}")
        return {"success": False, "error": "An unexpected error occurred"}


def publish_ride_request(ride_id, rider_id, pickup_lng, pickup_lat, destination_lng, destination_lat):
    """
    Publish a ride request to Redis Stream for future analytics.
    
    NOTE: This stream is NOT used for driver notification — drivers are
    notified in real-time via WebSockets (channel_layer.group_send).
    This serves as a write-only log for future use cases such as:
      - Ride request history & audit trail
      - Demand heatmaps & surge pricing analytics
      - Request pattern analysis
    
    Args:
        ride_id: Trip/ride ID
        rider_id: Rider's user ID
        pickup_lng: Pickup longitude
        pickup_lat: Pickup latitude
        destination_lng: Destination longitude
        destination_lat: Destination latitude
    
    Returns:
        dict: Status and message
    """
    if redis_client is None:
        logger.error("Redis client not available for publishing ride request")
        return {"success": False, "error": "Redis connection unavailable"}

    try:
        message_id = redis_client.xadd('ride_requests', {
            'ride_id': str(ride_id),
            'rider_id': str(rider_id),
            'pickup_lng': str(pickup_lng),
            'pickup_lat': str(pickup_lat),
            'destination_lng': str(destination_lng),
            'destination_lat': str(destination_lat),
            'status': 'pending',
        }, maxlen=50000)

        logger.info(f"Ride request {ride_id} published to stream: {message_id}")
        return {"success": True, "message_id": message_id}

    except redis.RedisError as e:
        logger.error(f"Redis error publishing ride request {ride_id}: {str(e)}")
        return {"success": False, "error": "Failed to publish ride request"}
    except Exception as e:
        logger.error(f"Unexpected error publishing ride request {ride_id}: {str(e)}")
        return {"success": False, "error": "An unexpected error occurred"}


def add_rider_ping(rider_id, lng, lat):
    """
    Log a rider's presence to measure demand for dynamic pricing.
    """
    if redis_client is None:
        return False
    try:
        is_valid, _ = _validate_coordinates(lng, lat)
        if not is_valid: return False
        
        member = f'rider:{rider_id}'
        # Add to geo index for spacial querying
        redis_client.geoadd('riders:geo', [lng, lat, member])
        # Set a short TTL (3 minutes) to track freshness
        redis_client.setex(f'active_rider:{rider_id}', 180, '1')
        return True
    except Exception as e:
        logger.error(f"Failed to add rider ping for {rider_id}: {e}")
        return False


def count_nearby_active_riders(lng, lat, radius=3000):
    """
    Count how many unique riders exist within the radius who have 
    pinged the system in the last 3 minutes.
    """
    if redis_client is None:
        return 0
    try:
        is_valid, _ = _validate_coordinates(lng, lat)
        if not is_valid: return 0
        
        riders = redis_client.geosearch(
            'riders:geo',
            longitude=lng,
            latitude=lat,
            radius=radius,
            unit='m'
        )
        if not riders:
            return 0
            
        pipeline = redis_client.pipeline()
        for rider in riders:
            # rider is formatted as "rider:<rider_id>"
            rider_id = rider.split(':')[1]
            pipeline.exists(f'active_rider:{rider_id}')
        
        results = pipeline.execute()
        return sum(results)
    except Exception as e:
        logger.error(f"Failed to count nearby riders: {e}")
        return 0

def get_all_active_drivers():
    """
    Retrieve all driver locations from the geospatial index.
    Returns:
        list of dicts: [{'driver_id': '1', 'vehicle_type': 'car', 'lat': 17.3, 'lng': 78.4}, ...]
    """
    if redis_client is None:
        return []
    try:
        # Walk every per-vehicle-type index. This used to read the single
        # legacy `drivers:geo` key, which nothing writes to any more — the
        # ops live map would have shown an empty city.
        result = []
        for key in redis_client.scan_iter(match=f'{GEO_KEY_PREFIX}*', count=100):
            members = redis_client.zrange(key, 0, -1)
            if not members:
                continue

            positions = redis_client.geopos(key, *members)

            for member, pos in zip(members, positions, strict=False):
                if pos:
                    # member format: driver:{driver_id}:{vehicle_type}
                    parts = member.split(':')
                    if len(parts) >= 3:
                        result.append({
                            'driver_id': parts[1],
                            'vehicle_type': parts[2],
                            'lng': pos[0],
                            'lat': pos[1],
                        })
        return result
    except Exception as e:
        logger.error(f"Failed to get active drivers: {e}")
        return []

def get_all_active_riders():
    """
    Retrieve all active rider locations.
    """
    if redis_client is None:
        return []
    try:
        members = redis_client.zrange('riders:geo', 0, -1)
        if not members:
            return []
            
        positions = redis_client.geopos('riders:geo', *members)
        
        # Check active status
        pipeline = redis_client.pipeline()
        for member in members:
            # member format: rider:{rider_id}
            parts = member.split(':')
            if len(parts) >= 2:
                rider_id = parts[1]
                pipeline.exists(f'active_rider:{rider_id}')
            else:
                pipeline.exists('invalid_key')
                
        active_statuses = pipeline.execute()
        
        result = []
        for member, pos, is_active in zip(members, positions, active_statuses, strict=False):
            if pos and is_active:
                parts = member.split(':')
                if len(parts) >= 2:
                    result.append({
                        'rider_id': parts[1],
                        'lng': pos[0],
                        'lat': pos[1]
                    })
        return result
    except Exception as e:
        logger.error(f"Failed to get active riders: {e}")
        return []

# --- Maps Proxy Caching & Rate Limiting ---

def check_maps_rate_limit(user_id, limit=100, period=3600):
    """
    Check if a user has exceeded their Maps API proxy rate limit.
    Uses a simple fixed-window counter.
    """
    if redis_client is None:
        return True  # Fail open if Redis is down
    try:
        key = f"maps_rate_limit:{user_id}"
        count = redis_client.incr(key)
        if count == 1:
            redis_client.expire(key, period)
        if count > limit:
            logger.warning(f"User {user_id} exceeded maps rate limit")
            return False
        return True
    except Exception as e:
        logger.error(f"Failed to check maps rate limit for {user_id}: {e}")
        return True

import json

def get_cached_map_data(key):
    """
    Retrieve cached map data from Redis.
    """
    if redis_client is None:
        return None
    try:
        data = redis_client.get(key)
        if data:
            return json.loads(data)
        return None
    except Exception as e:
        logger.error(f"Failed to get cached map data for {key}: {e}")
        return None

def cache_map_data(key, data, ttl_seconds):
    """
    Cache map data in Redis.
    """
    if redis_client is None:
        return False
    try:
        redis_client.setex(key, ttl_seconds, json.dumps(data))
        return True
    except Exception as e:
        logger.error(f"Failed to cache map data for {key}: {e}")
        return False
