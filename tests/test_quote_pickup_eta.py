"""The fare quote says how far the nearest free driver of the class is, in minutes only."""

from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from servers.ride.pickup_eta import pickup_eta
from servers.rider.models import Rider

User = get_user_model()
pytestmark = pytest.mark.django_db

QUOTE_URL = '/api/v1/ride/estimate-fare/'
NEARBY = 'servers.ride.pickup_eta.nearby_drivers'


@pytest.fixture
def rider_client():
    user = User.objects.create_user(phone_number='+919566000077', role='rider')
    Rider.objects.create(user_id=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    return client


def _quote(client):
    resp = client.post(QUOTE_URL, {
        'pickup_lat': '17.4450', 'pickup_long': '78.3800',
        'destination_lat': '17.4660', 'destination_long': '78.3800',
        'distance_km': '2.4', 'duration_min': '8', 'vehicle_type': 'sedan',
    }, format='json')
    assert resp.status_code == 200, resp.content
    body = resp.json()
    return body.get('data', body)


def test_minutes_to_the_nearest_driver_and_no_position(rider_client):
    with mock.patch(NEARBY, return_value=[['driver:7', '1450.0', (78.38, 17.44)]]) as near:
        body = _quote(rider_client)
    assert body['pickup_eta_min'] == 5  # 1.45 km at 18 km/h
    assert body['drivers_nearby'] is True
    assert near.call_args.kwargs['vehicle_type'] == 'sedan'
    assert '78.38' not in str(body) and 'driver:7' not in str(body)


def test_no_driver_nearby(rider_client):
    with mock.patch(NEARBY, return_value=[]):
        body = _quote(rider_client)
    assert body['pickup_eta_min'] is None and body['drivers_nearby'] is False


def test_a_redis_failure_never_fails_the_quote(rider_client):
    with mock.patch(NEARBY, side_effect=RuntimeError('down')):
        body = _quote(rider_client)
    assert body['drivers_nearby'] is False and body['estimated_fare']


def test_a_driver_at_the_pickup_is_one_minute_away():
    with mock.patch(NEARBY, return_value=[['driver:1', '20.0', (0, 0)]]):
        assert pickup_eta(17.4, 78.4, 'auto') == (1, True)
