"""Driver exclusivity must survive Redis being unavailable.

`test_driver_active_trip_contention.py` proves the row lock serialises two
concurrent acceptances — but it mocks Redis into *succeeding*
(`set_driver_active_trip` returns True). So it proves the lock works when the
coordination layer is healthy, which is the easy case.

This file removes that assumption. Redis is made genuinely unavailable: every
call on the client raises, exactly as an unreachable Redis does. That matters
because Redis is where "is this driver busy" is cached, and a previous defect in
this codebase read a Redis failure as "the driver is free" — the precise
condition under which a second acceptance would be allowed through.

The claim under test:

    PostgreSQL alone is sufficient. `driver_active_trip_ids` queries the Trip
    table, and `_accept_trip` takes `select_for_update` on the Driver row. With
    Redis dead, two riders' acceptances for one driver must still produce
    EXACTLY ONE assignment, and the loser must be refused deterministically.

Asserted on committed database state rather than on return values, because the
invariant is about the database. A raised exception is an acceptable outcome for
the loser; a second assignment is not.

Real threads, real PostgreSQL, real blocking. Marked `postgres` because
SELECT FOR UPDATE is a no-op on SQLite, so no SQLite run could demonstrate this.
Contention is forced rather than hoped for: the first thread to reach the guard
pins the Driver row while the second runs, and the second thread's wall-clock
duration is asserted to reflect that it genuinely blocked in the database.
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
from servers.ride.models import Trip, TripStatus

User = get_user_model()

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.django_db(transaction=True),
]

PICKUP_LAT = Decimal('17.4450')
PICKUP_LNG = Decimal('78.3800')
LOCK_HOLD_SECONDS = 1.5
JOIN_TIMEOUT_SECONDS = 30


def _require_postgres():
    if connection.vendor != 'postgresql':
        pytest.skip(
            f'needs PostgreSQL, got {connection.vendor!r}. SELECT FOR UPDATE is a '
            'no-op on SQLite, so this proves nothing there.'
        )


class _DeadRedis:
    """A Redis client that is unreachable.

    Every attribute access returns a callable that raises, so it fails the way a
    real client does regardless of which method the code reaches for. Deliberately
    not a Mock: a Mock would silently succeed and the test would prove the
    opposite of what it claims.
    """

    def __getattr__(self, name):
        def _boom(*args, **kwargs):
            raise ConnectionError(
                f'Error 111 connecting to redis:6379. Connection refused. ({name})'
            )
        return _boom


def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


def _requested_trip(rider, vehicle_type):
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
    """Run the production `_accept_trip` body.

    Deliberately applies NO patches of its own. This runs inside a worker thread,
    and `mock.patch` on a module global is not thread-safe: with two threads
    entering overlapping context managers, the second saves the FIRST thread's
    stub as "the original" and restores the stub on exit, leaving the module
    permanently patched. That leaked _DeadRedis into later tests in the same
    process and broke five unrelated PostgreSQL tests.

    Redis is patched once, by the calling thread, around the whole concurrent
    block -- see _run_concurrent_acceptances.
    """
    consumer = TripStatusConsumer()
    consumer.trip_id = trip_id
    consumer.user = driver_user
    raw = TripStatusConsumer.__dict__['_accept_trip'].func
    return raw(consumer)


@pytest.fixture
def two_riders_one_driver(db):
    """Driver D, available. Riders R1 and R2, each with a requested trip."""
    _require_postgres()
    r1 = User.objects.create_user(phone_number='+919511000001', role='rider')
    r2 = User.objects.create_user(phone_number='+919511000002', role='rider')
    duser = User.objects.create_user(phone_number='+919611000001', role='driver')
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    driver = Driver.objects.create(user_id=duser, approved=True, status='online')
    vehicle = Vehicle.objects.create(
        driver_id=driver, vehicle_type_id=vt, vehicle_number='TS09DD2222',
    )
    driver.active_vehicle = vehicle
    driver.save(update_fields=['active_vehicle'])

    yield {
        'driver': driver,
        'driver_user': duser,
        'trip_r1': _requested_trip(r1, vt),
        'trip_r2': _requested_trip(r2, vt),
    }


def _run_concurrent_acceptances(driver_user, trip_one_id, trip_two_id):
    """Two threads accept two trips for one driver, second forced to block.

    Returns (results, errors, durations).
    """
    results, errors, durations = {}, {}, {}
    first_arrival = threading.Event()
    real_guard = ride_models.driver_active_trip_ids
    gate_lock = threading.Lock()
    gate_state = {'held': False}

    def guard_with_hold(drv, exclude_trip_id=None):
        # Reached only after select_for_update on the Driver row has been granted,
        # so sleeping here keeps that row locked and forces the other transaction
        # to block in PostgreSQL rather than race us in Python.
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
                assert first_arrival.wait(timeout=10), \
                    'first thread never reached the guard'
            started = time.monotonic()
            results[name] = _accept_raw(trip_id, driver_user)
            durations[name] = time.monotonic() - started
        except BaseException as exc:  # noqa: BLE001
            errors[name] = exc
            durations[name] = time.monotonic() - started
        finally:
            connections.close_all()

    # One patch, applied by one thread, spanning both workers. The Redis-dependent
    # post-commit side effects are NOT stubbed -- whether they fail, and what that
    # does to the decision, is the thing under examination. Only the non-Redis
    # push notification is stubbed.
    with mock.patch.object(rc, 'redis_client', _DeadRedis()), \
            mock.patch('servers.auth_user.services.send_push_notification',
                       return_value=True), \
            mock.patch.object(ride_models, 'driver_active_trip_ids',
                              guard_with_hold):
        t1 = threading.Thread(target=worker, args=('R1', trip_one_id, False),
                              daemon=True)
        t2 = threading.Thread(target=worker, args=('R2', trip_two_id, True),
                              daemon=True)
        t1.start()
        t2.start()
        for t in (t1, t2):
            t.join(timeout=JOIN_TIMEOUT_SECONDS)
            assert not t.is_alive(), (
                f'thread did not finish within {JOIN_TIMEOUT_SECONDS}s -- likely a '
                'deadlock or an unreleased lock'
            )

    return results, errors, durations


# ---------------------------------------------------------------------------
# The mandatory proof
# ---------------------------------------------------------------------------

def test_exactly_one_acceptance_wins_with_redis_unavailable(two_riders_one_driver):
    """One driver, two riders, Redis dead. Exactly one assignment may exist.

    This is the invariant the whole dispatch design rests on, in the condition
    where its cache is gone.
    """
    s = two_riders_one_driver
    driver, r1, r2 = s['driver'], s['trip_r1'], s['trip_r2']

    results, errors, durations = _run_concurrent_acceptances(
        s['driver_user'], r1.id, r2.id,
    )

    r1.refresh_from_db()
    r2.refresh_from_db()
    assigned = [t for t in (r1, r2) if t.driver_id_id == driver.id]

    assert len(assigned) == 1, (
        f'{len(assigned)} trips hold driver {driver.id} with Redis unavailable. '
        f'results={results} errors={ {k: type(v).__name__ for k, v in errors.items()} }'
    )

    # And the database agrees, queried independently of the two trips above.
    held = Trip.objects.filter(
        driver_id=driver,
        status_id__status_code__in=('accepted', 'reached', 'in_progress'),
    ).count()
    assert held == 1, f'driver {driver.id} is on {held} active trips'


def test_the_loser_is_refused_deterministically(two_riders_one_driver):
    """The second acceptance must be refused, not left ambiguous.

    A driver whose tap neither succeeds nor visibly fails will tap again.
    """
    s = two_riders_one_driver
    results, errors, _ = _run_concurrent_acceptances(
        s['driver_user'], s['trip_r1'].id, s['trip_r2'].id,
    )

    s['trip_r1'].refresh_from_db()
    s['trip_r2'].refresh_from_db()
    loser = 'R2' if s['trip_r1'].driver_id_id else 'R1'

    outcome = results.get(loser)
    raised = errors.get(loser)
    assert outcome is not None or raised is not None, (
        f'{loser} produced neither a result nor an exception; the driver would '
        'not know what happened'
    )
    if outcome is not None:
        # A refusal, however it is spelled, must not read as success.
        assert outcome is not True, (
            f'{loser} was told its acceptance succeeded while the other trip holds '
            'the driver'
        )


def test_the_second_transaction_really_blocked_in_postgresql(two_riders_one_driver):
    """Guards against a green result that came from thread-scheduling luck.

    If the second thread did not block on the Driver row, this test proves
    nothing about locking and should fail rather than pass quietly.
    """
    s = two_riders_one_driver
    _, _, durations = _run_concurrent_acceptances(
        s['driver_user'], s['trip_r1'].id, s['trip_r2'].id,
    )

    second = durations.get('R2')
    assert second is not None, 'the second thread recorded no duration'
    assert second >= LOCK_HOLD_SECONDS * 0.6, (
        f'second acceptance completed in {second:.2f}s while the first held the '
        f'Driver row for {LOCK_HOLD_SECONDS}s. It did not block, so row locking '
        'is not being exercised and this suite proves nothing.'
    )


def test_no_trip_is_left_in_a_half_assigned_state(two_riders_one_driver):
    """PostgreSQL truth must stay coherent even though Redis is gone.

    The loser's trip must remain cleanly `requested` and unassigned -- available
    for another driver -- rather than carrying a driver with a non-accepted
    status or an accepted status with no driver.
    """
    s = two_riders_one_driver
    _run_concurrent_acceptances(
        s['driver_user'], s['trip_r1'].id, s['trip_r2'].id,
    )

    for trip in (s['trip_r1'], s['trip_r2']):
        trip.refresh_from_db()
        code = trip.status_id.status_code
        has_driver = trip.driver_id_id is not None
        if code == 'requested':
            assert not has_driver, (
                f'trip {trip.id} is still requested but holds a driver'
            )
        elif code == 'accepted':
            assert has_driver, (
                f'trip {trip.id} is accepted with no driver assigned'
            )
        else:
            pytest.fail(
                f'trip {trip.id} ended in unexpected status {code!r} after a '
                'contended acceptance with Redis down'
            )


def test_redis_really_was_unavailable(two_riders_one_driver):
    """The negative control for the whole file.

    If the stub were accidentally permissive, every test above would pass while
    proving the healthy-Redis case that is already covered elsewhere.
    """
    dead = _DeadRedis()
    for call in (lambda: dead.get('k'),
                 lambda: dead.set('k', 'v'),
                 lambda: dead.zadd('k', {'m': 1}),
                 lambda: dead.xadd('s', {'f': 'v'})):
        with pytest.raises(ConnectionError):
            call()

    # And the production accessor must refuse to guess when it cannot read.
    from servers.redis_client import (
        DriverTripStateUnavailable, get_driver_active_trip,
    )
    with mock.patch.object(rc, 'redis_client', dead):
        with pytest.raises(DriverTripStateUnavailable):
            get_driver_active_trip(s_id := 12345)
        assert s_id  # keep the walrus honest for linters
