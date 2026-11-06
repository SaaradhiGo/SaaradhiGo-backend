"""Who cancelled, and what /ride/active/ calls active (2026-11-06).

- A driver cancel over REST sent no socket frame, so the rider stayed on
  "on the way". Both REST cancels now broadcast the socket's cancel frames.
- Every cancel frame and the trip detail carry `cancelled_by`.
- /ride/active/ returns a trip completed in the last hour for recovery; it is
  now marked `is_active: false` so no app names it the live trip (trip 138).
"""

import asyncio
import json
from decimal import Decimal as D
from unittest import mock

import pytest
from django.utils import timezone

from servers.consumers import RideRequestConsumer, TripStatusConsumer
from servers.ride.models import Trip, TripStatus

pytestmark = pytest.mark.django_db


def _trip(rider, driver, code):
    st, _ = TripStatus.objects.get_or_create(status_code=code)
    return Trip.objects.create(
        user_id=rider, driver_id=driver.driver if driver else None,
        pickup_lat=D('17.4'), pickup_long=D('78.4'),
        destination_lat=D('17.45'), destination_long=D('78.36'),
        estimated_fare=D('150.00'), status_id=st,
    )


def _sent(layer):
    return {c.args[0]: c.args[1] for c in layer.group_send.call_args_list}


def test_a_driver_cancel_over_rest_reaches_the_riders_socket(auth_client_driver, auth_client_rider,
                                                             django_capture_on_commit_callbacks):
    driver_client, driver_user = auth_client_driver
    _, rider_user = auth_client_rider
    trip = _trip(rider_user, driver_user, 'accepted')
    layer = mock.Mock(group_send=mock.AsyncMock())
    with mock.patch('channels.layers.get_channel_layer', return_value=layer), \
            django_capture_on_commit_callbacks(execute=True):
        r = driver_client.post(f'/api/v1/ride/trip/{trip.id}/driver-cancel/',
                               {'reason': 'no_show'}, format='json')
    assert r.status_code == 200, r.content
    sent = _sent(layer)
    rider_frame = sent[f'rider_{rider_user.id}']
    assert rider_frame['type'] == 'trip_update' and rider_frame['status'] == 'cancel'
    assert rider_frame['cancelled_by'] == 'driver'
    assert sent[f'trip_{trip.id}']['type'] == 'trip_status_update'


def test_a_rider_cancel_over_rest_reaches_the_drivers_socket(auth_client_driver, auth_client_rider,
                                                             django_capture_on_commit_callbacks):
    _, driver_user = auth_client_driver
    rider_client, rider_user = auth_client_rider
    trip = _trip(rider_user, driver_user, 'accepted')
    layer = mock.Mock(group_send=mock.AsyncMock())
    with mock.patch('channels.layers.get_channel_layer', return_value=layer), \
            django_capture_on_commit_callbacks(execute=True):
        r = rider_client.post(f'/api/v1/ride/trip/{trip.id}/cancel/', {}, format='json')
    assert r.status_code == 200, r.content
    assert _sent(layer)[f'trip_{trip.id}']['cancelled_by'] == 'rider'


def _forwarded(cls, handler, event):
    consumer = cls.__new__(cls)
    consumer.participation = 'assigned_driver'
    out = []

    async def send(text_data):
        out.append(json.loads(text_data))
    consumer.send = send
    asyncio.run(getattr(cls, handler)(consumer, event))
    return out[0]


def test_the_socket_forwards_who_cancelled():
    event = {'trip_id': '9', 'status': 'cancel', 'message': '', 'driver_id': None,
             'cancelled_by': 'rider'}
    assert _forwarded(TripStatusConsumer, 'trip_status_update', event)['cancelled_by'] == 'rider'
    assert _forwarded(RideRequestConsumer, 'trip_update', event)['cancelled_by'] == 'rider'


def test_the_trip_detail_names_who_cancelled(auth_client_rider):
    client, rider_user = auth_client_rider
    trip = _trip(rider_user, None, 'cancelled')
    trip.cancelled_by = 'driver'
    trip.save(update_fields=['cancelled_by'])
    body = client.get(f'/api/v1/ride/trip/{trip.id}/').json()
    assert body.get('data', body)['cancelled_by'] == 'driver'


def test_a_recently_completed_trip_is_not_called_active(auth_client_driver, auth_client_rider):
    driver_client, driver_user = auth_client_driver
    _, rider_user = auth_client_rider
    trip = _trip(rider_user, driver_user, 'completed')
    trip.completed_at = timezone.now()
    trip.save(update_fields=['completed_at'])
    data = driver_client.get('/api/v1/ride/active/').json()['data']
    assert data['id'] == trip.id
    assert data['is_active'] is False and data['recovery'] == 'recently_completed'


def test_a_live_trip_is_active(auth_client_driver, auth_client_rider):
    driver_client, driver_user = auth_client_driver
    _, rider_user = auth_client_rider
    _trip(rider_user, driver_user, 'accepted')
    data = driver_client.get('/api/v1/ride/active/').json()['data']
    assert data['is_active'] is True and data['recovery'] is None
