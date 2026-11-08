"""The profile lists the areas an account can use (owner, 2026-11-08).

Read-only and derived from the Rider/Driver rows, so the web can route an
account that is both a rider and a driver to either area.
"""

import pytest

from servers.driver.models import Driver

pytestmark = pytest.mark.django_db

PROFILE = '/api/v1/auth/profile/'


def _profile(client):
    body = client.get(PROFILE).json()
    data = body.get('data', body)
    return data.get('user', data)


def test_a_rider_is_a_rider(auth_client_rider):
    client, _ = auth_client_rider
    assert _profile(client)['roles'] == ['rider']


def test_a_driver_is_a_driver(auth_client_driver):
    client, _ = auth_client_driver
    assert _profile(client)['roles'] == ['driver']


def test_a_rider_with_a_driver_row_is_both(auth_client_rider):
    client, user = auth_client_rider
    Driver.objects.create(user_id=user)
    assert _profile(client)['roles'] == ['rider', 'driver']


def test_roles_cannot_be_written(auth_client_rider):
    client, _ = auth_client_rider
    client.patch('/api/v1/auth/update/', {'roles': ['driver']}, format='json')
    assert _profile(client)['roles'] == ['rider']
