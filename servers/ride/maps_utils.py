import logging
import requests
from django.conf import settings

logger = logging.getLogger(__name__)

GOOGLE_MAPS_API_KEY = getattr(settings, 'GOOGLE_MAPS_API_KEY', '')

def google_places_autocomplete(search_text, location=None, radius=None):
    """
    Wrapper for Google Places Autocomplete API.
    """
    if not GOOGLE_MAPS_API_KEY:
        logger.error("GOOGLE_MAPS_API_KEY is not set.")
        return None

    url = "https://maps.googleapis.com/maps/api/place/autocomplete/json"
    params = {
        'input': search_text,
        'key': GOOGLE_MAPS_API_KEY,
        'components': 'country:in', # Default to India for SaaradhiGo
    }
    
    if location:
        params['location'] = location # "lat,lng"
    if radius:
        params['radius'] = radius

    try:
        response = requests.get(url, params=params, timeout=5)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error(f"Google Places API error: {str(e)}")
        return None

def google_place_details(place_id, session_token=None):
    """Wrapper for Google Place Details API.

    Resolves a place_id from autocomplete into coordinates + a formatted
    address. Exists so the mobile app never has to hold the Maps key: it
    used to call this endpoint directly from the device (and on web, via
    the public corsproxy.io relay, which saw every rider's search).

    `fields` is pinned to the minimum we need — Google bills Place Details
    per requested field group.
    """
    if not GOOGLE_MAPS_API_KEY:
        logger.error("GOOGLE_MAPS_API_KEY is not set.")
        return None
    if not place_id:
        return None

    url = "https://maps.googleapis.com/maps/api/place/details/json"
    params = {
        'place_id': place_id,
        'key': GOOGLE_MAPS_API_KEY,
        'fields': 'name,geometry,formatted_address,place_id',
    }
    # Passing the same session token used for autocomplete makes Google
    # bill the whole lookup as one session rather than per keystroke.
    if session_token:
        params['sessiontoken'] = session_token

    try:
        response = requests.get(url, params=params, timeout=5)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error(f"Google Place Details API error: {str(e)}")
        return None


def google_geocode(address=None, latlng=None):
    """
    Wrapper for Google Geocoding API (Forward or Reverse).
    """
    if not GOOGLE_MAPS_API_KEY:
        logger.error("GOOGLE_MAPS_API_KEY is not set.")
        return None

    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {'key': GOOGLE_MAPS_API_KEY}
    
    if address:
        params['address'] = address
    elif latlng:
        params['latlng'] = latlng
    else:
        return None

    try:
        response = requests.get(url, params=params, timeout=5)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error(f"Google Geocoding API error: {str(e)}")
        return None

def google_directions(origin_lat, origin_lng, dest_lat, dest_lng):
    """
    Wrapper for Google Directions API.
    """
    if not GOOGLE_MAPS_API_KEY:
        logger.error("GOOGLE_MAPS_API_KEY is not set.")
        return None

    url = "https://maps.googleapis.com/maps/api/directions/json"
    params = {
        'origin': f"{origin_lat},{origin_lng}",
        'destination': f"{dest_lat},{dest_lng}",
        'key': GOOGLE_MAPS_API_KEY,
        'alternatives': 'true'
    }

    try:
        response = requests.get(url, params=params, timeout=5)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error(f"Google Directions API error: {str(e)}")
        return None
