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
