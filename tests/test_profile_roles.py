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


RIDE = '/api/v1/auth/me/ride/'


def test_a_driver_rides_with_the_same_account(auth_client_driver):
    """13 Nov: 'Ride with your SaaradhiGo account' adds the rider role, once, to the same user."""
    client, user = auth_client_driver
    first = client.post(RIDE).json()
    first = first.get('data', first)
    assert first == {'roles': ['rider', 'driver'], 'added': True}
    again = client.post(RIDE).json()
    assert again.get('data', again)['added'] is False
    assert _profile(client)['roles'] == ['rider', 'driver']
    assert _profile(client)['id'] == user.id


def test_a_closing_account_cannot_add_riding(auth_client_driver):
    from servers.auth_user.models import AccountClosure
    client, user = auth_client_driver
    AccountClosure.objects.create(user=user, status='waiting_payouts')
    assert client.post(RIDE).status_code == 409
    assert _profile(client)['roles'] == ['driver']
