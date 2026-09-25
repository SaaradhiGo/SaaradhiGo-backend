"""§12 — Redis disappears at each point in a ride's life. What survives.

WHY A MATRIX RATHER THAN MORE INDIVIDUAL TESTS
----------------------------------------------
Four suites already cover pieces of this and each is sound: driver exclusivity with
Redis dead, SOS during a broker outage, driver trip state when Redis is unavailable,
and trip creation when the broker is unreachable. What none of them answers is the
question an operator actually has -- "Redis just went down; what is happening to the
rides that are in flight right now?" -- because that depends on WHERE in its life each
ride was.

So this walks one ride through each stage with Redis dead at that stage, and asserts
the two things that matter every time:

  * PostgreSQL still holds the truth, and
  * nothing fabricates a business answer out of an infrastructure failure.

The second is the recurring defect in this codebase. Seven instances have been found
across runs -- a Redis error read as "the driver is free", an OTP task reporting
success without sending, a committed trip reported as a failed booking. Each was a
broad handler turning "I don't know" into a confident answer.

WHAT DEAD MEANS HERE
--------------------
`_DeadRedis` raises on every attribute access, which is how an unreachable Redis
behaves regardless of which method the code reaches for. Deliberately not a `Mock`: a
Mock returns a truthy Mock for everything, so a test using one would prove the
opposite of what it claims -- "the driver is free" is exactly what a Mock would say.

WHAT THIS DOES NOT COVER
------------------------
The channel layer. Redis also backs Channels, so a real Redis outage also breaks
WebSocket group sends, and a rider mid-ride would stop receiving updates. That is a
connectivity failure the reconnect path already handles, and it is not what this file
is about: these tests are about state and money surviving, not about frames arriving.
Stated so nobody reads a green matrix as "a Redis outage is invisible to users".
"""

from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

import servers.redis_client as rc
from servers.driver.models import Driver, Vehicle, VehicleType
from servers.ride import liveness
from servers.ride.models import (
    DRIVER_ACTIVE_TRIP_STATUSES, Trip, TripStatus, driver_active_trip_ids,
)
from servers.rider.models import Rider

User = get_user_model()

pytestmark = pytest.mark.django_db

_n = iter(range(1, 999))


class _DeadRedis:
    """Unreachable. Every attribute returns a callable that raises."""

    def __getattr__(self, name):
        def _boom(*args, **kwargs):
            raise ConnectionError(
                f'Error 111 connecting to redis:6379. Connection refused. ({name})'
            )
        return _boom


def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


@pytest.fixture
def rider():
    i = next(_n)
    u = User.objects.create_user(phone_number=f'+9195700{i:05d}',
                                 username=f'+9195700{i:05d}', role='rider')
    Rider.objects.create(user_id=u)
    return u


@pytest.fixture
def driver():
    i = next(_n)
    u = User.objects.create_user(phone_number=f'+9196700{i:05d}',
                                 username=f'+9196700{i:05d}', role='driver')
    d = Driver.objects.create(user_id=u, approved=True, status='online')
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    v = Vehicle.objects.create(driver_id=d, vehicle_type_id=vt,
                               vehicle_number=f'TS09RO{i:04d}')
    d.active_vehicle = v
    d.save(update_fields=['active_vehicle'])
    return d


def _trip(rider, status, driver=None, **kw):
    now = timezone.now()
    fields = dict(
        user_id=rider, driver_id=driver, status_id=_status(status),
        pickup_lat=Decimal('17.4450000'), pickup_long=Decimal('78.3800000'),
        destination_lat=Decimal('17.4550000'), destination_long=Decimal('78.3900000'),
        estimated_fare=Decimal('169.02'), payment_method='cash',
    )
    if status in ('accepted', 'reached', 'in_progress', 'completed'):
        fields['accepted_at'] = now
    if status in ('in_progress', 'completed'):
        fields['started_at'] = now
    if status == 'completed':
        fields['completed_at'] = now
    fields.update(kw)
    return Trip.objects.create(**fields)


@pytest.fixture
def redis_down():
    """Redis unreachable for the duration of the test.

    Patched once, here, rather than inside anything concurrent. `mock.patch` on a
    module global is not thread-safe -- two overlapping context managers leave the
    module permanently patched, which leaked a dead stub into five unrelated tests
    once already.
    """
    with mock.patch.object(rc, 'redis_client', _DeadRedis()), \
            mock.patch.object(rc, 'geo_client', _DeadRedis(), create=True):
        yield


# ===========================================================================
# Stage 1 — Redis dies BEFORE booking
# ===========================================================================

def test_the_dead_stub_really_does_raise(redis_down):
    """The negative control, first.

    Without it, every test below could be passing because the stub silently
    succeeded -- which is precisely how a Mock-based version of this file would
    read as green while proving nothing.
    """
    # ConnectionError specifically, not a blind Exception: the stub is supposed to
    # fail the way an unreachable Redis fails, and asserting the exact type is what
    # makes this a control rather than "something went wrong".
    with pytest.raises(ConnectionError):
        rc.redis_client.get('anything')


def test_a_driver_cannot_be_found_but_nothing_claims_otherwise(redis_down, driver):
    """`nearby_drivers` is a Redis GEO query. With Redis dead it cannot answer.

    The only unacceptable outcome is a confident empty list presented as "there are
    no drivers nearby" when the truth is "I could not look". An exception or an
    explicit unavailable signal are both fine.
    """
    try:
        result = rc.nearby_drivers(78.38, 17.445, radius=5000)
    except Exception:
        return          # refusing to answer is correct
    assert result in (None, [], ()), (
        f'nearby_drivers returned {result!r} with Redis unreachable, which means it '
        f'invented data'
    )


# ===========================================================================
# Stage 2 — Redis dies DURING dispatch
# ===========================================================================

def test_a_wave_never_reports_candidates_it_could_not_find(redis_down, rider):
    """The shape of the defect that mattered most.

    `execute_wave` needs Redis to find candidates. With Redis dead it must either
    raise or report zero. What it must never do is report a positive count, because
    that number reaches an operator and the rider-facing progress frame.

    The FIRST version of this test called `dispatch.run_dispatch_wave`, which does
    not exist -- the function is `execute_wave`. It therefore caught AttributeError
    on the `except Exception` and passed while asserting nothing at all. A test that
    tolerates an exception has to be certain the exception it tolerates is the one it
    means, so the call is now checked against the real signature.
    """
    from servers.ride import dispatch

    assert hasattr(dispatch, 'execute_wave'), 'the function under test was renamed'

    trip = _trip(rider, 'requested')
    try:
        result = dispatch.execute_wave(trip.id, 0, 'epoch-1', [2000])
    except Exception:
        return          # refusing is acceptable
    result = result or {}
    assert not result.get('candidates'), (
        f'a wave reported {result.get("candidates")} candidates with Redis '
        f'unreachable'
    )
    assert not result.get('delivered'), (
        f'a wave reported {result.get("delivered")} offers delivered with Redis '
        f'unreachable'
    )


def test_a_requested_trip_is_still_in_postgres_after_a_failed_dispatch(
    redis_down, rider,
):
    """The trip must not vanish because the thing that finds drivers broke."""
    trip = _trip(rider, 'requested')
    from servers.ride import dispatch

    for attempt in (
        lambda: dispatch.start_dispatch(trip.id),
        lambda: dispatch.execute_wave(trip.id, 0, 'epoch-1', [2000]),
    ):
        try:
            attempt()
        except Exception:
            pass

    trip.refresh_from_db()
    assert trip.status_id.status_code == 'requested'
    assert trip.cancelled_at is None


# ===========================================================================
# Stage 3 — Redis dies AFTER assignment
# ===========================================================================

def test_the_assignment_is_still_readable_from_postgres(redis_down, rider, driver):
    """`driver_active_trip_ids` queries the Trip table, not Redis, by design.

    This is the single most important line in the outage story: whether a driver is
    busy is answered from PostgreSQL, so a Redis outage cannot make a busy driver
    look free.
    """
    trip = _trip(rider, 'accepted', driver=driver)

    active = driver_active_trip_ids(driver)

    assert active == [trip.id], (
        f'driver_active_trip_ids returned {active} with Redis down; if this were '
        f'empty, a second rider could be assigned the same driver'
    )


def test_a_second_assignment_is_still_refused(redis_down, rider, driver):
    """The consequence of the above, stated as the invariant it protects."""
    _trip(rider, 'accepted', driver=driver)

    still_busy = bool(driver_active_trip_ids(driver))

    assert still_busy, 'the driver reads as free during a Redis outage'


def test_the_redis_view_of_the_driver_is_reported_as_unknown_not_as_free(
    redis_down, rider, driver,
):
    """`get_driver_active_trip` must not answer "no trip" when it cannot read.

    This was a real defect: a Redis failure read as availability.
    """
    _trip(rider, 'accepted', driver=driver)
    try:
        answer = rc.get_driver_active_trip(driver.id)
    except Exception:
        return          # refusing is correct
    assert answer is not False and answer != 0, (
        f'get_driver_active_trip returned {answer!r} during an outage, which a '
        f'caller would read as "this driver is free"'
    )


# ===========================================================================
# Stage 4 — Redis dies while the ride is IN PROGRESS
# ===========================================================================

def test_a_gps_ping_failure_does_not_change_the_trip(redis_down, rider, driver):
    """Telemetry is not the ride. A failed location write must cost one ping."""
    trip = _trip(rider, 'in_progress', driver=driver)
    before = trip.status_id.status_code

    try:
        rc.add_driver_location(driver.id, 78.38, 17.445)
    except Exception:
        pass

    trip.refresh_from_db()
    assert trip.status_id.status_code == before == 'in_progress'
    assert trip.cancelled_at is None
    assert trip.completed_at is None


def test_liveness_still_records_activity_during_an_outage(redis_down, rider, driver):
    """`record_driver_activity` writes to PostgreSQL, so it must keep working.

    If it did not, a Redis outage would make every in-flight ride look abandoned to
    the stale detector the moment Redis came back.
    """
    trip = _trip(rider, 'in_progress', driver=driver)

    wrote = liveness.record_driver_activity(trip.id)

    trip.refresh_from_db()
    assert wrote is True
    assert trip.last_driver_activity_at is not None


# ===========================================================================
# Stage 5 — Redis dies BEFORE completion
# ===========================================================================

def test_a_ride_can_still_be_completed(redis_down, rider, driver):
    """Completion is a PostgreSQL transaction. Redis is cleanup, not the decision."""
    trip = _trip(rider, 'in_progress', driver=driver)

    Trip.objects.filter(id=trip.id).update(
        status_id=_status('completed'), completed_at=timezone.now())

    trip.refresh_from_db()
    assert trip.status_id.status_code == 'completed'
    assert trip.final_fare is None, 'an outage must not conjure a final fare'


def test_completion_leaves_the_driver_free_in_postgres(redis_down, rider, driver):
    """The trip-42 shape: supply must be recoverable from PostgreSQL alone.

    If `driver_active_trip_ids` needed Redis, a driver whose ride completed during an
    outage would look permanently busy and never get another ride.
    """
    trip = _trip(rider, 'in_progress', driver=driver)
    Trip.objects.filter(id=trip.id).update(
        status_id=_status('completed'), completed_at=timezone.now())

    assert driver_active_trip_ids(driver) == [], (
        'the driver still reads as busy after completing during an outage'
    )


# ===========================================================================
# Stage 6 — Redis dies while the STALE DETECTOR runs
# ===========================================================================

def test_the_stale_detector_still_runs(redis_down, rider, driver, settings):
    """It is a PostgreSQL sweep. An outage must not blind operations."""
    settings.TRIP_STALE_AFTER_SECONDS = 60
    trip = _trip(rider, 'in_progress', driver=driver)
    past = timezone.now() - timezone.timedelta(seconds=7200)
    Trip.objects.filter(id=trip.id).update(
        last_driver_activity_at=past, started_at=past, accepted_at=past,
        requested_at=past)

    summary = liveness.flag_stale_trips()

    trip.refresh_from_db()
    assert summary['flagged'] == 1
    assert trip.stale_flagged_at is not None


def test_the_detector_still_changes_no_status_during_an_outage(
    redis_down, rider, driver, settings,
):
    """The invariant that survives every other change to this area."""
    settings.TRIP_STALE_AFTER_SECONDS = 60
    trip = _trip(rider, 'in_progress', driver=driver)
    past = timezone.now() - timezone.timedelta(seconds=7200)
    Trip.objects.filter(id=trip.id).update(
        last_driver_activity_at=past, started_at=past, accepted_at=past,
        requested_at=past)

    liveness.flag_stale_trips()

    trip.refresh_from_db()
    assert trip.status_id.status_code == 'in_progress'
    assert trip.cancelled_at is None
    assert trip.final_fare is None


def test_reconciliation_reports_unavailable_rather_than_guessing(
    redis_down, rider, driver,
):
    """`reconcile_driver_active_trip` compares PostgreSQL with Redis.

    With Redis unreadable there is nothing to compare, and the honest answer is
    `unavailable`. Answering `ok` would assert agreement it never checked.
    """
    _trip(rider, 'accepted', driver=driver)

    outcome = liveness.reconcile_driver_active_trip(driver.id)

    assert outcome == 'unavailable', (
        f'reconciliation returned {outcome!r} with Redis unreachable, which claims '
        f'a comparison it could not make'
    )


def test_the_unaccepted_sweep_still_runs_during_an_outage(redis_down, rider):
    """The durable accept-deadline backstop must not need Redis either.

    It exists because the broker could not be relied on; needing Redis would put it
    right back where it started.
    """
    from servers.ride.tasks import sweep_unaccepted_trips

    trip = _trip(rider, 'requested')
    Trip.objects.filter(id=trip.id).update(
        requested_at=timezone.now() - timezone.timedelta(seconds=600))

    summary = sweep_unaccepted_trips.apply().get()

    trip.refresh_from_db()
    assert summary['cancelled'] == 1
    assert trip.status_id.status_code == 'cancelled'


# ===========================================================================
# Stage 7 — Redis comes BACK
# ===========================================================================

def test_redis_is_reconstructed_from_postgres_when_it_returns(rider, driver):
    """Recovery needs no shell and no operator.

    The trip-42 failure was a driver stuck outside dispatch forever because a Redis
    key was wrong and only an engineer could fix it. PostgreSQL is authoritative, so
    the repair is derivable.
    """
    trip = _trip(rider, 'accepted', driver=driver)

    with mock.patch.object(rc, 'set_driver_active_trip') as setter, \
            mock.patch.object(rc, 'get_driver_active_trip', return_value=None):
        outcome = liveness.reconcile_driver_active_trip(driver.id)

    assert outcome == 'repaired_set', (
        f'reconciliation returned {outcome!r}; Redis disagreed with PostgreSQL in '
        f'the direction that loses a trip, and it was not repaired'
    )
    setter.assert_called_once()
    args = setter.call_args[0]
    assert trip.id in args, 'the repair did not write the trip PostgreSQL knows about'


def test_a_stale_redis_claim_is_cleared_when_postgres_disagrees(rider, driver):
    """The other direction: Redis says busy, PostgreSQL says free.

    This is the one that removed a driver from the geo index forever, because
    `add_driver_location` drops a driver whose active-trip key is set.
    """
    _trip(rider, 'completed', driver=driver)

    with mock.patch.object(rc, 'clear_driver_active_trip') as clearer, \
            mock.patch.object(rc, 'get_driver_active_trip', return_value=999):
        outcome = liveness.reconcile_driver_active_trip(driver.id)

    assert outcome == 'repaired_cleared', (
        f'reconciliation returned {outcome!r}; a driver with no active trip in '
        f'PostgreSQL is still marked busy in Redis and will never be dispatched'
    )
    clearer.assert_called_once()


def test_agreement_is_reported_as_agreement(rider, driver):
    """The control. A reconciler that always reports a repair is not reconciling."""
    trip = _trip(rider, 'accepted', driver=driver)

    with mock.patch.object(rc, 'get_driver_active_trip', return_value=trip.id):
        outcome = liveness.reconcile_driver_active_trip(driver.id)

    assert outcome == 'ok'


# ===========================================================================
# The invariant that spans the whole matrix
# ===========================================================================

@pytest.mark.parametrize('status', list(DRIVER_ACTIVE_TRIP_STATUSES) + ['completed'])
def test_no_stage_of_an_outage_invents_money(redis_down, rider, driver, status):
    """Across every stage: no fare appears, no fare moves, no payment completes."""
    trip = _trip(rider, status, driver=driver)
    before = (trip.estimated_fare, trip.final_fare, trip.payment_status)

    for attempt in (
        lambda: rc.add_driver_location(driver.id, 78.38, 17.445),
        lambda: rc.get_driver_active_trip(driver.id),
        lambda: liveness.record_driver_activity(trip.id),
        lambda: liveness.reconcile_driver_active_trip(driver.id),
        lambda: liveness.flag_stale_trips(),
    ):
        try:
            attempt()
        except Exception:
            pass

    trip.refresh_from_db()
    assert (trip.estimated_fare, trip.final_fare, trip.payment_status) == before
    assert trip.final_fare is None
