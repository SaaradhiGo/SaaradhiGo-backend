"""SOS must be durable, responsive under load, and impossible to tune out.

Three properties, in order of importance:

1. **A real emergency is never suppressed.** Every de-duplication rule here is
   written to fail open: a different event type, an acknowledged original, or a
   later repeat all produce a new event. The decision is made in PostgreSQL, so a
   cache miss cannot cause a duplicate and a cache hit cannot cause a drop.

2. **Repeats do not page ops repeatedly.** A panic button gets pressed more than
   once and a reconnecting app retries. Each repeat used to create another SOSEvent
   and another fan-out, which is how a real alert gets ignored.

3. **Telemetry cannot starve it.** SOS travels over HTTP, so it never shared the
   consumer dispatch loop that the pilot blocker exposed -- but it does share the
   process and the thread that `database_sync_to_async` falls back to, so it is
   measured under sustained GPS load rather than assumed safe.

Also asserted: the SOS log line carries no coordinates. SOS is exactly where a
person's location is most sensitive, and a log line is retained, shipped and
searchable.
"""

import asyncio
import json
import logging
import time
from decimal import Decimal

import pytest
from channels.testing import WebsocketCommunicator
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from base.asgi import application
from servers.driver.models import Driver, Vehicle, VehicleType
from servers.ride.models import Trip, TripStatus
from servers.sos.models import SOSEvent

User = get_user_model()

LAT, LNG = 17.4450000, 78.3800000


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_ride(suffix, status_code='in_progress'):
    from django.utils import timezone

    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    duser = User.objects.create_user(
        phone_number=f'+9198600{suffix:05d}', role='driver')
    driver = Driver.objects.create(user_id=duser, approved=True, status='active')
    v = Vehicle.objects.create(driver_id=driver, vehicle_type_id=vt,
                               vehicle_number=f'TS09SO{suffix:04d}')
    driver.active_vehicle = v
    driver.save(update_fields=['active_vehicle'])
    rider = User.objects.create_user(
        phone_number=f'+9197600{suffix:05d}', role='rider')

    st, _ = TripStatus.objects.get_or_create(status_code=status_code)
    trip = Trip.objects.create(
        user_id=rider, driver_id=driver, status_id=st,
        pickup_lat=Decimal(str(LAT)), pickup_long=Decimal(str(LNG)),
        destination_lat=Decimal(str(LAT + 0.01)), destination_long=Decimal(str(LNG)),
        estimated_fare=Decimal('150.00'), payment_method='cash', otp='123456',
    )
    now = timezone.now()
    trip.accepted_at = trip.reached_at = trip.started_at = now
    trip.save()
    return {'trip': trip, 'rider': rider, 'driver': driver, 'driver_user': duser}


def _api(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def _sos(user, trip=None, event_type='panic', **extra):
    body = {'event_type': event_type, **extra}
    if trip is not None:
        body['trip_id'] = trip.id
    return _api(user).post('/api/v1/sos/', body, format='json')


def _events(caplog, name):
    return [r.__dict__ for r in caplog.records
            if getattr(r, 'event', None) == name]


# ---------------------------------------------------------------------------
# A real emergency is never suppressed
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_a_first_sos_is_always_recorded():
    ride = _make_ride(1)
    resp = _sos(ride['rider'], ride['trip'])

    assert resp.status_code == 201, resp.content
    body = resp.json().get('data', resp.json())
    assert body['sos_id']
    assert SOSEvent.objects.filter(trip=ride['trip']).count() == 1


@pytest.mark.django_db
def test_a_different_event_type_is_always_a_new_event():
    """Panic then medical is an escalation, not a retry."""
    ride = _make_ride(2)
    first = _sos(ride['rider'], ride['trip'], event_type='panic')
    second = _sos(ride['rider'], ride['trip'], event_type='medical')

    ids = {first.json()['data']['sos_id'], second.json()['data']['sos_id']}
    print(f'different types -> ids={ids}')
    assert len(ids) == 2, (
        'a different emergency type was folded into the previous event')
    assert SOSEvent.objects.filter(trip=ride['trip']).count() == 2


@pytest.mark.django_db
def test_a_new_sos_after_ops_acknowledged_is_a_new_event():
    """Once ops have the first one, a second press means something new."""
    ride = _make_ride(3)
    first = _sos(ride['rider'], ride['trip'])
    first_id = first.json()['data']['sos_id']

    SOSEvent.objects.filter(id=first_id).update(status='acknowledged')

    second = _sos(ride['rider'], ride['trip'])
    second_id = second.json()['data']['sos_id']
    print(f'after acknowledge -> first={first_id} second={second_id}')

    assert second_id != first_id, (
        'an SOS raised after ops acknowledged the previous one was suppressed'
    )
    assert SOSEvent.objects.filter(trip=ride['trip']).count() == 2


@pytest.mark.django_db
def test_a_repeat_outside_the_window_is_a_new_event():
    """Minutes later is a new emergency, not a duplicate press."""
    from datetime import timedelta

    from django.utils import timezone

    from servers.sos.views import SOS_DEDUPE_WINDOW_SECONDS

    ride = _make_ride(4)
    first = _sos(ride['rider'], ride['trip'])
    first_id = first.json()['data']['sos_id']

    # Age the original past the window.
    SOSEvent.objects.filter(id=first_id).update(
        created_at=timezone.now() - timedelta(
            seconds=SOS_DEDUPE_WINDOW_SECONDS + 30))

    second = _sos(ride['rider'], ride['trip'])
    assert second.json()['data']['sos_id'] != first_id, (
        f'a repeat more than {SOS_DEDUPE_WINDOW_SECONDS}s later was suppressed'
    )


@pytest.mark.django_db
def test_a_different_rider_is_never_deduped_against_another():
    ride_a = _make_ride(5)
    ride_b = _make_ride(6)
    a = _sos(ride_a['rider'], ride_a['trip'])
    b = _sos(ride_b['rider'], ride_b['trip'])

    assert a.json()['data']['sos_id'] != b.json()['data']['sos_id']
    assert SOSEvent.objects.count() >= 2


@pytest.mark.django_db
def test_the_driver_can_also_raise_sos_on_the_same_trip():
    """Driver and rider are different people in danger. Never collapsed."""
    ride = _make_ride(7)
    rider_sos = _sos(ride['rider'], ride['trip'])
    driver_sos = _sos(ride['driver_user'], ride['trip'])

    assert rider_sos.status_code == 201, rider_sos.content
    assert driver_sos.status_code == 201, driver_sos.content
    assert (rider_sos.json()['data']['sos_id']
            != driver_sos.json()['data']['sos_id'])


# ---------------------------------------------------------------------------
# Repeats do not page ops repeatedly
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_an_immediate_repeat_is_collapsed_onto_the_open_event(caplog):
    """The reconnect / double-tap case."""
    ride = _make_ride(8)
    first = _sos(ride['rider'], ride['trip'])
    first_id = first.json()['data']['sos_id']

    with caplog.at_level(logging.WARNING, logger='servers.sos.views'):
        repeats = [_sos(ride['rider'], ride['trip']) for _ in range(4)]

    ids = {r.json()['data']['sos_id'] for r in repeats}
    total = SOSEvent.objects.filter(trip=ride['trip']).count()
    collapsed = _events(caplog, 'sos_repeat_collapsed')
    print(f'5 presses -> events={total} ids={ids | {first_id}} '
          f'collapsed_logs={len(collapsed)}')

    assert total == 1, (
        f'five presses of one panic button created {total} SOS events, which is '
        'four extra pages for one emergency'
    )
    assert ids == {first_id}, ids
    assert all(r.status_code == 200 for r in repeats), (
        [r.status_code for r in repeats])
    assert all(r.json()['data'].get('repeat_of') == first_id for r in repeats), (
        'a collapsed repeat should say what it was a repeat of')
    assert len(collapsed) == 4, (
        'each collapse must be visible, so a stuck panic button is diagnosable')


# ---------------------------------------------------------------------------
# No coordinates in the logs
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_the_sos_log_never_contains_coordinates(caplog):
    """SOS is where location is most sensitive, and logs are retained and shipped."""
    ride = _make_ride(9)

    with caplog.at_level(logging.INFO, logger='servers.sos.views'):
        resp = _sos(ride['rider'], ride['trip'], lat=LAT, lng=LNG)
    assert resp.status_code == 201, resp.content

    raised = _events(caplog, 'sos_raised')
    assert raised, 'raising an SOS must be logged -- it is a safety event'
    blob = json.dumps({k: str(v) for k, v in raised[0].items()})
    for forbidden in ('17.445', '78.38', str(LAT), str(LNG)):
        assert forbidden not in blob, (
            f'the SOS log leaked {forbidden!r}: {blob}')
    assert raised[0]['has_location'] is True, (
        'the log should still say WHETHER a position was captured')
    # And the coordinates are in the database, where access is controlled.
    ev = SOSEvent.objects.get(id=resp.json()['data']['sos_id'])
    assert ev.latitude is not None and ev.longitude is not None


# ---------------------------------------------------------------------------
# Telemetry cannot starve it
# ---------------------------------------------------------------------------

@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_sos_stays_responsive_under_sustained_gps_load():
    """500 location frames in flight, then SOS. Latency measured, not assumed.

    SOS is an HTTP endpoint, so it never traversed the consumer dispatch loop that
    caused the pilot blocker. It does share the process and the single thread that
    `database_sync_to_async` falls back to, which is the reason to measure.
    """
    from servers.redis_client import set_driver_active_trip

    ride = await asyncio.to_thread(_make_ride, 10)
    trip, rider, driver = ride['trip'], ride['rider'], ride['driver']
    await asyncio.to_thread(set_driver_active_trip, driver.id, trip.id)

    def _token(u):
        from rest_framework_simplejwt.tokens import AccessToken
        return str(AccessToken.for_user(u))

    dtok = await asyncio.to_thread(_token, ride['driver_user'])
    dws = WebsocketCommunicator(
        application, f'/ws/driver/location/?token={dtok}&lat={LAT}&lng={LNG}')
    connected, _ = await dws.connect(timeout=20)
    assert connected
    await dws.receive_from(timeout=10)

    # Quiet baseline first, so the loaded number has something to be compared to.
    def _timed_sos(user, trip_obj, event_type):
        t0 = time.monotonic()
        r = _sos(user, trip_obj, event_type=event_type)
        return r.status_code, time.monotonic() - t0

    baseline_code, baseline = await asyncio.to_thread(
        _timed_sos, rider, trip, 'vehicle_breakdown')

    for i in range(500):
        await dws.send_to(text_data=json.dumps(
            {'lat': LAT + 0.0004 * i, 'lng': LNG}))

    loaded_code, loaded = await asyncio.to_thread(
        _timed_sos, rider, trip, 'panic')

    print(f'SOS latency: quiet={baseline:.2f}s (HTTP {baseline_code})  '
          f'under 500 queued frames={loaded:.2f}s (HTTP {loaded_code})')

    try:
        await dws.disconnect(timeout=5)
    except Exception:  # noqa: BLE001
        pass

    assert baseline_code == 201, baseline_code
    assert loaded_code == 201, (
        f'SOS failed under sustained GPS load: HTTP {loaded_code}')
    assert loaded < 10, f'SOS took {loaded:.1f}s under load'
    # Both events exist: load must not have silently dropped one.
    count = await asyncio.to_thread(
        lambda: SOSEvent.objects.filter(trip=trip).count())
    assert count == 2, f'expected both SOS events to be durable, found {count}'
