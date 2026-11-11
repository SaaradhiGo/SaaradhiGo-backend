"""POST /rider/locations/ takes coordinates as JSON numbers or numeric strings.

It used to slice the values as text, so a JSON number gave a 500. The rider
web sends toFixed(7) strings and the rider app sends double.toString()
strings (which can carry 15+ decimals); both must keep working.
"""

import pytest

from servers.rider.models import FavoritePlace

pytestmark = pytest.mark.django_db

SAVE = '/api/v1/rider/locations/'


def _save(client, lat, lng, **extra):
    body = {'address_text': 'Mindspace, Hitech City', **extra}
    if lat is not ...:
        body['latitude'] = lat
    if lng is not ...:
        body['longitude'] = lng
    return client.post(SAVE, body, format='json')


@pytest.mark.parametrize('lat,lng', [
    (17.441, 78.381),                              # JSON numbers
    (17, 78),                                      # JSON integers
    ('17.4410000', '78.3810000'),                  # rider web: toFixed(7)
    ('17.441012345678901', '78.38101234567891'),   # rider app: double.toString()
    (' 17.441 ', '78.381'),
])
def test_numbers_and_numeric_strings_are_saved(auth_client_rider, lat, lng):
    client = auth_client_rider[0]
    r = _save(client, lat, lng)
    assert r.status_code == 201, r.content
    place = FavoritePlace.objects.get()
    assert abs(float(place.latitude) - float(str(lat).strip())) < 1e-6
    assert abs(float(place.longitude) - float(lng)) < 1e-6
    loc = r.json()['data']['location']
    assert {'id', 'address_text', 'latitude', 'longitude', 'label'} <= set(loc)


@pytest.mark.parametrize('lat,lng,field', [
    (..., 78.381, 'latitude'),
    (17.441, ..., 'longitude'),
    (None, 78.381, 'latitude'),
    ('', 78.381, 'latitude'),
    ('abc', 78.381, 'latitude'),
    (True, 78.381, 'latitude'),
    ([17.4], 78.381, 'latitude'),
    ('NaN', 78.381, 'latitude'),
    ('Infinity', 78.381, 'latitude'),
    (90.5, 78.381, 'latitude'),
    (-91, 78.381, 'latitude'),
    (17.441, 180.01, 'longitude'),
    (17.441, '-181', 'longitude'),
])
def test_bad_coordinates_are_a_clear_400(auth_client_rider, lat, lng, field):
    client = auth_client_rider[0]
    r = _save(client, lat, lng)
    assert r.status_code == 400, r.content
    err = r.json()['error']
    assert err['code'] == 'VALIDATION_ERROR'
    assert err['details']['field'] == field
    assert field.capitalize() in err['message']
    assert FavoritePlace.objects.count() == 0


def test_range_edges_are_accepted(auth_client_rider):
    client = auth_client_rider[0]
    assert _save(client, -90, 180).status_code == 201
    assert _save(client, '90', '-180').status_code == 201
