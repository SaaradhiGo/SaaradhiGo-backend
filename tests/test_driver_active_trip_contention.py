"""Real PostgreSQL row-lock contention for the one-driver-one-trip invariant.

PR 2 established the limitation this file exists to remove: the default suite
runs on SQLite, where `SELECT FOR UPDATE` is a no-op, so no SQLite test can
demonstrate that two concurrent acceptances actually serialise. The sibling
file `test_driver_active_trip_invariant.py` proves the *logic* on SQLite; this
one proves the *locking* on PostgreSQL.

Marked `postgres` and excluded from the default run by `pytest.ini`. The
dedicated CI job executes `pytest -m postgres` against a PostgreSQL service
container.

What is being proven
--------------------
`_accept_trip` locks the Trip row, then the Driver row. Two drivers accepting
two *different* trips never contend on Trip (different rows) — they contend on
the single Driver row. The claim is:

    A locks Trip 100          B locks Trip 200
    A locks Driver 5          B waits for Driver 5
    A assigns, commits, releases Driver 5
                              B acquires Driver 5
                              B queries active trips, sees Trip 100
                              B rejects

Contention is forced, not hoped for. The first thread to reach the guard —
which is *after* it has taken the Driver lock — holds that lock for
`LOCK_HOLD_SECONDS` while the second thread runs. The second thread therefore
blocks inside `select_for_update` on the Driver row, and the test asserts its
wall-clock duration reflects that. Without genuine blocking that assertion
fails, so a green result cannot come from thread-scheduling luck.
"""

import threading
import time
from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.db import connection, connections

import servers.redis_client as rc
from servers.consumers import TripStatusConsumer
from servers.driver.models import Driver, Vehicle, VehicleType
from servers.ride import models as ride_models
from servers.ride.models import Trip, TripStatus, driver_active_trip_ids

User = get_user_model()

pytestmark = [
    pytest.mark.postgres,
    # transaction=True gives each thread a real committed view of the database,
    # which is the entire point — the default wrapping transaction would hide
    # the winner's commit from the loser.
    pytest.mark.django_db(transaction=True),
]

PICKUP_LAT = Decimal('17.4450')
PICKUP_LNG = Decimal('78.3800')

# How long the winner pins the Driver row while the loser is running. Long
# enough to be unambiguous against scheduler noise, short enough for CI.
LOCK_HOLD_SECONDS = 1.5
# A deadlock or a lock never released must fail the test, not hang the job.
JOIN_TIMEOUT_SECONDS = 30


def _require_postgres():
    if connection.vendor != 'postgresql':
        pytest.skip(
            f'needs PostgreSQL, got {connection.vendor!r}. Set DB_HOST/DB_NAME/'
            'DB_USER/DB_PASSWORD and DB_SSLMODE=disable, or run the postgres CI job.'
        )


def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


def _make_requested_trip(rider, vehicle_type):
    return Trip.objects.create(
        user_id=rider,
        status_id=_status('requested'),
        requested_vehicle_type=vehicle_type,
        pickup_lat=PICKUP_LAT,
        pickup_long=PICKUP_LNG,
        destination_lat=Decimal('17.4500'),
        destination_long=Decimal('78.4000'),
        pickup_address='Pickup',
        destination_address='Drop',
        estimated_fare=Decimal('150.00'),
    )


def _accept_raw(trip_id, driver_user):
    """Call the production `_accept_trip` body.

    `channels.db.DatabaseSyncToAsync` wraps it; attribute access yields a
    partial around the async wrapper, so the raw sync function comes off the
    class `__dict__`. Post-commit side effects (Redis, loser fanout, push) are
    stubbed — they follow the decision and are not the invariant under test.
    """
    consumer = TripStatusConsumer()
    consumer.trip_id = trip_id
    consumer.user = driver_user

    raw = TripStatusConsumer.__dict__['_accept_trip'].func
    with mock.patch.object(rc, 'set_driver_active_trip', return_value=True), \
            mock.patch.object(rc, 'remove_driver', return_value=True), \
            mock.patch.object(rc, 'cache_trip', return_value=True), \
            mock.patch('servers.ride.dispatch.dismiss_outstanding_offers', return_value=[]), \
            mock.patch('servers.auth_user.services.send_push_notification', return_value=True):
        return raw(consumer)


@pytest.fixture
def scenario(db):
    _require_postgres()
    rider = User.objects.create_user(phone_number='+919500000001', role='rider')
    duser = User.objects.create_user(phone_number='+919600000001', role='driver')
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    driver = Driver.objects.create(user_id=duser, approved=True, status='online')
    vehicle = Vehicle.objects.create(
        driver_id=driver, vehicle_type_id=vt, vehicle_number='TS09CC1111',
    )
    driver.active_vehicle = vehicle
    driver.save(update_fields=['active_vehicle'])

    trip_a = _make_requested_trip(rider, vt)
    trip_b = _make_requested_trip(rider, vt)

    yield {'driver': driver, 'driver_user': duser, 'trip_a': trip_a, 'trip_b': trip_b}

    if rc.redis_client is not None:
        try:
            rc.clear_driver_active_trip(driver.id)
        except Exception:  # noqa: BLE001
            pass


def test_two_concurrent_acceptances_for_one_driver_serialise(scenario):
    """Exactly one of two simultaneous acceptances may win."""
    driver = scenario['driver']
    driver_user = scenario['driver_user']
    trip_a, trip_b = scenario['trip_a'], scenario['trip_b']

    results = {}
    durations = {}
    errors = {}
    first_arrival = threading.Event()
    real_guard = ride_models.driver_active_trip_ids
    gate_lock = threading.Lock()
    gate_state = {'held': False}

    def guard_with_hold(drv, exclude_trip_id=None):
        """Patched guard: the first arrival pins the Driver row.

        Reached only *after* `select_for_update` on the Driver row has been
        granted, so sleeping here keeps that row locked for the duration and
        forces the other transaction to block in the database rather than
        racing us in Python.
        """
        with gate_lock:
            is_first = not gate_state['held']
            if is_first:
                gate_state['held'] = True
        if is_first:
            first_arrival.set()
            time.sleep(LOCK_HOLD_SECONDS)
        return real_guard(drv, exclude_trip_id=exclude_trip_id)

    def worker(name, trip_id, wait_for_first):
        try:
            if wait_for_first:
                # Start only once the other thread holds the Driver lock, so
                # this thread is guaranteed to arrive second and block on it.
                assert first_arrival.wait(timeout=10), 'first thread never reached the guard'
            started = time.monotonic()
            results[name] = _accept_raw(trip_id, driver_user)
            durations[name] = time.monotonic() - started
        except BaseException as exc:  # noqa: BLE001
            errors[name] = exc
        finally:
            connections.close_all()

    with mock.patch.object(ride_models, 'driver_active_trip_ids', guard_with_hold):
        threads = [
            threading.Thread(target=worker, args=('A', trip_a.id, False), daemon=True),
            threading.Thread(target=worker, args=('B', trip_b.id, True), daemon=True),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=JOIN_TIMEOUT_SECONDS)
            assert not t.is_alive(), (
                'thread did not finish within '
                f'{JOIN_TIMEOUT_SECONDS}s — likely a deadlock or an unreleased lock'
            )

    # No unexpected exception or deadlock.
    assert not errors, f'unexpected exceptions: {errors}'
    assert set(results) == {'A', 'B'}, f'missing results: {results}'

    winners = [n for n, r in results.items() if r.get('success') is True]
    losers = [n for n, r in results.items() if r.get('success') is not True]

    assert len(winners) == 1, f'expected exactly one winner, got {winners}: {results}'
    assert len(losers) == 1, f'expected exactly one loser, got {losers}: {results}'

    loser_name = losers[0]
    loser_result = results[loser_name]
    # The loser must be refused by the invariant, not by some other validation.
    assert loser_result.get('reason') == 'driver_already_on_trip', loser_result

    # Proof that the loser genuinely blocked on the Driver row rather than
    # merely running afterwards: it cannot have completed faster than the
    # winner held the lock.
    assert durations[loser_name] >= LOCK_HOLD_SECONDS * 0.6, (
        f'loser returned in {durations[loser_name]:.3f}s but the winner held the '
        f'Driver row for {LOCK_HOLD_SECONDS}s — it did not contend on the lock, '
        'so this run proves nothing about serialisation'
    )

    # Durable outcome: the driver is on exactly one trip.
    trip_a.refresh_from_db()
    trip_b.refresh_from_db()
    assigned = [t for t in (trip_a, trip_b) if t.driver_id_id == driver.id]
    unassigned = [t for t in (trip_a, trip_b) if t.driver_id_id is None]

    assert len(assigned) == 1, 'driver must end up on exactly one trip'
    assert len(unassigned) == 1

    won, lost = assigned[0], unassigned[0]
    assert won.status_id.status_code == 'accepted'
    assert won.otp is not None, 'winner must have an OTP'

    assert lost.status_id.status_code == 'requested', 'losing trip must stay requested'
    assert lost.driver_id_id is None, 'losing trip must have no driver'
    assert lost.otp is None, 'no OTP may be generated for the losing trip'
    assert lost.accepted_at is None

    assert driver_active_trip_ids(driver) == [won.id]


def test_loser_sees_the_committed_winner_not_a_stale_snapshot(scenario):
    """The READ COMMITTED half of the argument.

    The loser acquires the Driver lock only after the winner commits, so its
    guard query must observe the winner's assignment. If the loser read a
    snapshot taken before the winner committed it would see no active trip and
    wrongly proceed.
    """
    driver = scenario['driver']
    driver_user = scenario['driver_user']
    trip_a, trip_b = scenario['trip_a'], scenario['trip_b']

    observed = {}
    real_guard = ride_models.driver_active_trip_ids
    seen_first = threading.Event()
    state = {'first_done': False}

    def recording_guard(drv, exclude_trip_id=None):
        out = real_guard(drv, exclude_trip_id=exclude_trip_id)
        observed[threading.current_thread().name] = out
        return out

    def worker(name, trip_id, second):
        try:
            if second:
                assert seen_first.wait(timeout=15)
            _accept_raw(trip_id, driver_user)
            if not second:
                state['first_done'] = True
                seen_first.set()
        finally:
            connections.close_all()

    with mock.patch.object(ride_models, 'driver_active_trip_ids', recording_guard):
        t1 = threading.Thread(target=worker, args=('first', trip_a.id, False), name='first', daemon=True)
        t2 = threading.Thread(target=worker, args=('second', trip_b.id, True), name='second', daemon=True)
        t1.start(); t1.join(timeout=JOIN_TIMEOUT_SECONDS)
        assert not t1.is_alive()
        t2.start(); t2.join(timeout=JOIN_TIMEOUT_SECONDS)
        assert not t2.is_alive()

    assert state['first_done'] is True
    # The first thread saw a free driver; the second saw the committed trip.
    assert observed.get('first') == []
    assert observed.get('second') == [trip_a.id], observed

    trip_b.refresh_from_db()
    assert trip_b.driver_id_id is None
    assert trip_b.status_id.status_code == 'requested'


def test_select_for_update_is_actually_enforced(scenario):
    """Sanity check on the test environment itself.

    If this fails, the whole file is meaningless: the backend is not honouring
    row locks and every other assertion here would pass vacuously.
    """
    driver = scenario['driver']
    assert connection.vendor == 'postgresql'

    blocked_for = {}
    holder_ready = threading.Event()
    release = threading.Event()

    def holder():
        from django.db import transaction
        try:
            with transaction.atomic():
                Driver.objects.select_for_update().get(pk=driver.pk)
                holder_ready.set()
                release.wait(timeout=10)
        finally:
            connections.close_all()

    def waiter():
        from django.db import transaction
        try:
            assert holder_ready.wait(timeout=10)
            started = time.monotonic()
            with transaction.atomic():
                Driver.objects.select_for_update().get(pk=driver.pk)
            blocked_for['t'] = time.monotonic() - started
        finally:
            connections.close_all()

    th, tw = threading.Thread(target=holder, daemon=True), threading.Thread(target=waiter, daemon=True)
    th.start()
    tw.start()
    assert holder_ready.wait(timeout=10)
    time.sleep(1.0)
    release.set()
    th.join(timeout=JOIN_TIMEOUT_SECONDS)
    tw.join(timeout=JOIN_TIMEOUT_SECONDS)
    assert not th.is_alive() and not tw.is_alive()

    assert blocked_for.get('t', 0) >= 0.5, (
        f'second transaction acquired the Driver row lock in {blocked_for.get("t")}s; '
        'row locks are not being enforced, so the contention tests prove nothing'
    )
