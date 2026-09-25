"""§9 — the accept deadline needs a recovery path the broker cannot lose.

THE PROBLEM THIS SOLVES
-----------------------
`auto_cancel_trip` is scheduled once, with a 90-second countdown, when a trip is
created. That one deadline bounds the entire driver search and produces the frame
that tells a rider nobody accepted.

Measured in `qa/celery_worker_loss_drill.py`: a task whose worker is killed is not
lost, but it is invisible to every other worker for `visibility_timeout` plus up to
~100 s of restore-poll granularity -- about fifteen to seventeen minutes at the
configured 900 s.

Fifteen minutes is fine for a receipt. For a rider it means sitting in front of a
search that was already given up on, unable to rebook, because the trip is still
`requested`.

WHY NOT SIMPLY LOWER visibility_timeout
---------------------------------------
Its floor is set by the longest a message can legitimately sit unacked -- a 180 s
countdown plus a 360 s execution -- and going under that turns recovery into a
duplicate-execution generator for every other task. One broker setting cannot serve
a 90-second deadline and a six-minute task at once, which is exactly why §9 asks for
a per-task classification rather than a global retune.

So this deadline gets its own recovery, in the database. Recovery becomes the beat
interval (2 minutes), and it does not care whether the original message survived.

WHAT THESE TESTS ARE MOSTLY ABOUT
---------------------------------
Not that it cancels -- that it cancels ONLY what `auto_cancel_trip` already owns.
The negative controls are the point: an accepted trip, a trip in progress, a
completed trip and a trip still inside its deadline must all be untouched. A sweep
that cancelled a live ride would be far worse than the latency it fixes.
"""

from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from servers.driver.models import Driver, Vehicle, VehicleType
from servers.ride.models import Trip, TripStatus
from servers.ride.tasks import auto_cancel_trip, sweep_unaccepted_trips
from servers.rider.models import Notification, Rider

User = get_user_model()

pytestmark = pytest.mark.django_db

_n = iter(range(1, 999))


def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


@pytest.fixture
def rider():
    i = next(_n)
    u = User.objects.create_user(phone_number=f'+9195600{i:05d}',
                                 username=f'+9195600{i:05d}', role='rider')
    Rider.objects.create(user_id=u)
    return u


@pytest.fixture
def driver():
    i = next(_n)
    u = User.objects.create_user(phone_number=f'+9196600{i:05d}',
                                 username=f'+9196600{i:05d}', role='driver')
    d = Driver.objects.create(user_id=u, approved=True, status='online')
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    v = Vehicle.objects.create(driver_id=d, vehicle_type_id=vt,
                               vehicle_number=f'TS09SW{i:04d}')
    d.active_vehicle = v
    d.save(update_fields=['active_vehicle'])
    return d


def _trip(rider, status, age_seconds, driver=None):
    """A trip whose `requested_at` is genuinely in the past.

    `requested_at` is auto_now_add, so it has to be written after creation. A fixture
    that skips that step produces a trip the sweep correctly ignores, and a test that
    passes for the wrong reason.
    """
    t = Trip.objects.create(
        user_id=rider, driver_id=driver, status_id=_status(status),
        pickup_lat=Decimal('17.4450000'), pickup_long=Decimal('78.3800000'),
        destination_lat=Decimal('17.4550000'),
        destination_long=Decimal('78.3900000'),
        estimated_fare=Decimal('150.00'), payment_method='cash',
    )
    past = timezone.now() - timezone.timedelta(seconds=age_seconds)
    Trip.objects.filter(id=t.id).update(requested_at=past)
    t.refresh_from_db()
    return t


# ---------------------------------------------------------------------------
# What it must do
# ---------------------------------------------------------------------------

def test_a_trip_nobody_accepted_is_cancelled(rider):
    """The case broker redelivery would have taken fifteen minutes to reach."""
    trip = _trip(rider, 'requested', age_seconds=600)

    summary = sweep_unaccepted_trips.apply().get()

    trip.refresh_from_db()
    assert trip.status_id.status_code == 'cancelled', (
        'a trip 600s past its 90s deadline was left in requested; the rider is '
        'still watching a search nobody is answering, and cannot rebook'
    )
    assert trip.cancelled_by == 'system'
    assert trip.cancellation_reason == 'no_driver_accepted'
    assert summary['cancelled'] == 1


def test_the_rider_is_told(rider):
    """A cancellation nobody is told about is a silent disappearance."""
    _trip(rider, 'requested', age_seconds=600)

    sweep_unaccepted_trips.apply().get()

    assert Notification.objects.filter(user_id=rider).count() == 1


def test_it_is_safe_to_run_twice(rider):
    """Beat runs it every two minutes, and a redelivery can run it again."""
    trip = _trip(rider, 'requested', age_seconds=600)

    sweep_unaccepted_trips.apply().get()
    second = sweep_unaccepted_trips.apply().get()

    trip.refresh_from_db()
    assert trip.status_id.status_code == 'cancelled'
    assert second['cancelled'] == 0, 'the second run cancelled something again'
    assert Notification.objects.filter(user_id=rider).count() == 1, (
        'the rider was told twice'
    )


def test_a_late_auto_cancel_message_finds_nothing_to_do(rider):
    """The two mechanisms must not disagree.

    This is the whole scenario: the worker died, the sweep cancelled the trip, and
    the original message is finally redelivered a quarter of an hour later. It has
    to stand down.
    """
    trip = _trip(rider, 'requested', age_seconds=600)
    sweep_unaccepted_trips.apply().get()
    cancelled_at = Trip.objects.get(id=trip.id).cancelled_at

    outcome = auto_cancel_trip.apply(args=(trip.id,)).get()

    trip.refresh_from_db()
    assert 'already cancelled' in str(outcome).lower()
    assert trip.cancelled_at == cancelled_at, (
        'the late message rewrote cancelled_at, so the audit trail now says the '
        'trip was cancelled later than it was'
    )
    assert Notification.objects.filter(user_id=rider).count() == 1


# ---------------------------------------------------------------------------
# What it must NOT do. These are the important ones.
# ---------------------------------------------------------------------------

def test_a_trip_still_inside_its_deadline_is_untouched(rider):
    """The countdown path should win. This is a backstop, not a race."""
    trip = _trip(rider, 'requested', age_seconds=30)

    summary = sweep_unaccepted_trips.apply().get()

    trip.refresh_from_db()
    assert trip.status_id.status_code == 'requested'
    assert summary['examined'] == 0


def test_a_trip_inside_the_grace_period_is_untouched(rider, settings):
    """Past the 90s deadline but inside the grace, so the normal path still owns it."""
    settings.TRIP_ACCEPT_TIMEOUT_SECONDS = 90
    settings.TRIP_UNACCEPTED_SWEEP_GRACE_SECONDS = 60
    trip = _trip(rider, 'requested', age_seconds=120)

    sweep_unaccepted_trips.apply().get()

    trip.refresh_from_db()
    assert trip.status_id.status_code == 'requested'


def test_an_accepted_trip_is_never_cancelled(rider, driver):
    """The dangerous case. A driver is on the way to this rider."""
    trip = _trip(rider, 'accepted', age_seconds=3600, driver=driver)

    sweep_unaccepted_trips.apply().get()

    trip.refresh_from_db()
    assert trip.status_id.status_code == 'accepted'
    assert trip.cancelled_at is None
    assert trip.driver_id_id == driver.id, 'driver_id was cleared'


def test_an_in_progress_trip_is_never_cancelled(rider, driver):
    """A passenger is in the car.

    An in-progress ride that has gone quiet is a different question with a different
    answer: `flag_stale_active_trips` detects and escalates and changes no status.
    This sweep must not have an opinion about it at all.
    """
    trip = _trip(rider, 'in_progress', age_seconds=7200, driver=driver)

    summary = sweep_unaccepted_trips.apply().get()

    trip.refresh_from_db()
    assert trip.status_id.status_code == 'in_progress'
    assert trip.cancelled_at is None
    assert summary['examined'] == 0


def test_a_completed_trip_is_never_touched(rider, driver):
    trip = _trip(rider, 'completed', age_seconds=86400, driver=driver)

    sweep_unaccepted_trips.apply().get()

    trip.refresh_from_db()
    assert trip.status_id.status_code == 'completed'
    assert trip.cancelled_at is None


def test_a_requested_trip_that_somehow_has_a_driver_is_skipped(rider, driver):
    """Belt and braces. If status and driver_id ever disagree, do nothing."""
    trip = _trip(rider, 'requested', age_seconds=600, driver=driver)

    summary = sweep_unaccepted_trips.apply().get()

    trip.refresh_from_db()
    assert trip.status_id.status_code == 'requested'
    assert summary['examined'] == 0


def test_it_touches_no_money(rider):
    trip = _trip(rider, 'requested', age_seconds=600)
    before = (trip.estimated_fare, trip.final_fare, trip.payment_status)

    sweep_unaccepted_trips.apply().get()

    trip.refresh_from_db()
    assert (trip.estimated_fare, trip.final_fare, trip.payment_status) == before
    assert trip.final_fare is None


# ---------------------------------------------------------------------------
# Bounded work
# ---------------------------------------------------------------------------

def test_one_sweep_is_bounded(rider):
    """A backlog must not turn one beat tick into an unbounded unit of work."""
    for _ in range(7):
        _trip(rider, 'requested', age_seconds=600)

    summary = sweep_unaccepted_trips.apply(kwargs={'limit': 3}).get()

    assert summary['examined'] == 3
    assert Trip.objects.filter(status_id__status_code='requested').count() == 4


def test_one_bad_trip_does_not_stop_the_sweep(rider):
    """Otherwise a single wedged row blocks every rider queued behind it."""
    good = _trip(rider, 'requested', age_seconds=600)
    bad = _trip(rider, 'requested', age_seconds=700)

    real_run = auto_cancel_trip.run
    calls = {'n': 0}

    def flaky(trip_id):
        calls['n'] += 1
        if trip_id == bad.id:
            raise RuntimeError('row is wedged')
        return real_run(trip_id)

    with mock.patch.object(auto_cancel_trip, 'run', side_effect=flaky):
        summary = sweep_unaccepted_trips.apply().get()

    assert calls['n'] == 2, 'the sweep stopped at the first failure'
    good.refresh_from_db()
    assert good.status_id.status_code == 'cancelled'
    assert summary['examined'] == 2


def test_the_beat_schedule_actually_runs_it():
    """A recovery path nobody schedules is not a recovery path."""
    from django.conf import settings

    scheduled = {e['task'] for e in settings.CELERY_BEAT_SCHEDULE.values()}
    assert 'ride.sweep_unaccepted_trips' in scheduled, (
        'the durable backstop is not in the beat schedule, so the accept deadline '
        'is back to depending on broker redelivery'
    )
