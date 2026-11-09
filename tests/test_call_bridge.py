"""Masked rider-driver calls through the QA fake provider (servers/callbridge, 2026-11-09)."""

from datetime import timedelta
from decimal import Decimal as D

import pytest
from django.utils import timezone

from servers.callbridge.models import CallSession
from servers.ride.models import Trip, TripStatus

pytestmark = pytest.mark.django_db


def _trip(rider, driver, code, **kw):
    st, _ = TripStatus.objects.get_or_create(status_code=code)
    return Trip.objects.create(
        user_id=rider, driver_id=driver.driver if driver else None,
        pickup_lat=D('17.4'), pickup_long=D('78.4'), destination_lat=D('17.45'),
        destination_long=D('78.36'), estimated_fare=D('150.00'), status_id=st, **kw)


def _digits(user):
    return user.phone_number.lstrip('+')[-10:]


@pytest.mark.parametrize('side', ['rider', 'driver'])
def test_both_sides_get_a_bridge_and_never_a_real_number(auth_client_rider, auth_client_driver,
                                                         settings, side):
    settings.CALL_BRIDGE_FAKE_NUMBER = '+914000000000'
    rider_client, rider = auth_client_rider
    driver_client, driver = auth_client_driver
    trip = _trip(rider, driver, 'accepted')
    client = rider_client if side == 'rider' else driver_client
    r = client.post(f'/api/v1/calls/trips/{trip.id}/start/')
    assert r.status_code == 201, r.content
    call = r.json()['data']['call']
    assert call['from_role'] == side and call['kind'] == 'number'
    assert call['number'] == '+914000000000' and call['provider'] == 'fake'
    raw = r.content.decode()
    # The automated payload check: neither party's real number, in any response either receives.
    for body in (raw, client.get(f'/api/v1/calls/trips/{trip.id}/').content.decode(),
                 client.post(f'/api/v1/calls/sessions/{call["session_id"]}/end/').content.decode()):
        assert _digits(rider) not in body and _digits(driver) not in body


def test_a_call_is_a_trip_event_with_a_duration(auth_client_rider, auth_client_driver):
    rider_client, rider = auth_client_rider
    trip = _trip(rider, auth_client_driver[1], 'in_progress')
    sid = rider_client.post(f'/api/v1/calls/trips/{trip.id}/start/').json()['data']['call']['session_id']
    CallSession.objects.filter(pk=sid).update(started_at=timezone.now() - timedelta(seconds=42))
    ended = auth_client_driver[0].post(f'/api/v1/calls/sessions/{sid}/end/').json()['data']['call']
    assert 41 <= ended['duration_seconds'] <= 44
    events = rider_client.get(f'/api/v1/calls/trips/{trip.id}/').json()['data']['calls']
    assert [(e['from_role'], e['duration_seconds'] is not None) for e in events] == [('rider', True)]


def test_window_is_the_trip_and_15_minutes_after(auth_client_rider, auth_client_driver):
    rider_client, rider = auth_client_rider
    driver = auth_client_driver[1]
    just_done = _trip(rider, driver, 'completed', completed_at=timezone.now() - timedelta(minutes=10))
    long_done = _trip(rider, driver, 'completed', completed_at=timezone.now() - timedelta(minutes=16))
    waiting = _trip(rider, None, 'requested')
    assert rider_client.post(f'/api/v1/calls/trips/{just_done.id}/start/').status_code == 201
    assert rider_client.post(f'/api/v1/calls/trips/{long_done.id}/start/').status_code == 409
    assert rider_client.post(f'/api/v1/calls/trips/{waiting.id}/start/').status_code == 409


def test_strangers_cannot_call(auth_client_rider, auth_client_driver, auth_client_admin):
    trip = _trip(auth_client_rider[1], auth_client_driver[1], 'accepted')
    assert auth_client_admin[0].post(f'/api/v1/calls/trips/{trip.id}/start/').status_code == 404


def test_a_real_provider_stays_off_until_enabled(auth_client_rider, auth_client_driver, settings):
    settings.CALL_BRIDGE_PROVIDER = 'exotel'
    settings.CALL_BRIDGE_REAL_ENABLED = False
    trip = _trip(auth_client_rider[1], auth_client_driver[1], 'accepted')
    r = auth_client_rider[0].post(f'/api/v1/calls/trips/{trip.id}/start/')
    assert r.status_code == 503 and r.json()['error']['code'] == 'CALLING_UNAVAILABLE'
    assert not CallSession.objects.exists()
