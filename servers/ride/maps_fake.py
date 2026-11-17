"""The fake maps adapter (17 Nov, PROD round 2): every Maps call answered here, with no network.

Load, e2e and CI runs use it (settings.MAPS_ADAPTER == 'fake'), because the 16 Nov pilot load on QA
made about 1,400 real Routes calls. The answers are deterministic and keep the legacy shapes that
maps_utils returns, so the fare, ETA and booking code paths run unchanged:

  * autocomplete: three places around the search point (or Hyderabad), named after the search text;
  * place details: the coordinates encoded in the fake place id;
  * geocode: one result at the given point, locality "Fake locality";
  * directions: the straight line x 1.3 road factor, at 24 km/h, as one leg.

Nothing here is billed or metered, and nothing reaches Google.
"""

import math

ROAD_FACTOR = 1.3
SPEED_KMH = 24.0
_HYDERABAD = (17.385, 78.4867)
_OFFSETS = ((0.010, 0.012), (-0.014, 0.020), (0.022, -0.016))


def _point(location):
    from servers.ride.maps_utils import _parse_location

    return _parse_location(location) or _HYDERABAD


def _place_id(lat, lng):
    return f'fake:{lat:.6f},{lng:.6f}'


def _from_place_id(place_id):
    text = str(place_id or '').removeprefix('places/')
    if not text.startswith('fake:'):
        return None
    try:
        lat, lng = (float(x) for x in text[5:].split(','))
    except ValueError:
        return None
    return lat, lng


def autocomplete(search_text, location=None, bounds=None):
    if bounds:
        south, west, north, east = bounds
        lat, lng = (south + north) / 2, (west + east) / 2
    else:
        lat, lng = _point(location)
    name = (search_text or '').strip()[:60] or 'Place'
    predictions = []
    for i, (dlat, dlng) in enumerate(_OFFSETS, start=1):
        plat, plng = lat + dlat, lng + dlng
        predictions.append({
            'description': f'{name} {i}, Fake area',
            'place_id': _place_id(plat, plng),
            'structured_formatting': {'main_text': f'{name} {i}', 'secondary_text': 'Fake area'},
        })
    return {'predictions': predictions, 'status': 'OK'}


def place_details(place_id):
    point = _from_place_id(place_id) or _HYDERABAD
    lat, lng = point
    return {
        'status': 'OK',
        'location': {'lat': lat, 'lng': lng},
        'result': {
            'place_id': str(place_id),
            'name': 'Fake place',
            'formatted_address': f'Fake place, {lat:.4f}, {lng:.4f}',
            'geometry': {'location': {'lat': lat, 'lng': lng}},
        },
    }


def geocode(address=None, latlng=None):
    lat, lng = _point(latlng) if latlng else _HYDERABAD
    return {
        'status': 'OK',
        'results': [{
            'formatted_address': f'{address or "Fake address"}, Fake locality',
            'geometry': {'location': {'lat': lat, 'lng': lng}},
            'address_components': [{'long_name': 'Fake locality', 'short_name': 'Fake', 'types': ['locality']}],
            'place_id': _place_id(lat, lng),
        }],
    }


def _haversine_m(a_lat, a_lng, b_lat, b_lng):
    r = 6371000.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lng - a_lng)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def directions(origin_lat, origin_lng, dest_lat, dest_lng):
    metres = _haversine_m(float(origin_lat), float(origin_lng), float(dest_lat), float(dest_lng)) * ROAD_FACTOR
    seconds = metres / (SPEED_KMH * 1000 / 3600)
    leg = {
        'distance': {'value': int(metres), 'text': f'{metres / 1000.0:.1f} km'},
        'duration': {'value': int(seconds), 'text': f'{max(1, round(seconds / 60.0))} min'},
    }
    south, north = sorted((float(origin_lat), float(dest_lat)))
    west, east = sorted((float(origin_lng), float(dest_lng)))
    return {'status': 'OK', 'routes': [{
        'legs': [leg],
        'overview_polyline': {'points': ''},
        'bounds': {'southwest': {'lat': south, 'lng': west}, 'northeast': {'lat': north, 'lng': east}},
    }]}
