"""Matching end to end, and what the rider hears when nobody is nearby.

QA trip 191 (2026-11-08) got no offer and was cancelled at the 90 s deadline:
all three waves (1.5/3/5 km) found 0 candidates because the only QA sedan
driver was mid-trip on trip 190. Not a dispatch fault, but the rider was told
"No driver accepted your request" 50 s after the last wave had already found
nobody, and a client that missed the socket frame (the REST fallback) had no
reason to show at all.

1. E2E: a driver comes online in range through the real presence call
   (`add_driver_location`), the trip is dispatched through `start_dispatch`
   and the real `ride.dispatch_wave` task body, and an offer reaches the
   driver's group within TRIP_ACCEPT_TIMEOUT_SECONDS of the booking.
2. The no-driver frame says "No drivers nearby right now" when nobody was
   offered, and keeps the old sentence when drivers were offered and ignored.
3. GET /ride/trip/<id>/ carries the system reason and sentence.
"""

import time
from decimal import Decimal
from unittest import mock

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model

import servers.redis_client as rc
from servers.driver.models import Driver, Vehicle, VehicleType
from servers.ride import dispatch as dispatch_mod
from servers.ride import tasks as tasks_mod
from servers.ride.models import Trip, TripStatus

User = get_user_model()

PICKUP_LAT = Decimal('17.4504000')
PICKUP_LNG = Decimal('78.3810000')


@pytest.fixture(autouse=True)
def _isolate_redis_state():
    if rc.redis_client is None:
        pytest.skip('Redis not available')

    def _purge():
        for pattern in ('trip:offered:*', 'drivers:geo:*', 'driver:heartbeat:*',
                        f'{rc.VEHICLE_TYPE_PREFIX}*'):
            for key in rc.redis_client.scan_iter(match=pattern, count=500):
                rc.redis_client.delete(key)

    _purge()
    yield
    _purge()


@pytest.fixture
def recorder():
    """Every group_send, from dispatch and from the auto-cancel task."""
    sent = []

    class _Layer:
        async def group_send(self, group, message):
            sent.append((time.monotonic(), group, message))

    layer = _Layer()
    with mock.patch.object(dispatch_mod, 'get_channel_layer', lambda: layer), \
            mock.patch('channels.layers.get_channel_layer', lambda *a, **k: layer):
        yield sent


@pytest.fixture
def trip(db):
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    status, _ = TripStatus.objects.get_or_create(status_code='requested')
    rider = User.objects.create_user(phone_number='+919100000191', role='rider')
    return Trip.objects.create(
        user_id=rider, status_id=status, requested_vehicle_type=vt,
        pickup_lat=PICKUP_LAT, pickup_long=PICKUP_LNG,
        destination_lat=Decimal('17.4849000'), destination_long=Decimal('78.3870000'),
        pickup_address='Pickup', destination_address='Drop',
        estimated_fare=Decimal('128.00'),
    )


@pytest.fixture
def online_driver(db, trip):
    """An approved sedan driver, online ~400 m from the pickup."""
    user = User.objects.create_user(phone_number='+919200000191', role='driver')
    d = Driver.objects.create(user_id=user, approved=True, status='online')
    v = Vehicle.objects.create(driver_id=d, vehicle_type_id=trip.requested_vehicle_type,
                               vehicle_number='TS09AB0191')
    d.active_vehicle = v
    d.save(update_fields=['active_vehicle'])
    out = rc.add_driver_location(d.id, float(PICKUP_LNG) + 0.003, float(PICKUP_LAT) + 0.002)
    assert out.get('success') is not False, out
    return d


def _run_waves_inline():
    """Run each enqueued wave through the real Celery task body, in order."""
    queue = []

    def _enqueue(args=None, **kwargs):
        queue.append(args)

    return queue, mock.patch.object(tasks_mod.dispatch_wave, 'apply_async', _enqueue)


@pytest.mark.django_db
def test_booking_with_a_driver_in_range_sends_an_offer_within_the_timeout(
    trip, online_driver, recorder,
):
    timeout = int(settings.TRIP_ACCEPT_TIMEOUT_SECONDS)
    queue, patch = _run_waves_inline()
    booked = time.monotonic()
    with patch:
        started = dispatch_mod.start_dispatch(trip.id, reason='initial')
        assert started['enqueued'] is True
        # Wave 0 runs at once; successors would wait wave_gap_seconds each.
        elapsed_by_plan = 0.0
        while queue:
            args = queue.pop(0)
            tasks_mod.dispatch_wave.apply(args=args).get()
            offers = [m for _, g, m in recorder
                      if m.get('type') == 'ride_request' and g == f'driver_{online_driver.id}']
            if offers:
                break
            elapsed_by_plan += dispatch_mod.wave_gap_seconds()

    assert offers, 'no offer reached the online driver'
    assert offers[0]['trip_id'] == trip.id
    # Found in wave 0, well inside the accept deadline, in wall time and by the wave plan.
    assert elapsed_by_plan == 0.0
    assert elapsed_by_plan < timeout
    assert time.monotonic() - booked < timeout
    assert str(online_driver.id) in (rc.peek_offered_drivers(trip.id) or [])


@pytest.mark.django_db
def test_nobody_in_range_tells_the_rider_no_drivers_nearby(trip, recorder):
    """Trip 191: three empty waves, then the deadline."""
    queue, patch = _run_waves_inline()
    with patch:
        dispatch_mod.start_dispatch(trip.id, reason='initial')
        while queue:
            out = tasks_mod.dispatch_wave.apply(args=queue.pop(0)).get()
            assert out['candidates'] == 0
    tasks_mod.auto_cancel_trip(trip.id)

    frames = [m for _, g, m in recorder if m.get('reason') == 'no_driver_accepted']
    assert {g for _, g, m in recorder if m.get('reason') == 'no_driver_accepted'} == {
        f'rider_{trip.user_id_id}', f'trip_{trip.id}'}
    for f in frames:
        assert f['status'] == 'cancel'
        assert f['no_drivers_nearby'] is True
        assert f['message'] == 'No drivers nearby right now. Please try again.'


@pytest.mark.django_db
def test_offered_but_unaccepted_keeps_the_old_sentence(trip, online_driver, recorder):
    queue, patch = _run_waves_inline()
    with patch:
        dispatch_mod.start_dispatch(trip.id, reason='initial')
        tasks_mod.dispatch_wave.apply(args=queue.pop(0)).get()
    tasks_mod.auto_cancel_trip(trip.id)

    frame = next(m for _, g, m in recorder
                 if g == f'rider_{trip.user_id_id}' and m.get('reason') == 'no_driver_accepted')
    assert frame['no_drivers_nearby'] is False
    assert frame['message'] == tasks_mod.NO_DRIVER_MESSAGE


@pytest.mark.django_db
def test_redis_unknown_does_not_claim_nobody_was_nearby(trip, recorder):
    with mock.patch('servers.redis_client.peek_offered_drivers', return_value=None):
        tasks_mod.auto_cancel_trip(trip.id)
    frame = next(m for _, g, m in recorder if m.get('reason') == 'no_driver_accepted')
    assert frame['no_drivers_nearby'] is False


@pytest.mark.django_db
def test_trip_detail_carries_the_system_reason(trip, recorder):
    from rest_framework.test import APIClient

    tasks_mod.auto_cancel_trip(trip.id)
    client = APIClient()
    client.force_authenticate(trip.user_id)
    body = client.get(f'/api/v1/ride/trip/{trip.id}/').json()
    data = body.get('data', body)
    data = data.get('trip', data) if isinstance(data, dict) else data
    assert data['status'] == 'cancelled'
    assert data['cancelled_by'] == 'system'
    assert data['cancellation_reason'] == 'no_driver_accepted'
    assert data['cancellation_message'] == tasks_mod.NO_DRIVER_MESSAGE


@pytest.mark.django_db
def test_a_drivers_cancel_note_is_not_exposed(trip):
    from servers.ride.serializers import TripListSerializer

    trip.cancelled_by = 'driver'
    trip.cancellation_reason = 'other: rider was rude'
    trip.save(update_fields=['cancelled_by', 'cancellation_reason'])
    data = TripListSerializer(trip).data
    assert data['cancellation_reason'] is None
    assert data['cancellation_message'] is None
