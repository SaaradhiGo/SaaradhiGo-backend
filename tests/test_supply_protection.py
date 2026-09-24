"""One broken ride must not poison dispatch for everyone else.

This is the blast-radius question behind QA trip 42. That trip stranded its
driver, and the thing worth knowing is not just "can we recover the driver" but
"how much of the fleet did one abandoned ride take with it".

Ten drivers. One is stranded mid-trip. The claims:

  1. The other nine remain dispatchable. The damage is one driver, not the pool.
  2. The stranded one is NOT dispatchable, which is correct -- they are notionally
     on a trip -- and is operationally visible rather than silently missing.
  3. After the trip is legitimately resolved, that driver returns to supply
     without an engineer.

Claim 3 is the one that did not hold before. `driver:active_trip:<id>` has no TTL,
`add_driver_location` removes a driver from the geo index while it is set, and
nothing cleared it -- so a driver whose trip ended could ping forever and never
be offered work again.

Dispatch eligibility is computed from PostgreSQL here (`driver_active_trip_ids`),
which is the authoritative guard. The Redis geo index is the fast path over the
top of it, and `test_driver_exclusivity_redis_down.py` already proves the
database alone is sufficient.
"""

from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from servers.driver.models import Driver, Vehicle, VehicleType
from servers.ride import liveness
from servers.ride.models import (
    Trip, TripStatus, driver_active_trip_ids,
)
from servers.rider.models import Rider

User = get_user_model()

pytestmark = pytest.mark.django_db

FLEET = 10


def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


@pytest.fixture
def fleet():
    """Ten approved, online drivers and one rider."""
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    drivers = []
    for i in range(FLEET):
        u = User.objects.create_user(
            phone_number=f'+9195330000{i:02d}', role='driver')
        d = Driver.objects.create(user_id=u, approved=True, status='online')
        v = Vehicle.objects.create(
            driver_id=d, vehicle_type_id=vt, vehicle_number=f'TS09FF{i:04d}')
        d.active_vehicle = v
        d.save(update_fields=['active_vehicle'])
        drivers.append(d)

    rider_u = User.objects.create_user(phone_number='+919533009999', role='rider')
    Rider.objects.create(user_id=rider_u)
    return {'drivers': drivers, 'vehicle_type': vt, 'rider': rider_u}


def _start_trip(rider, driver, vt, silent_seconds=0):
    """Put one driver on an in-progress trip, optionally already quiet."""
    when = timezone.now() - timezone.timedelta(seconds=silent_seconds)
    trip = Trip.objects.create(
        user_id=rider, driver_id=driver, status_id=_status('in_progress'),
        requested_vehicle_type=vt,
        pickup_lat=Decimal('17.4450000'), pickup_long=Decimal('78.3800000'),
        destination_lat=Decimal('17.4550000'), destination_long=Decimal('78.3900000'),
        estimated_fare=Decimal('150.00'), payment_method='cash',
    )
    Trip.objects.filter(id=trip.id).update(
        requested_at=when, accepted_at=when, started_at=when,
        last_driver_activity_at=when,
    )
    trip.refresh_from_db()
    return trip


def _dispatchable(drivers):
    """Drivers with no active trip according to PostgreSQL -- the real guard."""
    return [d for d in drivers if not driver_active_trip_ids(d)]


# ---------------------------------------------------------------------------
# Blast radius
# ---------------------------------------------------------------------------

def test_nine_of_ten_drivers_remain_dispatchable(fleet):
    """One stranded ride costs one driver, not the fleet."""
    drivers, vt, rider = fleet['drivers'], fleet['vehicle_type'], fleet['rider']
    stranded = drivers[3]
    _start_trip(rider, stranded, vt, silent_seconds=3600)

    available = _dispatchable(drivers)

    assert len(available) == FLEET - 1, (
        f'{len(available)} of {FLEET} drivers are dispatchable; one stranded ride '
        'has taken more than one driver out of supply'
    )
    assert stranded not in available


def test_the_stranded_driver_is_operationally_visible(fleet, settings):
    """Not dispatchable is correct. Invisible is not.

    Before the detector, a stranded driver was simply absent from supply with
    nothing anywhere to say why.
    """
    settings.TRIP_STALE_AFTER_SECONDS = 600
    drivers, vt, rider = fleet['drivers'], fleet['vehicle_type'], fleet['rider']
    trip = _start_trip(rider, drivers[3], vt, silent_seconds=3600)

    summary = liveness.flag_stale_trips()

    trip.refresh_from_db()
    assert summary['flagged'] == 1
    assert trip.stale_flagged_at is not None
    assert trip in liveness.stale_candidates()


def test_a_healthy_long_ride_does_not_appear_in_the_queue(fleet, settings):
    """The control. A two-hour ride whose driver is pinging is not a problem.

    Without this, "detect stale rides" quietly becomes "flag every long ride",
    and the queue stops meaning anything.
    """
    settings.TRIP_STALE_AFTER_SECONDS = 600
    drivers, vt, rider = fleet['drivers'], fleet['vehicle_type'], fleet['rider']
    trip = _start_trip(rider, drivers[5], vt, silent_seconds=7200)
    # The driver is still there, just on a long journey.
    liveness.record_driver_activity(trip.id)

    summary = liveness.flag_stale_trips()

    trip.refresh_from_db()
    assert summary['flagged'] == 0
    assert trip.stale_flagged_at is None
    assert liveness.classify(trip)[0] == liveness.HEALTHY


def test_several_stranded_rides_are_each_flagged_once(fleet, settings):
    settings.TRIP_STALE_AFTER_SECONDS = 600
    drivers, vt, rider = fleet['drivers'], fleet['vehicle_type'], fleet['rider']
    trips = [_start_trip(rider, drivers[i], vt, silent_seconds=3600)
             for i in (1, 4, 7)]

    first = liveness.flag_stale_trips()
    second = liveness.flag_stale_trips()

    assert first['flagged'] == 3
    assert second['flagged'] == 0
    assert len(_dispatchable(drivers)) == FLEET - 3


# ---------------------------------------------------------------------------
# Return to supply, without an engineer
# ---------------------------------------------------------------------------

class _FakeRedisState:
    def __init__(self, value=None):
        self.value = value

    def get(self, key):
        return None if self.value is None else str(self.value).encode()

    def set(self, key, value):
        self.value = value
        return True

    def delete(self, *keys):
        self.value = None
        return 1


def test_resolving_the_trip_returns_the_driver_to_supply(fleet):
    """The property that did not hold before.

    Completing the trip must make the driver dispatchable again in PostgreSQL AND
    clear the ephemeral key that was keeping them out of the geo index.
    """
    drivers, vt, rider = fleet['drivers'], fleet['vehicle_type'], fleet['rider']
    stranded = drivers[3]
    trip = _start_trip(rider, stranded, vt, silent_seconds=3600)
    assert stranded not in _dispatchable(drivers)

    # A legitimate terminal transition, through the database.
    Trip.objects.filter(id=trip.id).update(
        status_id=_status('completed'), completed_at=timezone.now())

    # PostgreSQL now says the driver is free...
    assert stranded in _dispatchable(drivers)

    # ...and reconciliation clears the ephemeral key that outlived the trip.
    fake = _FakeRedisState(value=trip.id)
    with mock.patch('servers.redis_client.redis_client', fake):
        outcome = liveness.reconcile_driver_active_trip(stranded.id)

    assert outcome == 'repaired_cleared'
    assert fake.value is None, (
        'the stale active-trip key survived, so add_driver_location would keep '
        'removing this driver from the geo index and they would never be offered '
        'work again -- exactly the trip-42 failure'
    )


def test_the_recovered_driver_does_not_disturb_the_others(fleet):
    drivers, vt, rider = fleet['drivers'], fleet['vehicle_type'], fleet['rider']
    trip = _start_trip(rider, drivers[3], vt, silent_seconds=3600)
    Trip.objects.filter(id=trip.id).update(
        status_id=_status('completed'), completed_at=timezone.now())

    fake = _FakeRedisState(value=trip.id)
    with mock.patch('servers.redis_client.redis_client', fake):
        liveness.reconcile_driver_active_trip(drivers[3].id)

    assert len(_dispatchable(drivers)) == FLEET, (
        'the full fleet should be dispatchable again once the one bad ride is '
        'resolved'
    )


def test_reconciliation_of_one_driver_leaves_the_others_untouched(fleet):
    """A repair must be surgical.

    Nine other drivers are mid-shift; a reconciliation pass on one of them must
    not disturb their trips or their availability.
    """
    drivers, vt, rider = fleet['drivers'], fleet['vehicle_type'], fleet['rider']
    busy = [_start_trip(rider, drivers[i], vt) for i in (0, 1, 2)]
    trip = _start_trip(rider, drivers[9], vt, silent_seconds=3600)
    Trip.objects.filter(id=trip.id).update(status_id=_status('completed'))

    fake = _FakeRedisState(value=trip.id)
    with mock.patch('servers.redis_client.redis_client', fake):
        liveness.reconcile_driver_active_trip(drivers[9].id)

    for t in busy:
        t.refresh_from_db()
        assert t.status_id.status_code == 'in_progress'
        assert t.driver_id_id is not None
    assert len(_dispatchable(drivers)) == FLEET - 3


def test_the_stranded_driver_cannot_be_given_a_second_trip(fleet):
    """While the trip is unresolved the driver stays out of supply.

    Restoring a stranded driver must be the consequence of resolving their trip,
    never a workaround that hands them a second one.
    """
    drivers, vt, rider = fleet['drivers'], fleet['vehicle_type'], fleet['rider']
    stranded = drivers[3]
    _start_trip(rider, stranded, vt, silent_seconds=7200)

    # Even after the detector has flagged it.
    liveness.flag_stale_trips()

    assert driver_active_trip_ids(stranded), (
        'a flagged stale trip stopped counting as an active trip; the driver could '
        'be dispatched a second ride while still notionally on the first'
    )
    assert stranded not in _dispatchable(drivers)
