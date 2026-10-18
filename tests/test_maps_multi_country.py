"""The maps adapters must serve every market, not just India.

THE DEFECT
----------
`google_places_autocomplete` sent `components=country:in` on every request.
On a platform with a country registry, a zone hierarchy and per-market
currency, destination search therefore returned Indian results only. A rider
anywhere else searched their own city and got nothing back, with no error to
explain why -- the request succeeded, it just could not match.

WHAT THESE TESTS PIN
--------------------
1. The country comes from the SERVICE MARKET containing the pickup point,
   for at least two countries.
2. A point in no zone -- an unsupported market -- gets NO country filter,
   rather than somebody else's.
3. Places API (New) and the legacy API both work, and both produce the same
   shape, because the proxy contract and two shipped apps depend on it.
4. Missing credentials, exhausted quota and a rejected key are
   distinguishable from each other and from a generic outage.

Every provider call is intercepted. Nothing here reaches Google, so the suite
costs nothing to run and needs no key.
"""

import json
from unittest import mock

import pytest
import requests
from django.test import override_settings

from servers.pricing.models import ServiceZone
from servers.ride import maps_utils

pytestmark = pytest.mark.django_db

KEY = 'test-key-not-a-real-credential'


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _zone(code, name, country, lat, lng, delta=0.5):
    """A square zone around (lat, lng).

    Geometry is a GeoJSON polygon, which is what ServiceZone stores and what
    find_zone_for_point reads through Shapely -- note the (lon, lat) ordering
    GeoJSON uses.
    """
    ring = [
        [lng - delta, lat - delta],
        [lng + delta, lat - delta],
        [lng + delta, lat + delta],
        [lng - delta, lat + delta],
        [lng - delta, lat - delta],
    ]
    return ServiceZone.objects.create(
        code=code, name=name, country=country, zone_type='city',
        polygon_geojson={'type': 'Polygon', 'coordinates': [ring]},
        priority=10, is_active=True,
    )


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(f'HTTP {self.status_code}')
            err.response = self
            raise err


NEW_AUTOCOMPLETE_OK = {
    'suggestions': [
        {'placePrediction': {'placeId': 'abc123',
                             'text': {'text': 'Benz Circle, Vijayawada'}}},
        {'placePrediction': {'placeId': 'def456',
                             'text': {'text': 'MG Road, Vijayawada'}}},
    ]
}

LEGACY_AUTOCOMPLETE_OK = {
    'status': 'OK',
    'predictions': [
        {'description': 'Benz Circle, Vijayawada', 'place_id': 'abc123'},
    ],
}


@pytest.fixture
def vijayawada():
    return _zone('IN-AP-VJA-T', 'Vijayawada', 'IN', 16.5062, 80.6480)


@pytest.fixture
def dubai():
    return _zone('AE-DXB-T', 'Dubai', 'AE', 25.2048, 55.2708)


# --------------------------------------------------------------------------
# 1 + 2. the country comes from the market
# --------------------------------------------------------------------------

@override_settings(GOOGLE_MAPS_API_KEY=KEY, GOOGLE_PLACES_API_VERSION='new')
@pytest.mark.parametrize('fixture_name,point,expected', [
    ('vijayawada', '16.5062,80.6480', 'IN'),
    ('dubai', '25.2048,55.2708', 'AE'),
])
def test_country_comes_from_the_zone(request, fixture_name, point, expected):
    request.getfixturevalue(fixture_name)

    with mock.patch.object(maps_utils.requests, 'post',
                           return_value=FakeResponse(NEW_AUTOCOMPLETE_OK)) as post:
        maps_utils.google_places_autocomplete('airport', location=point)

    body = post.call_args.kwargs['json']
    assert body['includedRegionCodes'] == [expected], (
        'the market containing the pickup point decides the country; this is '
        'the hardcoded country:in defect returning'
    )


@override_settings(GOOGLE_MAPS_API_KEY=KEY, GOOGLE_PLACES_API_VERSION='new')
def test_unsupported_market_gets_no_country_filter(vijayawada):
    """A point outside every zone must not inherit another market's country."""
    with mock.patch.object(maps_utils.requests, 'post',
                           return_value=FakeResponse(NEW_AUTOCOMPLETE_OK)) as post:
        # Mid-Atlantic: in no service zone.
        maps_utils.google_places_autocomplete('harbour', location='30.0,-40.0')

    body = post.call_args.kwargs['json']
    assert 'includedRegionCodes' not in body


@override_settings(GOOGLE_MAPS_API_KEY=KEY, GOOGLE_PLACES_API_VERSION='new')
def test_no_location_means_no_country_filter():
    with mock.patch.object(maps_utils.requests, 'post',
                           return_value=FakeResponse(NEW_AUTOCOMPLETE_OK)) as post:
        maps_utils.google_places_autocomplete('airport')

    assert 'includedRegionCodes' not in post.call_args.kwargs['json']


@override_settings(GOOGLE_MAPS_API_KEY=KEY, GOOGLE_PLACES_API_VERSION='new')
def test_explicit_country_overrides_the_lookup(vijayawada):
    with mock.patch.object(maps_utils.requests, 'post',
                           return_value=FakeResponse(NEW_AUTOCOMPLETE_OK)) as post:
        maps_utils.google_places_autocomplete(
            'airport', location='16.5062,80.6480', country='ae')

    assert post.call_args.kwargs['json']['includedRegionCodes'] == ['AE']


@override_settings(GOOGLE_MAPS_API_KEY=KEY, GOOGLE_PLACES_API_VERSION='legacy')
def test_legacy_path_also_uses_the_market(dubai):
    with mock.patch.object(maps_utils.requests, 'get',
                           return_value=FakeResponse(LEGACY_AUTOCOMPLETE_OK)) as get:
        maps_utils.google_places_autocomplete('mall', location='25.2048,55.2708')

    assert get.call_args.kwargs['params']['components'] == 'country:ae'


@override_settings(GOOGLE_MAPS_API_KEY=KEY, GOOGLE_PLACES_API_VERSION='legacy')
def test_legacy_omits_components_outside_every_market():
    with mock.patch.object(maps_utils.requests, 'get',
                           return_value=FakeResponse(LEGACY_AUTOCOMPLETE_OK)) as get:
        maps_utils.google_places_autocomplete('harbour', location='30.0,-40.0')

    assert 'components' not in get.call_args.kwargs['params']


def test_country_for_location_reads_the_zone(vijayawada, dubai):
    assert maps_utils.country_for_location('16.5062,80.6480') == 'IN'
    assert maps_utils.country_for_location((25.2048, 55.2708)) == 'AE'
    assert maps_utils.country_for_location('30.0,-40.0') is None
    assert maps_utils.country_for_location('nonsense') is None
    assert maps_utils.country_for_location(None) is None


# --------------------------------------------------------------------------
# 3. both provider versions produce the contract the clients read
# --------------------------------------------------------------------------

@override_settings(GOOGLE_MAPS_API_KEY=KEY, GOOGLE_PLACES_API_VERSION='new')
def test_new_autocomplete_is_normalised_to_the_legacy_shape(vijayawada):
    with mock.patch.object(maps_utils.requests, 'post',
                           return_value=FakeResponse(NEW_AUTOCOMPLETE_OK)):
        result = maps_utils.google_places_autocomplete(
            'benz', location='16.5062,80.6480')

    assert result['status'] == 'OK'
    assert result['predictions'] == [
        {'description': 'Benz Circle, Vijayawada', 'place_id': 'abc123'},
        {'description': 'MG Road, Vijayawada', 'place_id': 'def456'},
    ], 'the website and both apps read predictions[].description/place_id'


@override_settings(GOOGLE_MAPS_API_KEY=KEY, GOOGLE_PLACES_API_VERSION='new')
def test_new_autocomplete_empty_is_zero_results_not_an_error():
    with mock.patch.object(maps_utils.requests, 'post',
                           return_value=FakeResponse({'suggestions': []})):
        result = maps_utils.google_places_autocomplete('zzzz')

    assert result['status'] == 'ZERO_RESULTS'
    assert result['predictions'] == []


@override_settings(GOOGLE_MAPS_API_KEY=KEY, GOOGLE_PLACES_API_VERSION='new')
def test_new_autocomplete_requests_only_the_fields_used():
    """Google bills by field mask; asking for more costs more."""
    with mock.patch.object(maps_utils.requests, 'post',
                           return_value=FakeResponse(NEW_AUTOCOMPLETE_OK)) as post:
        maps_utils.google_places_autocomplete('benz')

    mask = post.call_args.kwargs['headers']['X-Goog-FieldMask']
    assert 'placeId' in mask and 'text' in mask
    assert '*' not in mask


@override_settings(GOOGLE_MAPS_API_KEY=KEY, GOOGLE_PLACES_API_VERSION='new')
def test_new_place_details_gives_coordinates_in_both_shapes():
    payload = {
        'id': 'abc123',
        'displayName': {'text': 'Benz Circle'},
        'formattedAddress': 'Benz Circle, Vijayawada, AP',
        'location': {'latitude': 16.5062, 'longitude': 80.6480},
    }
    with mock.patch.object(maps_utils.requests, 'get',
                           return_value=FakeResponse(payload)):
        result = maps_utils.google_place_details('abc123')

    # The website reads data.location; the apps read result.geometry.location.
    assert result['location'] == {'lat': 16.5062, 'lng': 80.6480}
    assert result['result']['geometry']['location'] == {'lat': 16.5062,
                                                        'lng': 80.6480}
    assert result['result']['formatted_address'] == 'Benz Circle, Vijayawada, AP'


@override_settings(GOOGLE_MAPS_API_KEY=KEY, GOOGLE_PLACES_API_VERSION='new')
def test_new_place_details_does_not_double_prefix_the_id():
    payload = {'id': 'abc', 'displayName': {'text': 'X'},
               'location': {'latitude': 1.0, 'longitude': 2.0}}
    with mock.patch.object(maps_utils.requests, 'get',
                           return_value=FakeResponse(payload)) as get:
        maps_utils.google_place_details('places/abc')

    assert get.call_args[0][0].endswith('/v1/places/abc')
    assert 'places/places' not in get.call_args[0][0]


@override_settings(GOOGLE_MAPS_API_KEY=KEY, GOOGLE_PLACES_API_VERSION='legacy')
def test_legacy_place_details_also_exposes_flat_location():
    payload = {
        'status': 'OK',
        'result': {
            'place_id': 'abc123', 'name': 'Benz Circle',
            'formatted_address': 'Benz Circle, Vijayawada',
            'geometry': {'location': {'lat': 16.5062, 'lng': 80.6480}},
        },
    }
    with mock.patch.object(maps_utils.requests, 'get',
                           return_value=FakeResponse(payload)):
        result = maps_utils.google_place_details('abc123')

    assert result['location'] == {'lat': 16.5062, 'lng': 80.6480}


# --------------------------------------------------------------------------
# 4. failures are distinguishable
# --------------------------------------------------------------------------

@override_settings(GOOGLE_MAPS_API_KEY='')
def test_missing_credentials_is_reported_as_such():
    assert maps_utils.google_places_autocomplete('benz') is None
    assert maps_utils.MapsFailure.reason() == maps_utils.NOT_CONFIGURED
    assert maps_utils.maps_configured() is False


@override_settings(GOOGLE_MAPS_API_KEY=KEY)
def test_key_is_read_lazily_not_at_import():
    """A key supplied after import must be seen.

    It used to be a module-level constant, so every settings override -- and
    any process configuring settings late -- was ignored.
    """
    assert maps_utils.maps_configured() is True


@override_settings(GOOGLE_MAPS_API_KEY=KEY, GOOGLE_PLACES_API_VERSION='new')
def test_quota_exceeded_is_distinguishable():
    with mock.patch.object(maps_utils.requests, 'post',
                           return_value=FakeResponse({}, status_code=429)):
        assert maps_utils.google_places_autocomplete('benz') is None
    assert maps_utils.MapsFailure.reason() == maps_utils.QUOTA_EXCEEDED


@override_settings(GOOGLE_MAPS_API_KEY=KEY, GOOGLE_PLACES_API_VERSION='new')
@pytest.mark.parametrize('code', [401, 403])
def test_rejected_key_is_distinguishable(code):
    with mock.patch.object(maps_utils.requests, 'post',
                           return_value=FakeResponse({}, status_code=code)):
        assert maps_utils.google_places_autocomplete('benz') is None
    assert maps_utils.MapsFailure.reason() == maps_utils.PERMISSION_DENIED


@override_settings(GOOGLE_MAPS_API_KEY=KEY, GOOGLE_PLACES_API_VERSION='legacy')
def test_legacy_over_query_limit_is_quota_not_generic_error():
    """The legacy API answers HTTP 200 and puts the failure in the body."""
    with mock.patch.object(
        maps_utils.requests, 'get',
        return_value=FakeResponse({'status': 'OVER_QUERY_LIMIT',
                                   'error_message': 'quota'}),
    ):
        assert maps_utils.google_places_autocomplete('benz') is None
    assert maps_utils.MapsFailure.reason() == maps_utils.QUOTA_EXCEEDED


@override_settings(GOOGLE_MAPS_API_KEY=KEY, GOOGLE_PLACES_API_VERSION='legacy')
def test_legacy_request_denied_is_permission_denied():
    with mock.patch.object(
        maps_utils.requests, 'get',
        return_value=FakeResponse({'status': 'REQUEST_DENIED',
                                   'error_message': 'not enabled'}),
    ):
        assert maps_utils.google_places_autocomplete('benz') is None
    assert maps_utils.MapsFailure.reason() == maps_utils.PERMISSION_DENIED


@override_settings(GOOGLE_MAPS_API_KEY=KEY, GOOGLE_PLACES_API_VERSION='new')
def test_network_failure_is_generic_upstream_error():
    with mock.patch.object(maps_utils.requests, 'post',
                           side_effect=requests.ConnectionError('boom')):
        assert maps_utils.google_places_autocomplete('benz') is None
    assert maps_utils.MapsFailure.reason() == maps_utils.UPSTREAM_ERROR


@override_settings(GOOGLE_MAPS_API_KEY=KEY, GOOGLE_PLACES_API_VERSION='new')
def test_zero_results_is_not_a_failure(vijayawada):
    with mock.patch.object(maps_utils.requests, 'post',
                           return_value=FakeResponse({'suggestions': []})):
        result = maps_utils.google_places_autocomplete(
            'zzz', location='16.5062,80.6480')
    assert result is not None
    assert maps_utils.MapsFailure.reason() is None


# --------------------------------------------------------------------------
# the key must never leave the server
# --------------------------------------------------------------------------

@override_settings(GOOGLE_MAPS_API_KEY=KEY, GOOGLE_PLACES_API_VERSION='new')
def test_the_key_is_sent_in_a_header_not_the_response(vijayawada):
    with mock.patch.object(maps_utils.requests, 'post',
                           return_value=FakeResponse(NEW_AUTOCOMPLETE_OK)) as post:
        result = maps_utils.google_places_autocomplete(
            'benz', location='16.5062,80.6480')

    assert post.call_args.kwargs['headers']['X-Goog-Api-Key'] == KEY
    assert KEY not in json.dumps(result), (
        'the provider key must never appear in anything returned to a client'
    )


@override_settings(GOOGLE_MAPS_API_KEY=KEY, GOOGLE_PLACES_API_VERSION='new')
def test_unknown_version_setting_falls_back_to_new():
    with override_settings(GOOGLE_PLACES_API_VERSION='banana'):
        with mock.patch.object(maps_utils.requests, 'post',
                               return_value=FakeResponse(NEW_AUTOCOMPLETE_OK)) as post:
            maps_utils.google_places_autocomplete('benz')
        assert post.called, 'an unrecognised version must not disable search'
