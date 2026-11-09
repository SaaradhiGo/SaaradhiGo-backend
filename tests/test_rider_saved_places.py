"""/rider/places/: Home, Work and up to ten others, with a service-area notice (2026-11-09)."""

import pytest

from servers.rider.models import FavoritePlace
from tests.conftest import ensure_zone_covering

pytestmark = pytest.mark.django_db

URL = '/api/v1/rider/places/'
INSIDE = (17.441, 78.381)       # Hitech City, inside the test zone
OUTSIDE = (12.9716, 77.5946)    # Bengaluru, outside it


@pytest.fixture(autouse=True)
def zone():
    return ensure_zone_covering(*INSIDE)


def _add(client, label='home', name='', at=INSIDE, address='Mindspace, Hitech City'):
    return client.post(URL, {'label': label, 'name': name, 'address_text': address,
                             'latitude': at[0], 'longitude': at[1]}, format='json')


def test_create_list_update_delete(auth_client_rider):
    client, user = auth_client_rider
    r = _add(client)
    assert r.status_code == 201, r.content
    place = r.json()['data']['place']
    assert place['label'] == 'home' and place['in_service_area'] is True and place['notice'] is None

    r = client.patch(f'{URL}{place["id"]}/', {'address_text': 'Cyber Towers', 'latitude': 17.45,
                                              'longitude': 78.38}, format='json')
    assert r.status_code == 200, r.content
    moved = r.json()['data']['place']
    assert moved['address_text'] == 'Cyber Towers' and moved['latitude'].startswith('17.45')

    listed = client.get(URL).json()['data']
    assert [p['id'] for p in listed['places']] == [place['id']]
    assert listed['limits'] == {'home': 1, 'work': 1, 'other': 10}

    assert client.delete(f'{URL}{place["id"]}/').status_code == 200
    assert not FavoritePlace.objects.filter(user_id=user).exists()


def test_home_and_work_come_first(auth_client_rider):
    client = auth_client_rider[0]
    _add(client, 'other', 'Gym')
    _add(client, 'work')
    _add(client, 'home')
    labels = [p['label'] for p in client.get(URL).json()['data']['places']]
    assert labels == ['home', 'work', 'other']


@pytest.mark.parametrize('label', ['home', 'work'])
def test_one_home_and_one_work(auth_client_rider, label):
    client = auth_client_rider[0]
    assert _add(client, label).status_code == 201
    r = _add(client, label)
    assert r.status_code == 409 and r.json()['error']['code'] == 'PLACE_LIMIT'


def test_ten_others_at_most(auth_client_rider):
    client = auth_client_rider[0]
    for i in range(10):
        assert _add(client, 'other', f'Place {i}').status_code == 201
    r = _add(client, 'other', 'Eleventh')
    assert r.status_code == 409 and r.json()['error']['code'] == 'PLACE_LIMIT'


def test_relabel_respects_the_limit(auth_client_rider):
    client = auth_client_rider[0]
    _add(client, 'home')
    gym = _add(client, 'other', 'Gym').json()['data']['place']
    r = client.patch(f'{URL}{gym["id"]}/', {'label': 'home'}, format='json')
    assert r.status_code == 409
    r = client.patch(f'{URL}{gym["id"]}/', {'label': 'work'}, format='json')
    assert r.status_code == 200 and r.json()['data']['place']['name'] == ''


def test_rename_an_other_place(auth_client_rider):
    client = auth_client_rider[0]
    gym = _add(client, 'other', 'Gym').json()['data']['place']
    r = client.patch(f'{URL}{gym["id"]}/', {'name': 'Yoga studio'}, format='json')
    assert r.json()['data']['place']['name'] == 'Yoga studio'
    r = client.patch(f'{URL}{gym["id"]}/', {'name': ''}, format='json')
    assert r.status_code == 400


def test_an_other_place_needs_a_name(auth_client_rider):
    r = _add(auth_client_rider[0], 'other', '')
    assert r.status_code == 400 and r.json()['error']['details']['field'] == 'name'


def test_home_ignores_a_name(auth_client_rider):
    r = _add(auth_client_rider[0], 'home', 'My flat')
    assert r.json()['data']['place']['name'] == ''


@pytest.mark.parametrize('body,field', [
    ({'label': 'garage'}, 'label'),
    ({'address_text': '  '}, 'address_text'),
    ({'latitude': 'abc'}, 'latitude'),
    ({'longitude': 181}, 'longitude'),
    ({'name': 'x' * 41, 'label': 'other'}, 'name'),
])
def test_bad_fields_are_a_400(auth_client_rider, body, field):
    full = {'label': 'other', 'name': 'Gym', 'address_text': 'A', 'latitude': 17.4,
            'longitude': 78.3, **body}
    r = auth_client_rider[0].post(URL, full, format='json')
    assert r.status_code == 400 and r.json()['error']['details']['field'] == field


def test_outside_the_service_area_is_saved_with_a_notice(auth_client_rider):
    r = _add(auth_client_rider[0], 'work', at=OUTSIDE)
    assert r.status_code == 201
    place = r.json()['data']['place']
    assert place['in_service_area'] is False
    assert place['notice'] == 'Outside our service area for now'


def test_another_riders_place_is_not_found(auth_client_rider, django_user_model):
    other = django_user_model.objects.create_user(phone_number='+919000000999', password='x')
    theirs = FavoritePlace.objects.create(user_id=other, label='home', address_text='Theirs',
                                          latitude=17.4, longitude=78.3)
    client = auth_client_rider[0]
    assert client.patch(f'{URL}{theirs.id}/', {'name': 'Mine'}, format='json').status_code == 404
    assert client.delete(f'{URL}{theirs.id}/').status_code == 404
    assert FavoritePlace.objects.filter(pk=theirs.pk).exists()


def test_signed_out_is_refused(api_client):
    assert api_client.get(URL).status_code in (401, 403)
