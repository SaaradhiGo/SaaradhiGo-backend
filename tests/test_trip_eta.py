"""Arrival estimates on an active trip (servers/ride/eta.py, 2026-11-09)."""

import asyncio
import json
from datetime import datetime, timezone as dt_tz
from decimal import Decimal as D
from unittest import mock

import pytest

from servers.consumers import DriverLocationConsumer, TripStatusConsumer
from servers.ride import eta
from servers.ride.models import Trip, TripStatus

pytestmark = pytest.mark.django_db

PICKUP = (D('17.4000'), D('78.4000'))
DROP = (D('17.4500'), D('78.3600'))


def _trip(rider, driver, code):
    st, _ = TripStatus.objects.get_or_create(status_code=code)
    return Trip.objects.create(
        user_id=rider, driver_id=driver.driver if driver else None,
        pickup_lat=PICKUP[0], pickup_long=PICKUP[1],
        destination_lat=DROP[0], destination_long=DROP[1],
        estimated_fare=D('150.00'), status_id=st,
    )


def test_whole_minutes_at_the_market_speed(settings):
    settings.ETA_ROAD_FACTOR = 1.0
    settings.ETA_MARKET_KMH = {'IN': 18.0}
    assert eta._minutes(3000, 'IN') == 10        # 3 km at 18 km/h
    assert eta._minutes(3001, 'IN') == 11        # rounded up, never a fraction
    assert eta._minutes(5, 'IN') == 1            # never 0
    settings.PICKUP_ETA_CITY_KMH = 30
    assert eta._minutes(3000, 'ZZ') == 6         # unknown market: the city default


def test_to_pickup_carries_distance_minutes_and_arrival(auth_client_rider, auth_client_driver):
    trip = _trip(auth_client_rider[1], auth_client_driver[1], 'accepted')
    now = datetime(2026, 11, 9, 4, 0, 30, tzinfo=dt_tz.utc)
    out = eta.compute(trip, driver_at=(17.39, 78.40), now=now)
    assert out['phase'] == 'to_pickup' and out['estimated'] is True
    assert out['pickup_distance_km'] == '1.4'          # 1.11 km straight x 1.3
    assert isinstance(out['pickup_eta_min'], int) and out['pickup_eta_min'] >= 1
    assert out['drop_eta_min'] > out['pickup_eta_min']
    assert out['arrive_at'].endswith(':00+05:30') or ':00' in out['arrive_at']


def test_on_trip_counts_from_where_the_driver_is(auth_client_rider, auth_client_driver):
    trip = _trip(auth_client_rider[1], auth_client_driver[1], 'in_progress')
    far = eta.compute(trip, driver_at=(17.40, 78.40))
    near = eta.compute(trip, driver_at=(17.449, 78.361))
    assert far['phase'] == near['phase'] == 'to_drop'
    assert near['drop_eta_min'] == 1 < far['drop_eta_min']
    assert near['pickup_eta_min'] is None


@pytest.mark.parametrize('code', ['requested', 'completed', 'cancelled'])
def test_no_estimate_outside_an_active_trip(auth_client_rider, auth_client_driver, code):
    trip = _trip(auth_client_rider[1], auth_client_driver[1], code)
    assert eta.compute(trip, driver_at=(17.39, 78.40)) is None


def test_no_estimate_without_a_live_position(auth_client_rider, auth_client_driver):
    trip = _trip(auth_client_rider[1], auth_client_driver[1], 'accepted')
    with mock.patch.object(eta, 'driver_position', return_value=None):
        assert eta.compute(trip) is None


def test_rest_endpoint_for_rider_and_driver_only(auth_client_rider, auth_client_driver, django_user_model):
    rider_client, rider = auth_client_rider
    driver_client, driver = auth_client_driver
    trip = _trip(rider, driver, 'accepted')
    with mock.patch.object(eta, 'driver_position', return_value=(17.39, 78.40)):
        r = rider_client.get(f'/api/v1/ride/trip/{trip.id}/eta/')
        assert r.status_code == 200 and r.json()['data']['eta']['phase'] == 'to_pickup'
        assert driver_client.get(f'/api/v1/ride/trip/{trip.id}/eta/').status_code == 200
    stranger = django_user_model.objects.create_user(phone_number='+919000000777', role='rider')
    from rest_framework.test import APIClient
    from rest_framework_simplejwt.tokens import AccessToken
    other = APIClient()
    other.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(stranger)}')
    assert other.get(f'/api/v1/ride/trip/{trip.id}/eta/').status_code == 404


def test_rest_endpoint_says_null_not_zero(auth_client_rider, auth_client_driver):
    rider_client, rider = auth_client_rider
    trip = _trip(rider, auth_client_driver[1], 'accepted')
    with mock.patch.object(eta, 'driver_position', return_value=None):
        r = rider_client.get(f'/api/v1/ride/trip/{trip.id}/eta/')
    assert r.json()['data']['eta'] is None


def test_the_push_is_throttled_to_every_15_seconds():
    consumer = DriverLocationConsumer()
    consumer.channel_layer = mock.Mock(group_send=mock.AsyncMock())
    with mock.patch.object(eta, 'for_trip_id', return_value={'phase': 'to_pickup'}), \
            mock.patch.object(eta, 'clock', side_effect=[100.0, 105.0, 116.0]):
        for _ in range(3):
            asyncio.run(consumer._maybe_push_eta(7, 17.39, 78.40))
    calls = consumer.channel_layer.group_send.call_args_list
    assert len(calls) == 2
    assert calls[0].args == ('trip_7', {'type': 'trip_eta_update', 'trip_id': 7,
                                        'eta': {'phase': 'to_pickup'}})


def test_the_rider_gets_the_frame_and_the_driver_does_not():
    frames = []
    for role in ('rider', 'assigned_driver'):
        consumer = TripStatusConsumer()
        consumer.participation = role
        consumer._queue_location_frame = frames.append
        asyncio.run(consumer.trip_eta_update({'trip_id': 7, 'eta': {'drop_eta_min': 4}}))
    assert frames == [{'type': 'trip_eta_update', 'trip_id': 7, 'eta': {'drop_eta_min': 4}}]
    json.dumps(frames)
