"""Place suggestions stay inside the active city's service area (owner, 2026-11-06).

Typing "for" in Hyderabad must not suggest Vijayawada or Kochi. With `zone`, the
geocode proxy RESTRICTS (not biases) the provider to the zone's bounding box and
country.
"""

from unittest import mock

import pytest
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient

from servers.ride import maps_utils
from tests.test_maps_multi_country import (
    KEY, LEGACY_AUTOCOMPLETE_OK, NEW_AUTOCOMPLETE_OK, FakeResponse, _zone,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    # The maps cache is Redis, which outlives a test run locally: never read it here.
    with mock.patch('servers.ride.views.get_cached_map_data', return_value=None):
        yield


@override_settings(GOOGLE_MAPS_API_KEY=KEY, GOOGLE_PLACES_API_VERSION='new')
def test_a_zone_restricts_the_search_to_its_area():
    _zone('IN-TG-HYD-T', 'Hyderabad', 'IN', 17.385, 78.4867, delta=0.3)
    with mock.patch.object(maps_utils.requests, 'post',
                           return_value=FakeResponse(NEW_AUTOCOMPLETE_OK)) as post:
        r = APIClient().post('/api/v1/ride/maps/geocode',
                             {'input': 'for', 'zone': 'IN-TG-HYD-T',
                              'location': '17.385,78.4867'}, format='json')
    assert r.status_code == 200, r.content
    body = post.call_args.kwargs['json']
    rect = body['locationRestriction']['rectangle']
    assert rect['low'] == {'latitude': pytest.approx(17.085), 'longitude': pytest.approx(78.1867)}
    assert rect['high'] == {'latitude': pytest.approx(17.685), 'longitude': pytest.approx(78.7867)}
    assert 'locationBias' not in body
    assert body['includedRegionCodes'] == ['IN']


@override_settings(GOOGLE_MAPS_API_KEY=KEY, GOOGLE_PLACES_API_VERSION='legacy')
def test_the_legacy_api_uses_strict_bounds():
    _zone('IN-TG-HYD-T', 'Hyderabad', 'IN', 17.385, 78.4867, delta=0.3)
    with mock.patch.object(maps_utils.requests, 'get',
                           return_value=FakeResponse(LEGACY_AUTOCOMPLETE_OK)) as get:
        APIClient().post('/api/v1/ride/maps/geocode',
                         {'input': 'for', 'zone': 'IN-TG-HYD-T'}, format='json')
    params = get.call_args.kwargs['params']
    assert params['strictbounds'] == 'true' and params['components'] == 'country:in'
    assert 40_000 < params['radius'] < 50_000


@override_settings(GOOGLE_MAPS_API_KEY=KEY, GOOGLE_PLACES_API_VERSION='new')
def test_an_unknown_zone_falls_back_to_the_bias():
    with mock.patch.object(maps_utils.requests, 'post',
                           return_value=FakeResponse(NEW_AUTOCOMPLETE_OK)) as post:
        APIClient().post('/api/v1/ride/maps/geocode',
                         {'input': 'for', 'zone': 'NOPE', 'location': '17.385,78.4867'},
                         format='json')
    body = post.call_args.kwargs['json']
    assert 'locationRestriction' not in body and 'locationBias' in body


def test_zone_bounds_reads_multipolygons():
    mp = {'type': 'MultiPolygon', 'coordinates': [
        [[[78.0, 17.0], [78.5, 17.0], [78.5, 17.5], [78.0, 17.0]]],
        [[[79.0, 16.0], [79.2, 16.0], [79.2, 16.2], [79.0, 16.0]]],
    ]}
    assert maps_utils.zone_bounds(mp) == (16.0, 78.0, 17.5, 79.2)
    assert maps_utils.zone_bounds({'type': 'Polygon'}) is None
