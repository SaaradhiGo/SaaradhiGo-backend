"""One driver, at most one active trip — the non-concurrency half.

`_accept_trip` locks the Trip row and then the Driver row and re-validates
approval, blocked state, vehicle and fatigue inside that lock. What it never
did was ask whether the driver was *already on a ride*. The only thing
stopping a double assignment was Redis: `set_driver_active_trip` plus
`remove_driver` keep a busy driver out of the geo index so dispatch stops
offering to them. That is an offer-time filter, and it failed three ways:

  D1  Redis loses the active-trip key (flush, restart, eviction). The next
      location ping re-adds the driver to the geo index and they get offered
      a second ride.
  D2  `set_driver_active_trip` fails at accept time; its `False` return is
      discarded, so the key is never written at all.
  D3  No infrastructure failure whatsoever: two offers are already on the
      driver's screen from overlapping dispatch windows and they tap both.

These tests pin the PostgreSQL guard that closes all three. They run on
SQLite and deliberately prove nothing about lock contention — `SELECT FOR
UPDATE` is a no-op there. The contention proof lives in
`test_driver_active_trip_contention.py` and runs against real PostgreSQL.
"""

from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

import servers.redis_client as rc
from servers.consumers import TripStatusConsumer
from servers.driver.models import Driver, Vehicle, VehicleType
from servers.ride.models import (
    DRIVER_ACTIVE_TRIP_STATUSES, Trip, TripStatus, driver_active_trip_ids,
)

User = get_user_model()

PICKUP_LAT = Decimal('17.4450')
PICKUP_LNG = Decimal('78.3800')


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


def _make_trip(rider, vehicle_type, status='requested', driver=None):
    trip = Trip.objects.create(
        user_id=rider,
        status_id=_status(status),
        requested_vehicle_type=vehicle_type,
        pickup_lat=PICKUP_LAT,
        pickup_long=PICKUP_LNG,
        destination_lat=Decimal('17.4500'),
        destination_long=Decimal('78.4000'),
        pickup_address='Pickup',
        destination_address='Drop',
        estimated_fare=Decimal('150.00'),
    )
    if driver is not None:
        trip.driver_id = driver
        trip.accepted_at = timezone.now()
        trip.save(update_fields=['driver_id', 'accepted_at'])
    return trip


def accept(trip, driver_user):
    """Invoke the real `_accept_trip` synchronously.

    It is wrapped in `channels.db.DatabaseSyncToAsync`. Attribute access
    returns a `functools.partial` around the async wrapper, so the raw sync
    function has to come off the class `__dict__`. This therefore exercises
    production code rather than a reimplementation of it.

    Only the post-commit side effects are stubbed (Redis writes, the loser
    fanout, the rider push): they all happen *after* the decision point, and
    none of them is the invariant under test.
    """
    consumer = TripStatusConsumer()
    consumer.trip_id = trip.id
    consumer.user = driver_user

    raw = TripStatusConsumer.__dict__['_accept_trip'].func
    with mock.patch.object(rc, 'set_driver_active_trip', return_value=True), \
            mock.patch.object(rc, 'remove_driver', return_value=True), \
            mock.patch.object(rc, 'cache_trip', return_value=True), \
            mock.patch('servers.ride.dispatch.dismiss_outstanding_offers', return_value=[]), \
            mock.patch('servers.auth_user.services.send_push_notification', return_value=True):
        return raw(consumer)


@pytest.fixture(autouse=True)
def _isolate_redis(db):
    """Redis is not rolled back with the test transaction and SQLite restarts
    primary keys, so driver/trip id 1 recurs and keys leak between tests."""
    def purge():
        if rc.redis_client is None:
            return
        for pattern in ('trip:offered:*', 'drivers:geo:*', 'driver:heartbeat:*',
                        'driver:active_trip:*'):
            try:
                for k in rc.redis_client.scan_iter(match=pattern, count=500):
                    rc.redis_client.delete(k)
            except Exception:  # noqa: BLE001
                pass
    purge()
    yield
    purge()


@pytest.fixture
def rider(db):
    return User.objects.create_user(phone_number='+919300000001', role='rider')


@pytest.fixture
def vehicle_type(db):
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    return vt


@pytest.fixture
def driver(db, vehicle_type):
    user = User.objects.create_user(phone_number='+919400000001', role='driver')
    d = Driver.objects.create(user_id=user, approved=True, status='online')
    v = Vehicle.objects.create(
        driver_id=d, vehicle_type_id=vehicle_type, vehicle_number='TS09AB4321',
    )
    d.active_vehicle = v
    d.save(update_fields=['active_vehicle'])
    return d


# ---------------------------------------------------------------------------
# The canonical status set
# ---------------------------------------------------------------------------

def test_driver_active_statuses_exclude_requested():
    """`requested` belongs to the broader rider-facing active set, not this one.

    A requested trip has no driver, so including it here would make every
    unassigned trip look like it occupied somebody.
    """
    assert DRIVER_ACTIVE_TRIP_STATUSES == ('accepted', 'reached', 'in_progress')
    assert 'requested' not in DRIVER_ACTIVE_TRIP_STATUSES
    assert 'completed' not in DRIVER_ACTIVE_TRIP_STATUSES
    assert 'cancelled' not in DRIVER_ACTIVE_TRIP_STATUSES


def test_active_statuses_are_all_real_trip_statuses(db):
    declared = {c for c, _ in TripStatus._meta.get_field('status_code').choices}
    assert set(DRIVER_ACTIVE_TRIP_STATUSES) <= declared


@pytest.mark.django_db
def test_helper_ignores_the_trip_being_accepted(rider, vehicle_type, driver):
    trip = _make_trip(rider, vehicle_type, status='accepted', driver=driver)
    assert driver_active_trip_ids(driver) == [trip.id]
    assert driver_active_trip_ids(driver, exclude_trip_id=trip.id) == []


# ---------------------------------------------------------------------------
# Existing active trip blocks a second acceptance
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@pytest.mark.parametrize('busy_status', ['accepted', 'reached', 'in_progress'])
def test_driver_on_an_active_trip_cannot_accept_another(
    rider, vehicle_type, driver, busy_status,
):
    busy = _make_trip(rider, vehicle_type, status=busy_status, driver=driver)
    second = _make_trip(rider, vehicle_type, status='requested')

    result = accept(second, driver.user_id)

    assert result['success'] is False
    assert result['reason'] == 'driver_already_on_trip'

    second.refresh_from_db()
    assert second.driver_id_id is None, 'losing trip must keep no driver'
    assert second.status_id.status_code == 'requested'
    assert second.otp is None, 'no OTP may be generated for a refused accept'
    assert second.accepted_at is None

    # The trip the driver really is on is untouched.
    busy.refresh_from_db()
    assert busy.driver_id_id == driver.id
    assert busy.status_id.status_code == busy_status


@pytest.mark.django_db
def test_rejection_does_not_leak_the_conflicting_trip(rider, vehicle_type, driver):
    """The refusal goes to the acting driver's own socket, but it still must
    not name another trip in the payload."""
    _make_trip(rider, vehicle_type, status='accepted', driver=driver)
    second = _make_trip(rider, vehicle_type, status='requested')

    result = accept(second, driver.user_id)

    assert 'trip_id' not in result
    assert str(second.id) not in result['error']
    assert 'conflicting' not in result


@pytest.mark.django_db
def test_rejection_writes_no_redis_state(rider, vehicle_type, driver):
    """D1/D2/D3 all end here: a refused accept must not mark the driver busy
    for the trip it refused."""
    _make_trip(rider, vehicle_type, status='in_progress', driver=driver)
    second = _make_trip(rider, vehicle_type, status='requested')

    consumer = TripStatusConsumer()
    consumer.trip_id = second.id
    consumer.user = driver.user_id
    raw = TripStatusConsumer.__dict__['_accept_trip'].func

    with mock.patch.object(rc, 'set_driver_active_trip') as set_active, \
            mock.patch.object(rc, 'remove_driver') as remove, \
            mock.patch.object(rc, 'cache_trip') as cache, \
            mock.patch('servers.ride.dispatch.dismiss_outstanding_offers') as dismiss:
        result = raw(consumer)

    assert result['reason'] == 'driver_already_on_trip'
    set_active.assert_not_called()
    remove.assert_not_called()
    cache.assert_not_called()
    dismiss.assert_not_called()


# ---------------------------------------------------------------------------
# History must not block future work
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@pytest.mark.parametrize('terminal_status', ['completed', 'cancelled'])
def test_historical_trips_do_not_block_a_new_acceptance(
    rider, vehicle_type, driver, terminal_status,
):
    """`Trip.driver_id` is never nulled, so completed and cancelled trips keep
    their driver for history and settlement. That retention must not make the
    driver permanently ineligible — which is exactly why the guard filters on
    status rather than on `driver IS NOT NULL`.
    """
    past = _make_trip(rider, vehicle_type, status=terminal_status, driver=driver)
    assert past.driver_id_id == driver.id

    fresh = _make_trip(rider, vehicle_type, status='requested')
    result = accept(fresh, driver.user_id)

    assert result['success'] is True, result
    fresh.refresh_from_db()
    assert fresh.driver_id_id == driver.id
    assert fresh.status_id.status_code == 'accepted'
    assert fresh.otp is not None


@pytest.mark.django_db
def test_many_historical_trips_still_allow_acceptance(rider, vehicle_type, driver):
    for _ in range(5):
        _make_trip(rider, vehicle_type, status='completed', driver=driver)
    for _ in range(3):
        _make_trip(rider, vehicle_type, status='cancelled', driver=driver)

    fresh = _make_trip(rider, vehicle_type, status='requested')
    assert accept(fresh, driver.user_id)['success'] is True


@pytest.mark.django_db
def test_free_driver_accepts_normally(rider, vehicle_type, driver):
    """The guard must not regress the happy path."""
    trip = _make_trip(rider, vehicle_type, status='requested')
    result = accept(trip, driver.user_id)

    assert result['success'] is True
    assert result['driver_id'] == driver.id
    trip.refresh_from_db()
    assert trip.driver_id_id == driver.id
    assert trip.status_id.status_code == 'accepted'


@pytest.mark.django_db
def test_driver_freed_by_completion_can_accept_again(rider, vehicle_type, driver):
    first = _make_trip(rider, vehicle_type, status='requested')
    assert accept(first, driver.user_id)['success'] is True

    second = _make_trip(rider, vehicle_type, status='requested')
    assert accept(second, driver.user_id)['reason'] == 'driver_already_on_trip'

    # Finish the first trip, then the driver is free again.
    first.refresh_from_db()
    first.status_id = _status('completed')
    first.save(update_fields=['status_id'])

    assert accept(second, driver.user_id)['success'] is True


# ---------------------------------------------------------------------------
# D1 / D2 — PostgreSQL rejects even with Redis absent
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_postgres_rejects_when_redis_active_trip_key_is_missing(
    rider, vehicle_type, driver,
):
    """D1 and D2 together.

    The driver genuinely holds an active trip in PostgreSQL, but the Redis
    active-trip key does not exist — flushed, evicted, or never written
    because `set_driver_active_trip` failed and its return value was
    discarded. Before this guard, the next location ping would put the driver
    back in the geo index and nothing would refuse the second acceptance.
    """
    busy = _make_trip(rider, vehicle_type, status='accepted', driver=driver)

    if rc.redis_client is not None:
        rc.clear_driver_active_trip(driver.id)
        assert rc.get_driver_active_trip(driver.id) is None

    second = _make_trip(rider, vehicle_type, status='requested')
    result = accept(second, driver.user_id)

    assert result['reason'] == 'driver_already_on_trip'
    second.refresh_from_db()
    assert second.driver_id_id is None
    assert driver_active_trip_ids(driver) == [busy.id]


@pytest.mark.django_db
def test_redis_entirely_unavailable_still_rejects(rider, vehicle_type, driver):
    """Harder version of D1: the Redis client itself is gone."""
    _make_trip(rider, vehicle_type, status='reached', driver=driver)
    second = _make_trip(rider, vehicle_type, status='requested')

    with mock.patch.object(rc, 'redis_client', None):
        result = accept(second, driver.user_id)

    assert result['reason'] == 'driver_already_on_trip'
    second.refresh_from_db()
    assert second.driver_id_id is None


# ---------------------------------------------------------------------------
# D3 — two stale offer cards, no infrastructure failure
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_two_outstanding_offers_only_one_can_be_accepted(rider, vehicle_type, driver):
    """D3, the case that needs nothing to be broken.

    Two dispatch generations overlapped, so the driver holds two valid offer
    cards. Tapping both used to assign both.
    """
    trip_a = _make_trip(rider, vehicle_type, status='requested')
    trip_b = _make_trip(rider, vehicle_type, status='requested')

    first = accept(trip_a, driver.user_id)
    second = accept(trip_b, driver.user_id)

    assert first['success'] is True
    assert second['success'] is False
    assert second['reason'] == 'driver_already_on_trip'

    trip_a.refresh_from_db()
    trip_b.refresh_from_db()
    assert trip_a.driver_id_id == driver.id
    assert trip_b.driver_id_id is None
    assert trip_b.status_id.status_code == 'requested'
    assert trip_b.otp is None
    assert driver_active_trip_ids(driver) == [trip_a.id]


@pytest.mark.django_db
def test_re_accepting_the_same_trip_is_not_a_conflict(rider, vehicle_type, driver):
    """A duplicate tap on the trip the driver already won must not be reported
    as a conflict with itself — that is what `exclude_trip_id` is for. It is
    still refused, by the pre-existing `driver_id is not None` check.
    """
    trip = _make_trip(rider, vehicle_type, status='requested')
    assert accept(trip, driver.user_id)['success'] is True

    again = accept(trip, driver.user_id)
    assert again['success'] is False
    assert again.get('taken') is True
    assert again.get('reason') != 'driver_already_on_trip'


# ---------------------------------------------------------------------------
# Django admin defence-in-depth
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_admin_form_blocks_manual_double_assignment(rider, vehicle_type, driver):
    """`Trip` was registered bare, giving superusers an editable driver field
    with no validation at all — a second assignment path."""
    from servers.ride.admin import TripAdminForm

    _make_trip(rider, vehicle_type, status='accepted', driver=driver)
    target = _make_trip(rider, vehicle_type, status='requested')

    form = TripAdminForm(
        instance=target,
        data={
            'user_id': rider.pk,
            'driver_id': driver.pk,
            'status_id': _status('accepted').pk,
            'pickup_lat': str(PICKUP_LAT), 'pickup_long': str(PICKUP_LNG),
            'destination_lat': '17.4500', 'destination_long': '78.4000',
            'surge_multiplier': '1.00', 'cancellation_fee': '0.00',
            'cancelled_by': '', 'cancellation_reason': '',
        },
    )
    assert form.is_valid() is False
    assert 'driver_id' in form.errors


@pytest.mark.django_db
def test_admin_form_allows_assignment_to_a_free_driver(rider, vehicle_type, driver):
    from servers.ride.admin import TripAdminForm

    _make_trip(rider, vehicle_type, status='completed', driver=driver)
    target = _make_trip(rider, vehicle_type, status='requested')

    form = TripAdminForm(
        instance=target,
        data={
            'user_id': rider.pk,
            'driver_id': driver.pk,
            'status_id': _status('accepted').pk,
            'pickup_lat': str(PICKUP_LAT), 'pickup_long': str(PICKUP_LNG),
            'destination_lat': '17.4500', 'destination_long': '78.4000',
            'surge_multiplier': '1.00', 'cancellation_fee': '0.00',
            'cancelled_by': '', 'cancellation_reason': '',
        },
    )
    assert form.errors.get('driver_id') is None


@pytest.mark.django_db
def test_admin_hides_the_otp_field_from_editing():
    from django.contrib import admin as dj_admin

    from servers.ride.models import Trip as TripModel

    registered = dj_admin.site._registry[TripModel]
    assert 'otp' in registered.readonly_fields
