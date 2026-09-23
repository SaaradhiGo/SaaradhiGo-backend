"""Durable dispatch: Celery owns the wave loop, the rider socket does not.

Dispatch used to run as an `asyncio.create_task` inside `RideRequestConsumer`
and `disconnect()` cancelled it, so a rider whose phone dropped during the
search lost waves 2 and 3 and the trip then timed out as
`no_driver_accepted` though most drivers were never asked.

These tests pin the properties that make the new design safe:

* a wave performs **no PostgreSQL writes**, so duplicate or stale delivery is
  harmless — this is the business-idempotency claim, and it is asserted by
  comparing the trip row before and after;
* eligibility is read from PostgreSQL on every execution, never from Redis;
* the `dispatch_epoch` is not durable and no lifecycle decision reads it;
* offer dismissal now happens on every terminal transition.

A note on concurrency, honestly stated: the suite runs on SQLite, where
`SELECT FOR UPDATE` is a no-op, so no test here can demonstrate real row-lock
contention — that needs PostgreSQL. What the race tests below *do* prove is
the property that actually broke production behaviour: that the losing
operation re-reads committed state inside its transaction instead of writing
back a stale in-memory instance. The dangerous interleaving is reproduced
deterministically, which is stronger than a thread race that may not
interleave at all on a given run.
"""

from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

import servers.redis_client as rc
from servers.driver.models import Driver, Vehicle, VehicleType
from servers.ride import dispatch as dispatch_mod
from servers.ride.models import Trip, TripStatus

User = get_user_model()

PICKUP_LAT = Decimal('17.4450')
PICKUP_LNG = Decimal('78.3800')
VEHICLE_TYPE = 'sedan'


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_redis_state():
    """Redis is not rolled back by the test transaction.

    SQLite restarts primary keys for every test, so trip id 1 (and driver id
    1) recur constantly and their Redis keys would otherwise leak between
    tests — a previous test's offer set makes the next test's wave think the
    driver was already notified. Scoped to the two prefixes this module
    touches; no FLUSHDB.
    """
    def _purge():
        if rc.redis_client is None:
            return
        for pattern in ('trip:offered:*', 'drivers:geo:*', 'driver:heartbeat:*'):
            try:
                for key in rc.redis_client.scan_iter(match=pattern, count=500):
                    rc.redis_client.delete(key)
            except Exception:  # noqa: BLE001
                pass

    _purge()
    yield
    _purge()


@pytest.fixture
def rider(db):
    return User.objects.create_user(phone_number='+919100000001', role='rider')


@pytest.fixture
def other_rider(db):
    return User.objects.create_user(phone_number='+919100000009', role='rider')


@pytest.fixture
def vehicle_type(db):
    # `type` is unique and the pricing seed migrations already create the
    # standard set, so get_or_create rather than create.
    vt, _ = VehicleType.objects.get_or_create(type=VEHICLE_TYPE)
    return vt


@pytest.fixture
def driver(db, vehicle_type):
    user = User.objects.create_user(phone_number='+919200000001', role='driver')
    d = Driver.objects.create(user_id=user, approved=True)
    Vehicle.objects.create(
        driver_id=d, vehicle_type_id=vehicle_type, vehicle_number='TS09AB0001',
    )
    return d


@pytest.fixture
def searching_trip(db, rider, vehicle_type):
    """A trip in `requested` with no driver — i.e. dispatchable."""
    status, _ = TripStatus.objects.get_or_create(status_code='requested')
    return Trip.objects.create(
        user_id=rider,
        status_id=status,
        requested_vehicle_type=vehicle_type,
        pickup_lat=PICKUP_LAT,
        pickup_long=PICKUP_LNG,
        destination_lat=Decimal('17.4500'),
        destination_long=Decimal('78.4000'),
        pickup_address='Pickup',
        destination_address='Drop',
        estimated_fare=Decimal('150.00'),
    )


@pytest.fixture
def geo_driver(driver):
    """Put the driver in the Redis GEO index with a live heartbeat."""
    if rc.redis_client is None:
        pytest.skip('Redis not available')
    key = rc.geo_key_for(VEHICLE_TYPE)
    member = f'driver:{driver.id}:{VEHICLE_TYPE}'
    rc.redis_client.geoadd(key, [float(PICKUP_LNG), float(PICKUP_LAT), member])
    rc.redis_client.setex(f'{rc.HEARTBEAT_PREFIX}{driver.id}', 60, '1')
    yield driver
    rc.redis_client.zrem(key, member)
    rc.redis_client.delete(f'{rc.HEARTBEAT_PREFIX}{driver.id}')


@pytest.fixture
def channel_recorder():
    """Capture group_send calls instead of needing a live channel layer."""
    sent = []

    class _Layer:
        async def group_send(self, group, message):
            sent.append((group, message))

    with mock.patch.object(dispatch_mod, 'get_channel_layer', lambda: _Layer()):
        yield sent


@pytest.fixture
def captured_enqueues():
    """Capture dispatch_wave.apply_async without running a worker."""
    calls = []

    def _capture(args=None, **kwargs):
        calls.append({'args': args, 'countdown': kwargs.get('countdown')})

    from servers.ride import tasks as tasks_mod
    with mock.patch.object(tasks_mod.dispatch_wave, 'apply_async', _capture):
        yield calls


def _trip_fingerprint(trip_id):
    """The durable fields a dispatch wave must never change."""
    t = Trip.objects.select_related('status_id').get(id=trip_id)
    return (
        t.driver_id_id,
        t.status_id.status_code if t.status_id else None,
        t.accepted_at, t.cancelled_at, t.cancelled_by,
        t.final_fare, t.estimated_fare,
    )


def _set_status(trip, code):
    status, _ = TripStatus.objects.get_or_create(status_code=code)
    trip.status_id = status
    trip.save(update_fields=['status_id'])
    return trip


# ---------------------------------------------------------------------------
# 1-2. Celery owns execution and scheduling
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_start_dispatch_enqueues_wave_zero(searching_trip, captured_enqueues):
    result = dispatch_mod.start_dispatch(searching_trip.id, reason='initial')

    assert result['enqueued'] is True
    assert len(captured_enqueues) == 1
    trip_id, wave_index, epoch, radii = captured_enqueues[0]['args']
    assert trip_id == searching_trip.id
    assert wave_index == 0
    assert epoch == result['epoch']
    assert radii == [1500, 3000, 5000]
    # Wave 0 runs immediately; only successors carry a countdown.
    assert captured_enqueues[0]['countdown'] is None


@pytest.mark.django_db
def test_wave_schedules_successor_with_countdown_not_sleep(
    searching_trip, geo_driver, channel_recorder, captured_enqueues,
):
    """Successors are delayed by Celery, never by occupying a worker."""
    out = dispatch_mod.execute_wave(
        searching_trip.id, 0, 'epoch-a', [1500, 3000, 5000],
    )
    assert out['executed'] is True

    assert len(captured_enqueues) == 1
    trip_id, wave_index, epoch, radii = captured_enqueues[0]['args']
    assert (trip_id, wave_index, epoch) == (searching_trip.id, 1, 'epoch-a')
    assert captured_enqueues[0]['countdown'] == dispatch_mod.wave_gap_seconds()


@pytest.mark.django_db
def test_final_wave_schedules_no_successor(
    searching_trip, geo_driver, channel_recorder, captured_enqueues,
):
    dispatch_mod.execute_wave(searching_trip.id, 2, 'epoch-a', [1500, 3000, 5000])
    assert captured_enqueues == []


@pytest.mark.django_db
def test_wave_delivers_offer_to_nearby_driver(
    searching_trip, geo_driver, channel_recorder, captured_enqueues,
):
    out = dispatch_mod.execute_wave(searching_trip.id, 0, 'e1', [1500])

    assert out['candidates'] == 1
    assert out['delivered'] == 1
    offers = [m for g, m in channel_recorder if m.get('type') == 'ride_request']
    assert len(offers) == 1
    assert offers[0]['trip_id'] == searching_trip.id
    assert (f'driver_{geo_driver.id}', offers[0]) in [
        (g, m) for g, m in channel_recorder if m.get('type') == 'ride_request'
    ]


@pytest.mark.django_db
def test_wave_preserves_vehicle_type_geo_isolation(
    searching_trip, geo_driver, channel_recorder, captured_enqueues, db,
):
    """A bike parked on the pickup must not receive a sedan request."""
    bike_type, _ = VehicleType.objects.get_or_create(type='bike')
    bike_user = User.objects.create_user(phone_number='+919200000777', role='driver')
    bike_driver = Driver.objects.create(user_id=bike_user, approved=True)
    Vehicle.objects.create(
        driver_id=bike_driver, vehicle_type_id=bike_type, vehicle_number='TS09ZZ9999',
    )
    bike_key = rc.geo_key_for('bike')
    bike_member = f'driver:{bike_driver.id}:bike'
    rc.redis_client.geoadd(bike_key, [float(PICKUP_LNG), float(PICKUP_LAT), bike_member])
    rc.redis_client.setex(f'{rc.HEARTBEAT_PREFIX}{bike_driver.id}', 60, '1')
    try:
        out = dispatch_mod.execute_wave(searching_trip.id, 0, 'e1', [1500])
        offered_groups = {g for g, m in channel_recorder if m.get('type') == 'ride_request'}
        assert f'driver_{geo_driver.id}' in offered_groups
        assert f'driver_{bike_driver.id}' not in offered_groups
        assert out['candidates'] == 1
    finally:
        rc.redis_client.zrem(bike_key, bike_member)
        rc.redis_client.delete(f'{rc.HEARTBEAT_PREFIX}{bike_driver.id}')


# ---------------------------------------------------------------------------
# 3-4. Socket and process lifetime are irrelevant
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_rider_disconnect_does_not_cancel_dispatch(rider, vehicle_type, geo_driver):
    """The acceptance scenario, steps 1-8.

    Rider requests a ride, the socket goes away, and the queued wave still
    executes against a trip that is still searching. Previously `disconnect()`
    called `self._dispatch_task.cancel()` and waves 2 and 3 never ran.
    """
    from asgiref.sync import sync_to_async
    from channels.testing import WebsocketCommunicator

    from servers.consumers import RideRequestConsumer
    from servers.ride import tasks as tasks_mod

    enqueued = []

    def _capture(args=None, **kwargs):
        enqueued.append({'args': args, 'countdown': kwargs.get('countdown')})

    comm = WebsocketCommunicator(RideRequestConsumer.as_asgi(), '/ws/ride/request/')
    comm.scope['user'] = rider

    with mock.patch.object(tasks_mod.dispatch_wave, 'apply_async', _capture):
        connected, _ = await comm.connect()
        assert connected
        await comm.receive_json_from()  # connection_established

        await comm.send_json_to({
            'pickup_lat': str(PICKUP_LAT), 'pickup_lng': str(PICKUP_LNG),
            'destination_lat': '17.4500', 'destination_lng': '78.4000',
            'pickup_address': 'Pickup', 'destination_address': 'Drop',
            'distance_km': 5, 'duration_min': 12,
            'vehicle_type': VEHICLE_TYPE, 'payment_method': 'cash',
        })

        # Drain until the trip is confirmed created.
        trip_id = None
        for _ in range(6):
            msg = await comm.receive_json_from(timeout=5)
            if msg.get('type') == 'trip_created':
                trip_id = msg['trip_id']
                break
        assert trip_id is not None, 'trip_created never arrived'

        # trip_created is sent before dispatch is handed off, so let the
        # consumer finish its receive() before asserting on the enqueue.
        await comm.receive_nothing(timeout=1.0)

        # Wave 0 was handed to Celery, not run on the socket.
        assert len(enqueued) == 1
        assert enqueued[0]['args'][0] == trip_id
        assert enqueued[0]['args'][1] == 0

        # The rider vanishes. This used to kill the search.
        await comm.disconnect()

    # The consumer is gone. The queued wave still runs and the trip is still
    # searching, so waves 2 and 3 would follow.
    _, wave_index, epoch, radii = enqueued[0]['args']
    state = await sync_to_async(dispatch_mod.load_dispatch_state)(trip_id)
    assert state.dispatchable is True

    out = await sync_to_async(dispatch_mod.execute_wave)(trip_id, wave_index, epoch, radii)
    assert out['executed'] is True


@pytest.mark.django_db
def test_wave_runs_with_no_consumer_in_existence(
    searching_trip, geo_driver, channel_recorder, captured_enqueues,
):
    """Steps 5-8 of the acceptance scenario: nothing about the wave touches a
    consumer instance, so the original Daphne process can be gone."""
    out = dispatch_mod.execute_wave(searching_trip.id, 1, 'e1', [1500, 3000, 5000])
    assert out['executed'] is True
    assert out['radius'] == 3000


# ---------------------------------------------------------------------------
# 5-7. Stale waves no-op against authoritative state
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@pytest.mark.parametrize('wave_index', [1, 2])
def test_wave_noops_after_driver_accepted(
    searching_trip, geo_driver, driver, channel_recorder, captured_enqueues, wave_index,
):
    searching_trip.driver_id = driver
    _set_status(searching_trip, 'accepted')
    searching_trip.save(update_fields=['driver_id'])

    before = _trip_fingerprint(searching_trip.id)
    out = dispatch_mod.execute_wave(searching_trip.id, wave_index, 'e1', [1500, 3000, 5000])

    assert out == {'executed': False, 'reason': 'already_accepted'}
    assert channel_recorder == []
    assert captured_enqueues == []
    assert _trip_fingerprint(searching_trip.id) == before


@pytest.mark.django_db
@pytest.mark.parametrize('status', ['cancelled', 'completed', 'in_progress'])
def test_wave_noops_on_terminal_or_advanced_status(
    searching_trip, geo_driver, channel_recorder, captured_enqueues, status,
):
    _set_status(searching_trip, status)
    before = _trip_fingerprint(searching_trip.id)

    out = dispatch_mod.execute_wave(searching_trip.id, 1, 'e1', [1500, 3000, 5000])

    assert out['executed'] is False
    assert out['reason'] == f'status_{status}'
    assert channel_recorder == []
    assert captured_enqueues == []
    assert _trip_fingerprint(searching_trip.id) == before


@pytest.mark.django_db
def test_wave_noops_for_deleted_trip(channel_recorder, captured_enqueues):
    out = dispatch_mod.execute_wave(9_999_999, 0, 'e1', [1500])
    assert out == {'executed': False, 'reason': 'trip_missing'}
    assert channel_recorder == []


# ---------------------------------------------------------------------------
# 8-9. Business idempotency vs notification de-duplication
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_duplicate_wave_execution_is_business_idempotent(
    searching_trip, geo_driver, channel_recorder, captured_enqueues,
):
    """At-least-once delivery: running the identical wave twice must not
    change one durable field."""
    before = _trip_fingerprint(searching_trip.id)

    first = dispatch_mod.execute_wave(searching_trip.id, 0, 'same-epoch', [1500])
    second = dispatch_mod.execute_wave(searching_trip.id, 0, 'same-epoch', [1500])

    assert _trip_fingerprint(searching_trip.id) == before
    assert Trip.objects.get(id=searching_trip.id).driver_id_id is None
    assert first['candidates'] == 1
    # Notification de-duplication is the Redis-backed half: the second run
    # finds the driver already offered within this generation.
    assert second['candidates'] == 0


@pytest.mark.django_db
def test_redis_offer_state_loss_cannot_corrupt_trip(
    searching_trip, geo_driver, channel_recorder, captured_enqueues,
):
    """Flushing the dedupe sets may duplicate a notification. It must not
    touch the lifecycle."""
    before = _trip_fingerprint(searching_trip.id)

    dispatch_mod.execute_wave(searching_trip.id, 0, 'gen-1', [1500])
    rc.clear_generation_offers(searching_trip.id, 'gen-1')
    rc.clear_offered_drivers(searching_trip.id)
    again = dispatch_mod.execute_wave(searching_trip.id, 0, 'gen-1', [1500])

    # Duplicate offer — acceptable, and explicitly allowed.
    assert again['candidates'] == 1
    # Lifecycle untouched — mandatory.
    assert _trip_fingerprint(searching_trip.id) == before
    assert Trip.objects.get(id=searching_trip.id).driver_id_id is None


@pytest.mark.django_db
def test_wave_survives_redis_geo_unavailable(
    searching_trip, channel_recorder, captured_enqueues,
):
    before = _trip_fingerprint(searching_trip.id)
    with mock.patch.object(rc, 'nearby_drivers', side_effect=RuntimeError('redis down')):
        out = dispatch_mod.execute_wave(searching_trip.id, 0, 'e1', [1500, 3000])
    assert out['executed'] is True
    assert out['candidates'] == 0
    assert _trip_fingerprint(searching_trip.id) == before
    # Still hands over to the next wave rather than stranding the trip.
    assert len(captured_enqueues) == 1


# ---------------------------------------------------------------------------
# 10. Notification failure must not corrupt dispatch
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_fcm_enqueue_failure_does_not_break_dispatch(
    searching_trip, geo_driver, channel_recorder, captured_enqueues,
):
    from servers.auth_user import services as svc

    before = _trip_fingerprint(searching_trip.id)
    with mock.patch.object(
        svc.send_push_notification_task, 'delay', side_effect=RuntimeError('broker down'),
    ):
        out = dispatch_mod.execute_wave(searching_trip.id, 0, 'e1', [1500, 3000])

    assert out['executed'] is True
    assert out['delivered'] == 1        # socket offer still went out
    assert out['pushes_queued'] == 0    # push failed, swallowed
    assert _trip_fingerprint(searching_trip.id) == before
    assert len(captured_enqueues) == 1  # successor still scheduled


@pytest.mark.django_db
def test_offer_pushes_use_one_bulk_query(
    searching_trip, geo_driver, channel_recorder, captured_enqueues,
    django_assert_max_num_queries,
):
    """The old loop issued Driver.objects.get() per driver per wave."""
    with django_assert_max_num_queries(6):
        dispatch_mod.execute_wave(searching_trip.id, 0, 'e1', [1500])


# ---------------------------------------------------------------------------
# 11-12. Retry uses the same engine; generations cannot interfere
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_retry_goes_through_the_same_celery_entry_point(searching_trip, captured_enqueues):
    result = dispatch_mod.start_dispatch(
        searching_trip.id, radius=4000, reason='retry',
    )
    assert result['enqueued'] is True
    # An explicit radius means a single wave, as `_handle_retry` did before.
    assert result['radii'] == [4000]
    trip_id, wave_index, epoch, radii = captured_enqueues[0]['args']
    assert (trip_id, wave_index, radii) == (searching_trip.id, 0, [4000])


@pytest.mark.django_db
def test_retry_radius_is_capped(searching_trip, captured_enqueues):
    assert dispatch_mod.start_dispatch(
        searching_trip.id, radius=99999, reason='retry',
    )['radii'] == [dispatch_mod.MAX_WAVE_RADIUS_M]


@pytest.mark.django_db
def test_old_generation_cannot_interfere_with_a_new_one(
    searching_trip, geo_driver, channel_recorder, captured_enqueues,
):
    """A wave left over from an abandoned search can only re-offer; it can
    neither suppress the new generation nor alter the lifecycle."""
    before = _trip_fingerprint(searching_trip.id)

    # Generation A offers the driver, then the rider retries -> generation B.
    dispatch_mod.execute_wave(searching_trip.id, 0, 'gen-A', [1500])
    gen_b = dispatch_mod.execute_wave(searching_trip.id, 0, 'gen-B', [2000])
    # B re-reaches the driver: dedupe is scoped per generation, which is what
    # the old implementation did with a fresh in-process `offered` set.
    assert gen_b['candidates'] == 1

    # Now A's stale successor finally runs. It must not disturb anything.
    stale = dispatch_mod.execute_wave(searching_trip.id, 0, 'gen-A', [1500])
    assert stale['candidates'] == 0
    assert _trip_fingerprint(searching_trip.id) == before


@pytest.mark.django_db
def test_epoch_is_not_consulted_for_eligibility(searching_trip, geo_driver, channel_recorder, captured_enqueues):
    """The stated invariant: eligibility comes from PostgreSQL alone.

    The same trip, two unrelated epochs, identical eligibility verdict.
    """
    state = dispatch_mod.load_dispatch_state(searching_trip.id)
    assert state.dispatchable is True
    for epoch in ('anything', 'else-entirely'):
        assert dispatch_mod.execute_wave(
            searching_trip.id, 0, epoch, [1500],
        )['executed'] is True


# ---------------------------------------------------------------------------
# 13-16. One offer-dismissal mechanism, shared by every terminal transition
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_dismiss_outstanding_offers_notifies_and_is_idempotent(
    searching_trip, channel_recorder,
):
    rc.add_offered_drivers(searching_trip.id, ['501', '502'])

    first = dispatch_mod.dismiss_outstanding_offers(searching_trip.id, reason='test')
    assert sorted(first) == ['501', '502']
    assert sorted(g for g, _ in channel_recorder) == ['driver_501', 'driver_502']
    assert all(m['type'] == 'trip_taken' for _, m in channel_recorder)

    channel_recorder.clear()
    # Safe to call twice — the set was popped, so nothing is re-notified.
    assert dispatch_mod.dismiss_outstanding_offers(searching_trip.id, reason='test') == []
    assert channel_recorder == []


@pytest.mark.django_db
def test_dismiss_excludes_the_winning_driver(searching_trip, channel_recorder):
    rc.add_offered_drivers(searching_trip.id, ['601', '602', '603'])
    notified = dispatch_mod.dismiss_outstanding_offers(
        searching_trip.id, reason='accepted', exclude_driver_id=602,
    )
    assert sorted(notified) == ['601', '603']


@pytest.mark.django_db
def test_dismiss_is_safe_when_offer_set_already_gone(searching_trip, channel_recorder):
    rc.clear_offered_drivers(searching_trip.id)
    assert dispatch_mod.dismiss_outstanding_offers(searching_trip.id, reason='x') == []


@pytest.mark.django_db
def test_dismiss_never_touches_the_trip_lifecycle(searching_trip, channel_recorder):
    rc.add_offered_drivers(searching_trip.id, ['701'])
    before = _trip_fingerprint(searching_trip.id)
    dispatch_mod.dismiss_outstanding_offers(searching_trip.id, reason='x')
    assert _trip_fingerprint(searching_trip.id) == before


@pytest.mark.django_db
def test_rest_rider_cancel_dismisses_outstanding_offers(
    searching_trip, rider, channel_recorder,
):
    """This path previously left every candidate holding a live card."""
    from rest_framework.test import APIClient
    from rest_framework_simplejwt.tokens import AccessToken

    rc.add_offered_drivers(searching_trip.id, ['801', '802'])

    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(rider)}')
    resp = client.post(
        f'/api/v1/ride/trip/{searching_trip.id}/cancel/',
        {'reason': 'other'}, format='json',
    )
    assert resp.status_code in (200, 201), resp.content

    assert Trip.objects.get(id=searching_trip.id).cancelled_by == 'rider'
    notified = sorted(g for g, m in channel_recorder if m.get('type') == 'trip_taken')
    assert notified == ['driver_801', 'driver_802']
    assert rc.pop_offered_drivers(searching_trip.id) == []


@pytest.mark.django_db
def test_auto_cancel_timeout_dismisses_outstanding_offers(
    searching_trip, channel_recorder,
):
    from servers.ride.tasks import auto_cancel_trip

    rc.add_offered_drivers(searching_trip.id, ['901'])
    auto_cancel_trip(searching_trip.id)

    trip = Trip.objects.select_related('status_id').get(id=searching_trip.id)
    assert trip.status_id.status_code == 'cancelled'
    assert trip.cancellation_reason == 'no_driver_accepted'
    assert [g for g, m in channel_recorder if m.get('type') == 'trip_taken'] == ['driver_901']


# ---------------------------------------------------------------------------
# 21-22. Races: the losing side must re-read committed state
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_timeout_cannot_overwrite_an_accepted_trip(searching_trip, driver, channel_recorder):
    """Acceptance at ~90s vs auto-cancel at ~90s.

    Reproduces the exact interleaving that used to corrupt state: the task
    holds a trip instance loaded *before* acceptance, then acceptance commits,
    then the task proceeds. The old implementation called a bare
    `trip.save()`, writing every column from that stale instance — reverting
    the acceptance and clearing `driver_id` back to NULL.
    """
    from servers.ride.tasks import auto_cancel_trip

    # The task's pre-acceptance view of the world.
    stale = Trip.objects.select_related('status_id').get(id=searching_trip.id)
    assert stale.driver_id_id is None

    # A driver accepts and commits.
    searching_trip.driver_id = driver
    searching_trip.accepted_at = timezone.now()
    _set_status(searching_trip, 'accepted')
    searching_trip.save(update_fields=['driver_id', 'accepted_at'])

    # Timeout now fires. It must re-read and stand down.
    result = auto_cancel_trip(searching_trip.id)

    trip = Trip.objects.select_related('status_id').get(id=searching_trip.id)
    assert 'already accepted' in result
    assert trip.driver_id_id == driver.id, 'timeout cleared an assigned driver'
    assert trip.status_id.status_code == 'accepted'
    assert trip.cancelled_at is None
    assert trip.cancelled_by in ('', None)


@pytest.mark.django_db
def test_timeout_still_cancels_a_genuinely_unaccepted_trip(searching_trip, channel_recorder):
    from servers.ride.tasks import auto_cancel_trip

    auto_cancel_trip(searching_trip.id)
    trip = Trip.objects.select_related('status_id').get(id=searching_trip.id)
    assert trip.status_id.status_code == 'cancelled'
    assert trip.cancelled_by == 'system'


@pytest.mark.django_db
def test_timeout_is_idempotent(searching_trip, channel_recorder):
    """Duplicate delivery of the timeout must not restate the cancellation."""
    from servers.ride.tasks import auto_cancel_trip

    auto_cancel_trip(searching_trip.id)
    first = Trip.objects.get(id=searching_trip.id).cancelled_at
    auto_cancel_trip(searching_trip.id)
    assert Trip.objects.get(id=searching_trip.id).cancelled_at == first


@pytest.mark.django_db
def test_cancelled_trip_cannot_then_be_dispatched(
    searching_trip, geo_driver, channel_recorder, captured_enqueues,
):
    """Rider cancels, then a queued wave runs: existing semantics say the
    committed cancellation wins and the wave simply stops."""
    from servers.ride.tasks import auto_cancel_trip

    auto_cancel_trip(searching_trip.id)
    channel_recorder.clear()

    out = dispatch_mod.execute_wave(searching_trip.id, 1, 'e1', [1500, 3000])
    assert out['reason'] == 'status_cancelled'
    assert [m for _, m in channel_recorder if m.get('type') == 'ride_request'] == []


# ---------------------------------------------------------------------------
# 17-20. Reconnect snapshot and authorization
# ---------------------------------------------------------------------------

@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_rider_reconnect_while_searching_receives_current_state(searching_trip, rider):
    from channels.testing import WebsocketCommunicator

    from servers.consumers import RideRequestConsumer

    comm = WebsocketCommunicator(RideRequestConsumer.as_asgi(), '/ws/ride/request/')
    comm.scope['user'] = rider
    connected, _ = await comm.connect()
    assert connected
    assert (await comm.receive_json_from())['type'] == 'connection_established'

    snapshot = await comm.receive_json_from()
    assert snapshot['type'] == 'current_trip'
    assert snapshot['trip']['id'] == searching_trip.id
    assert snapshot['trip']['status'] == 'requested'
    await comm.disconnect()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_rider_reconnect_after_acceptance_sees_accepted_trip(searching_trip, rider, driver):
    """The acceptance frame was sent to a socket that no longer existed, so
    reconnect must reconstruct state from PostgreSQL."""
    from asgiref.sync import sync_to_async
    from channels.testing import WebsocketCommunicator

    from servers.consumers import RideRequestConsumer

    @sync_to_async
    def _accept():
        searching_trip.driver_id = driver
        searching_trip.accepted_at = timezone.now()
        _set_status(searching_trip, 'accepted')
        searching_trip.save(update_fields=['driver_id', 'accepted_at'])

    await _accept()

    comm = WebsocketCommunicator(RideRequestConsumer.as_asgi(), '/ws/ride/request/')
    comm.scope['user'] = rider
    connected, _ = await comm.connect()
    assert connected
    await comm.receive_json_from()  # connection_established

    snapshot = await comm.receive_json_from()
    assert snapshot['type'] == 'current_trip'
    assert snapshot['trip']['id'] == searching_trip.id
    assert snapshot['trip']['status'] == 'accepted'
    assert snapshot['trip']['driver_name']
    # Driver PII beyond a display name must not ride along.
    assert 'phone_number' not in snapshot['trip']
    await comm.disconnect()


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_reconnect_never_leaks_another_riders_trip(searching_trip, other_rider):
    from channels.testing import WebsocketCommunicator

    from servers.consumers import RideRequestConsumer

    comm = WebsocketCommunicator(RideRequestConsumer.as_asgi(), '/ws/ride/request/')
    comm.scope['user'] = other_rider
    connected, _ = await comm.connect()
    assert connected
    assert (await comm.receive_json_from())['type'] == 'connection_established'
    # No trip of their own -> no snapshot at all.
    assert await comm.receive_nothing(timeout=0.3) is True
    await comm.disconnect()


@pytest.mark.django_db
def test_snapshot_withholds_otp_from_everyone_but_the_rider(searching_trip, rider, other_rider, driver):
    """Reuses TripDetailSerializer precisely so this rule is not re-invented."""
    from servers.ride.serializers import TripDetailSerializer

    searching_trip.driver_id = driver
    searching_trip.otp = '123456'
    _set_status(searching_trip, 'accepted')
    searching_trip.save(update_fields=['driver_id', 'otp'])

    class _Ctx:
        def __init__(self, user):
            self.user = user

    own = TripDetailSerializer(searching_trip, context={'request': _Ctx(rider)}).data
    foreign = TripDetailSerializer(searching_trip, context={'request': _Ctx(other_rider)}).data
    assert own['otp'] == '123456'
    assert foreign['otp'] is None
