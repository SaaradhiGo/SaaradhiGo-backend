"""Saved places can be labelled Home, Work or Other (2026-11-06).

Nullable and additive: places saved before this, and any saved without a
label, read back as None. The home screen prices labelled places first.
"""

import pytest

from servers.rider.models import FavoritePlace

pytestmark = pytest.mark.django_db

SAVE = '/api/v1/rider/locations/'
LIST = '/api/v1/rider/locations/all/'


def _save(client, **extra):
    return client.post(SAVE, {'address_text': 'Mindspace, Hitech City',
                              'latitude': '17.4410', 'longitude': '78.3810', **extra},
                       format='json')


def _items(client):
    body = client.get(LIST).json()
    data = body.get('data', body)
    return data if isinstance(data, list) else next(v for v in data.values() if isinstance(v, list))


def test_a_label_is_saved_and_read_back(auth_client_rider):
    auth_client_rider = auth_client_rider[0]
    assert _save(auth_client_rider, label='work').status_code == 201
    assert [p['label'] for p in _items(auth_client_rider)] == ['work']


def test_no_label_is_none(auth_client_rider):
    auth_client_rider = auth_client_rider[0]
    assert _save(auth_client_rider).status_code == 201
    assert _items(auth_client_rider)[0]['label'] is None


def test_an_unknown_label_is_refused(auth_client_rider):
    auth_client_rider = auth_client_rider[0]
    assert _save(auth_client_rider, label='gym').status_code == 400
    assert FavoritePlace.objects.count() == 0
