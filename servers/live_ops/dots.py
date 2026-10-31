"""The shape of what leaves the server: dots, trips and alert pins.

BUILT FROM A WHITELIST, NEVER FILTERED FROM A BLACKLIST
-------------------------------------------------------
The producer side (`DriverLocationConsumer`) puts a driver's name, phone,
plate and rating on every frame it publishes. Nothing here copies a frame and
deletes the bad keys: each function names the keys it emits and builds the
dict from scratch, so a field somebody adds to a frame tomorrow cannot reach
a browser. `DOT_KEYS`, `TRIP_KEYS` and `ALERT_KEYS` are the contract; the
tests check every payload against them.
"""

from datetime import datetime, timezone

from .config import DOT_COORD_DECIMALS, TRIP_COORD_DECIMALS

#: online / on_trip / offline. The producer says 'busy' for on_trip.
ON_TRIP, ONLINE, OFFLINE = 'on_trip', 'online', 'offline'
_STATUS = {'busy': ON_TRIP, 'on_trip': ON_TRIP, 'online': ONLINE, 'offline': OFFLINE}

DOT_KEYS = frozenset({
    'driver_id', 'status', 'vehicle_class', 'lat', 'lng', 'heading',
    'updated_at', 'trip_id', 'city',
})
TRIP_KEYS = frozenset({
    'id', 'state', 'pickup_lat', 'pickup_lng', 'drop_lat', 'drop_lng',
    'vehicle_class', 'age_s', 'city', 'stale',
})
ALERT_KEYS = frozenset({
    'id', 'kind', 'severity', 'title', 'city', 'created_at', 'is_sos', 'lat', 'lng',
})
#: Reaching a browser with any of these in a payload is a bug.
FORBIDDEN_KEYS = frozenset({
    'name', 'full_name', 'driver_name', 'rider_name', 'phone', 'phone_number',
    'email', 'plate', 'vehicle_number', 'vehicle_model', 'ratings', 'rating',
    'rider_id', 'user_id', 'pickup_address', 'destination_address', 'address',
    'otp',
})


def status_of(value):
    return _STATUS.get(str(value or '').lower(), ONLINE)


def round_to(value, places):
    try:
        return round(float(value), places)
    except (TypeError, ValueError):
        return None


def trip_id_of(value):
    """The producer sends an int, None, or 'unknown' (Redis could not say)."""
    try:
        return int(value) if value not in (None, '', 'unknown') else None
    except (TypeError, ValueError):
        return None


def heading_of(value):
    try:
        degrees = float(value)
    except (TypeError, ValueError):
        return None
    return round(degrees % 360, 1) if degrees == degrees else None


def iso(moment):
    if moment is None:
        return None
    if not isinstance(moment, datetime):
        moment = datetime.fromtimestamp(float(moment), tz=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def build_dot(*, driver_id, status, lat, lng, vehicle_class=None, heading=None,
              updated_at=None, trip_id=None, city=None, exact=True):
    """A driver dot. `exact` False (no `pii.view`) is the masking layer's
    two decimals, applied here because a socket frame never passes the
    renderer that does it for REST."""
    places = DOT_COORD_DECIMALS if exact else 2
    return {
        'driver_id': int(driver_id),
        'status': status_of(status),
        'vehicle_class': (str(vehicle_class).lower() if vehicle_class else None),
        'lat': round_to(lat, places),
        'lng': round_to(lng, places),
        'heading': heading_of(heading),
        'updated_at': iso(updated_at),
        'trip_id': trip_id_of(trip_id),
        'city': city,
    }


def build_trip(*, trip_id, state, pickup_lat, pickup_lng, drop_lat, drop_lng,
               vehicle_class, age_s, city, stale, exact=True):
    places = TRIP_COORD_DECIMALS if exact else 2
    return {
        'id': int(trip_id),
        'state': state,
        'pickup_lat': round_to(pickup_lat, places),
        'pickup_lng': round_to(pickup_lng, places),
        'drop_lat': round_to(drop_lat, places),
        'drop_lng': round_to(drop_lng, places),
        'vehicle_class': (str(vehicle_class).lower() if vehicle_class else None),
        'age_s': max(0, int(age_s)),
        'city': city,
        'stale': bool(stale),
    }


def build_alert(*, alert_id, kind, severity, title, city, created_at, is_sos,
                lat=None, lng=None, exact=True):
    places = TRIP_COORD_DECIMALS if exact else 2
    return {
        'id': int(alert_id),
        'kind': kind,
        'severity': severity,
        'title': title,
        'city': city,
        'created_at': iso(created_at),
        'is_sos': bool(is_sos),
        'lat': round_to(lat, places) if lat is not None else None,
        'lng': round_to(lng, places) if lng is not None else None,
    }
