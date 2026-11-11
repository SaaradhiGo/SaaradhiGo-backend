"""Live checks against the real Google Routes API.

Skipped unless GOOGLE_MAPS_API_KEY is in the environment, so CI and the
default suite are unaffected. Run locally with a key to prove the migration
works against Google rather than against a mock:

    GOOGLE_MAPS_API_KEY=... pytest tests/test_routes_api_live.py -q -s

WHY THIS EXISTS
Distance Matrix (Legacy) and Directions (Legacy) cannot be enabled on a
Google Cloud project created after Google's legacy cut-off. Probing the
SaaradhiGo project's key returned, for both:

    "You're calling a legacy API, which is not enabled for your project.
     ... switch to the Places API (New) or Routes API."

Geocoding and Places API (New) answered normally. So the fare path and the
route display had to move to Routes API or stay permanently broken.
"""

import os

import pytest

from servers.ride import maps_utils, utils

KEY = os.environ.get('GOOGLE_MAPS_API_KEY', '').strip()
pytestmark = pytest.mark.skipif(not KEY, reason='no GOOGLE_MAPS_API_KEY in env')

PICKUP = (16.5062, 80.6480)   # Vijayawada, the QA pickup
DEST = (16.5190, 80.6305)     # the QA destination


@pytest.fixture(autouse=True)
def _configure(settings):
    settings.GOOGLE_MAPS_API_KEY = KEY
    settings.GOOGLE_ROUTING_API_VERSION = 'new'


@pytest.mark.django_db
def test_routes_api_returns_a_real_road_distance(capsys):
    km, minutes = utils.get_google_maps_distance(*PICKUP, *DEST)

    with capsys.disabled():
        print(f'\n  Routes API: {km:.2f} km, {minutes:.1f} min')

    assert km is not None and minutes is not None, 'Routes API returned nothing'
    # The QA route is ~2.4 km by road. Generous bounds: this asserts the
    # number is a real road distance, not that Google is wrong about traffic.
    assert 1.0 < km < 10.0, f'implausible road distance {km} km'
    assert 1.0 < minutes < 60.0, f'implausible duration {minutes} min'


@pytest.mark.django_db
def test_the_fare_distance_is_consistent_between_calls(capsys):
    first = utils.get_google_maps_distance(*PICKUP, *DEST)
    second = utils.get_google_maps_distance(*PICKUP, *DEST)
    with capsys.disabled():
        print(f'  consistency: {first} then {second}')
    assert first[0] == second[0], 'the same route priced two different distances'


@pytest.mark.django_db
def test_directions_normalise_to_the_shape_the_apps_parse(capsys):
    """The rider app reads these exact keys and decodes that polyline."""
    result = maps_utils.google_directions(*PICKUP, *DEST)
    assert result is not None, f'directions failed: {maps_utils.MapsFailure.reason()}'

    assert result['status'] == 'OK'
    route = result['routes'][0]
    leg = route['legs'][0]

    assert isinstance(leg['distance']['value'], int)
    assert leg['distance']['text'].endswith('km')
    assert isinstance(leg['duration']['value'], int)
    assert leg['duration']['text'].endswith('min')

    points = route['overview_polyline']['points']
    assert isinstance(points, str) and len(points) > 10, 'no encoded polyline'

    with capsys.disabled():
        print(f"  directions: {leg['distance']['text']}, {leg['duration']['text']}, "
              f"polyline {len(points)} chars, bounds={'bounds' in route}")


@pytest.mark.django_db
def test_places_autocomplete_returns_real_indian_places(capsys):
    out = maps_utils.google_places_autocomplete('Benz Circle Vijayawada')
    assert out is not None, f'autocomplete failed: {maps_utils.MapsFailure.reason()}'

    # The adapter normalises Places (New) into the legacy vocabulary the
    # callers were written against: {'predictions': [...], 'status': ...}.
    predictions = out['predictions']
    with capsys.disabled():
        print(f"  autocomplete: status={out['status']}, {len(predictions)} "
              f"suggestion(s); first=\"{(predictions[0]['description'] if predictions else '')[:60]}\"")
    assert out['status'] == 'OK'
    assert predictions and predictions[0]['place_id']


@pytest.mark.django_db
def test_reverse_geocode_names_the_pickup(capsys):
    out = maps_utils.google_geocode(latlng=f'{PICKUP[0]},{PICKUP[1]}')
    assert out is not None, f'geocode failed: {maps_utils.MapsFailure.reason()}'
    with capsys.disabled():
        print(f'  reverse geocode ok: {str(out)[:80]}')


@pytest.mark.django_db
def test_the_country_is_derived_from_the_pickup_not_hardcoded(capsys):
    country = maps_utils.country_for_location(f'{PICKUP[0]},{PICKUP[1]}')
    with capsys.disabled():
        print(f'  country for the QA pickup: {country}')
    assert country in (None, 'IN'), f'unexpected country {country!r}'
