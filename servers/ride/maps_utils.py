"""Server-side adapters for the Maps provider.

The API key never leaves this process: every client reaches these through
`/ride/maps/*`, which is why the key is read from settings here and nowhere
near a bundle or a browser.

WHAT CHANGED AND WHY
--------------------

1. THE COUNTRY IS NO LONGER HARDCODED.

   `google_places_autocomplete` sent `components=country:in` on every request,
   so on a platform that carries a country registry, zone hierarchy and
   per-market currency, destination search silently returned Indian results
   only. A rider in any other market searched their own city and got nothing,
   with no error to explain it.

   The country now comes from the SERVICE MARKET: the zone that contains the
   pickup point, whose `country` is the same ISO-2 code `base/countries.py`
   and the rate cards already use. One source of truth, not a second one
   declared here. Outside every zone the restriction is simply omitted --
   an unsupported market gets unfiltered results rather than another
   market's.

2. PLACES API (NEW) IS SUPPORTED.

   The legacy `maps/api/place/{autocomplete,details}/json` endpoints cannot be
   enabled on Google Cloud projects created from October 2026, so a new
   project cannot run the old code at all. Both are implemented and selected
   by `GOOGLE_PLACES_API_VERSION` ('new' by default, 'legacy' for a project
   that still has it enabled).

   The NEW responses are normalised back into the legacy shape this codebase
   and both apps already consume -- `predictions[].{description, place_id}`
   and a details `result.geometry.location.{lat,lng}`. The proxy contract is
   unchanged, so no client has to be redeployed to benefit, and a project
   with legacy access can switch back with one variable.

   Geocoding, Directions and Distance Matrix are NOT part of the Places
   migration. They are separate APIs, still current, and are untouched.

3. FAILURES ARE DISTINGUISHABLE.

   `None` meant "no key", "quota exhausted", "network timeout" and "the
   provider refused the request" indiscriminately, which is how a deployment
   with no key configured looked identical to an outage. Each adapter now
   records why through `MapsFailure`, so a caller can tell an unconfigured
   deployment from a billing problem, while still returning `None` so every
   existing call site keeps working unchanged.
"""

from __future__ import annotations

import logging
import threading

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

# Read lazily. This used to be a module-level constant, so a key supplied
# after import -- which is every test that overrides settings, and any process
# that configures settings late -- was ignored, and the adapter reported "not
# set" while the key sat right there in settings.
def _api_key() -> str:
    return (getattr(settings, 'GOOGLE_MAPS_API_KEY', '') or '').strip()


def maps_configured() -> bool:
    """Whether this deployment can talk to the provider at all."""
    return bool(_api_key())


def _places_api_version() -> str:
    """'new' (default) or 'legacy'.

    Defaults to NEW because a Google Cloud project created from October 2026
    cannot enable Places API (Legacy); defaulting to the one a fresh project
    cannot use would make this fail by default.
    """
    value = (getattr(settings, 'GOOGLE_PLACES_API_VERSION', '') or '').strip().lower()
    return value if value in ('new', 'legacy') else 'new'


# --------------------------------------------------------------------------
# failure reporting
# --------------------------------------------------------------------------

NOT_CONFIGURED = 'not_configured'
QUOTA_EXCEEDED = 'quota_exceeded'
PERMISSION_DENIED = 'permission_denied'
UPSTREAM_ERROR = 'upstream_error'
INVALID_REQUEST = 'invalid_request'

# Per-thread so a concurrent request cannot read another's failure.
_state = threading.local()


class MapsFailure:
    """Why the last adapter call in this thread returned None."""

    @staticmethod
    def set(reason: str, detail: str = '') -> None:
        _state.reason = reason
        _state.detail = detail

    @staticmethod
    def clear() -> None:
        _state.reason = None
        _state.detail = ''

    @staticmethod
    def reason() -> str | None:
        return getattr(_state, 'reason', None)

    @staticmethod
    def detail() -> str:
        return getattr(_state, 'detail', '')


def _fail(reason: str, detail: str = ''):
    MapsFailure.set(reason, detail)
    # Never log the key, and never log the caller's full search text at error
    # level -- a search string is a place somebody is going.
    logger.error('Maps adapter failed: %s (%s)', reason, detail[:200])
    return None


def _classify_http(exc: requests.RequestException) -> tuple[str, str]:
    response = getattr(exc, 'response', None)
    if response is None:
        return UPSTREAM_ERROR, str(exc)[:200]
    if response.status_code == 429:
        return QUOTA_EXCEEDED, 'provider returned HTTP 429'
    if response.status_code in (401, 403):
        return PERMISSION_DENIED, (
            f'provider returned HTTP {response.status_code}: the key is '
            f'rejected, or this API is not enabled on the project'
        )
    return UPSTREAM_ERROR, f'provider returned HTTP {response.status_code}'


# Legacy status strings that mean something more specific than "error".
_LEGACY_STATUS = {
    'OVER_QUERY_LIMIT': QUOTA_EXCEEDED,
    'REQUEST_DENIED': PERMISSION_DENIED,
    'INVALID_REQUEST': INVALID_REQUEST,
}


def _check_legacy_payload(payload):
    """Legacy endpoints answer HTTP 200 with a status field; honour it."""
    status_value = (payload or {}).get('status')
    if status_value in ('OK', 'ZERO_RESULTS', None):
        return payload
    reason = _LEGACY_STATUS.get(status_value, UPSTREAM_ERROR)
    return _fail(reason, f'{status_value}: {(payload or {}).get("error_message", "")}')


# --------------------------------------------------------------------------
# market resolution
# --------------------------------------------------------------------------

def _parse_location(location) -> tuple[float, float] | None:
    """Accept "lat,lng" or (lat, lng); return None if it is neither."""
    if location is None:
        return None
    try:
        if isinstance(location, str):
            lat_text, lng_text = location.split(',')[:2]
            return float(lat_text), float(lng_text)
        lat, lng = location
        return float(lat), float(lng)
    except (TypeError, ValueError):
        return None


def country_for_location(location) -> str | None:
    """ISO-2 country of the service market containing this point.

    Returns None outside every zone, which is how an unsupported market is
    represented -- deliberately not a default of 'IN'. Falling back to a
    country would reintroduce the bug this replaces, just less visibly.
    """
    point = _parse_location(location)
    if point is None:
        return None
    try:
        from servers.pricing.services import find_zone_for_point

        zone = find_zone_for_point(point[0], point[1])
    except Exception:
        # Market lookup must never take destination search down with it.
        logger.warning('zone lookup failed while resolving country', exc_info=True)
        return None
    if zone is None:
        return None
    code = (getattr(zone, 'country', '') or '').strip().upper()
    return code or None


def _resolve_country(country, location) -> str | None:
    if country:
        code = str(country).strip().upper()
        return code or None
    return country_for_location(location)


# --------------------------------------------------------------------------
# autocomplete
# --------------------------------------------------------------------------

def google_places_autocomplete(search_text, location=None, radius=None,
                               country=None, session_token=None):
    """Place predictions for `search_text`, biased to the caller's market.

    Returns the legacy shape -- {'predictions': [{description, place_id}, ...]}
    -- whichever provider API produced it, because that is what the proxy,
    the rider app and the website already read.
    """
    MapsFailure.clear()
    if not maps_configured():
        return _fail(NOT_CONFIGURED, 'GOOGLE_MAPS_API_KEY is not set')
    if not search_text:
        return _fail(INVALID_REQUEST, 'empty search text')

    code = _resolve_country(country, location)
    if _places_api_version() == 'legacy':
        return _autocomplete_legacy(search_text, location, radius, code, session_token)
    return _autocomplete_new(search_text, location, radius, code, session_token)


def _autocomplete_legacy(search_text, location, radius, country, session_token):
    params = {
        'input': search_text,
        'key': _api_key(),
    }
    # Only restrict when we actually know the market. The previous
    # unconditional 'country:in' is the defect this replaces.
    if country:
        params['components'] = f'country:{country.lower()}'
    if location:
        params['location'] = location if isinstance(location, str) else \
            f'{location[0]},{location[1]}'
    if radius:
        params['radius'] = radius
    if session_token:
        params['sessiontoken'] = session_token

    try:
        response = requests.get(
            'https://maps.googleapis.com/maps/api/place/autocomplete/json',
            params=params, timeout=5,
        )
        response.raise_for_status()
        return _check_legacy_payload(response.json())
    except requests.RequestException as exc:
        return _fail(*_classify_http(exc))
    except ValueError as exc:
        return _fail(UPSTREAM_ERROR, f'unparseable response: {exc}')


def _autocomplete_new(search_text, location, radius, country, session_token):
    """Places API (New): POST places:autocomplete.

    The field mask is pinned to the two fields the clients use. Google bills
    by the fields requested, so asking for everything would cost more for a
    response we would throw away.
    """
    body = {'input': search_text}
    if country:
        body['includedRegionCodes'] = [country]
    point = _parse_location(location)
    if point:
        body['locationBias'] = {
            'circle': {
                'center': {'latitude': point[0], 'longitude': point[1]},
                'radius': float(radius) if radius else 50000.0,
            }
        }
    if session_token:
        body['sessionToken'] = session_token

    try:
        response = requests.post(
            'https://places.googleapis.com/v1/places:autocomplete',
            json=body,
            headers={
                'X-Goog-Api-Key': _api_key(),
                'X-Goog-FieldMask': (
                    'suggestions.placePrediction.placeId,'
                    'suggestions.placePrediction.text'
                ),
                'Content-Type': 'application/json',
            },
            timeout=5,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        return _fail(*_classify_http(exc))
    except ValueError as exc:
        return _fail(UPSTREAM_ERROR, f'unparseable response: {exc}')

    predictions = []
    for suggestion in (payload or {}).get('suggestions') or []:
        prediction = (suggestion or {}).get('placePrediction') or {}
        place_id = prediction.get('placeId')
        description = ((prediction.get('text') or {}).get('text') or '').strip()
        if place_id and description:
            predictions.append({
                'description': description,
                'place_id': place_id,
            })

    # ZERO_RESULTS rather than OK when empty, mirroring the legacy vocabulary
    # the callers were written against.
    return {
        'predictions': predictions,
        'status': 'OK' if predictions else 'ZERO_RESULTS',
    }


# --------------------------------------------------------------------------
# place details
# --------------------------------------------------------------------------

def google_place_details(place_id, session_token=None):
    """Resolve a place_id to coordinates and a formatted address.

    Always returns the legacy shape, plus a convenience top-level `location`
    that the website reads.
    """
    MapsFailure.clear()
    if not maps_configured():
        return _fail(NOT_CONFIGURED, 'GOOGLE_MAPS_API_KEY is not set')
    if not place_id:
        return _fail(INVALID_REQUEST, 'missing place_id')

    if _places_api_version() == 'legacy':
        return _place_details_legacy(place_id, session_token)
    return _place_details_new(place_id, session_token)


def _place_details_legacy(place_id, session_token):
    params = {
        'place_id': place_id,
        'key': _api_key(),
        # Pinned: Google bills Place Details per requested field group.
        'fields': 'name,geometry,formatted_address,place_id',
    }
    if session_token:
        params['sessiontoken'] = session_token

    try:
        response = requests.get(
            'https://maps.googleapis.com/maps/api/place/details/json',
            params=params, timeout=5,
        )
        response.raise_for_status()
        payload = _check_legacy_payload(response.json())
    except requests.RequestException as exc:
        return _fail(*_classify_http(exc))
    except ValueError as exc:
        return _fail(UPSTREAM_ERROR, f'unparseable response: {exc}')

    if payload is None:
        return None
    # Add the flat `location` the website reads, without disturbing `result`.
    location = (((payload.get('result') or {}).get('geometry') or {})
                .get('location') or {})
    if 'lat' in location and 'lng' in location:
        payload.setdefault('location', {'lat': location['lat'],
                                        'lng': location['lng']})
    return payload


def _place_details_new(place_id, session_token):
    # The New API addresses a place as `places/<id>`; ids from New autocomplete
    # come bare, and an id that already carries the prefix must not get a
    # second one.
    name = place_id if str(place_id).startswith('places/') else f'places/{place_id}'
    params = {}
    if session_token:
        params['sessionToken'] = session_token

    try:
        response = requests.get(
            f'https://places.googleapis.com/v1/{name}',
            params=params,
            headers={
                'X-Goog-Api-Key': _api_key(),
                'X-Goog-FieldMask': 'id,displayName,formattedAddress,location',
            },
            timeout=5,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        return _fail(*_classify_http(exc))
    except ValueError as exc:
        return _fail(UPSTREAM_ERROR, f'unparseable response: {exc}')

    location = (payload or {}).get('location') or {}
    lat = location.get('latitude')
    lng = location.get('longitude')
    if lat is None or lng is None:
        return _fail(UPSTREAM_ERROR, 'place details carried no coordinates')

    display = ((payload.get('displayName') or {}).get('text') or '').strip()
    return {
        'status': 'OK',
        'location': {'lat': lat, 'lng': lng},
        'result': {
            'place_id': (payload.get('id') or place_id),
            'name': display,
            'formatted_address': payload.get('formattedAddress') or display,
            'geometry': {'location': {'lat': lat, 'lng': lng}},
        },
    }


# --------------------------------------------------------------------------
# geocoding and directions -- separate APIs, not part of the Places migration
# --------------------------------------------------------------------------

def google_geocode(address=None, latlng=None, country=None):
    """Forward or reverse geocoding.

    `country` biases a forward lookup to the caller's market; a reverse
    lookup needs no bias because the coordinates already name the place.
    """
    MapsFailure.clear()
    if not maps_configured():
        return _fail(NOT_CONFIGURED, 'GOOGLE_MAPS_API_KEY is not set')

    params = {'key': _api_key()}
    if address:
        params['address'] = address
        code = _resolve_country(country, None)
        if code:
            params['components'] = f'country:{code.lower()}'
    elif latlng:
        params['latlng'] = latlng
    else:
        return _fail(INVALID_REQUEST, 'neither address nor latlng given')

    try:
        response = requests.get(
            'https://maps.googleapis.com/maps/api/geocode/json',
            params=params, timeout=5,
        )
        response.raise_for_status()
        return _check_legacy_payload(response.json())
    except requests.RequestException as exc:
        return _fail(*_classify_http(exc))
    except ValueError as exc:
        return _fail(UPSTREAM_ERROR, f'unparseable response: {exc}')


def google_directions(origin_lat, origin_lng, dest_lat, dest_lng):
    MapsFailure.clear()
    if not maps_configured():
        return _fail(NOT_CONFIGURED, 'GOOGLE_MAPS_API_KEY is not set')

    try:
        response = requests.get(
            'https://maps.googleapis.com/maps/api/directions/json',
            params={
                'origin': f'{origin_lat},{origin_lng}',
                'destination': f'{dest_lat},{dest_lng}',
                'key': _api_key(),
                'alternatives': 'true',
            },
            timeout=5,
        )
        response.raise_for_status()
        return _check_legacy_payload(response.json())
    except requests.RequestException as exc:
        return _fail(*_classify_http(exc))
    except ValueError as exc:
        return _fail(UPSTREAM_ERROR, f'unparseable response: {exc}')
