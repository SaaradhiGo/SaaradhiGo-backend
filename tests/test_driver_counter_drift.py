"""The driver's completed-trip counter must survive going offline.

THE DEFECT, FOUND BY RECONCILING DRIVER #8 AFTER TRIP #90
The driver profile reported 3 completed trips. The authoritative trip
history contained 4, and the earnings endpoint -- which counts trips
itself -- also said 4. Money reconciled on 4; only the stored counter was
behind, by exactly one.

ROOT CAUSE: A STALE MODEL WRITE, NOT A LOST INCREMENT
`DriverLocationConsumer.connect()` loads `self.driver` once, and holds that
instance for the whole socket. Going online or offline then did:

    self.driver.status = "online"
    self.driver.save()            # <- every column, from a stale snapshot

A trip completing elsewhere increments `total_trips` in the database with
`F('total_trips') + 1`. The consumer's instance never learns about it, so
the next online/offline toggle writes the whole row back from the snapshot
taken at connect time and the increment is gone.

Sequence that loses one, which is exactly what happened on QA:
  1. driver goes online          -> instance cached with total_trips = 3
  2. driver completes a trip     -> database row becomes 4
  3. driver goes offline         -> full save writes 3 back

The increment itself is correct: it is a database-side F() expression
inside the transaction already holding the trip row, and the transition
guard means a replayed completion never reaches it. Nothing was wrong with
the counting. The row was overwritten afterwards.

`total_trips` is not the only casualty. A full save from a connect-time
snapshot reverts ANY column another path wrote during the session --
`ratings`, `approved`, `qa_simulated_eligibility`, `active_vehicle`. An
operator approving a driver's KYC while that driver was online would have
had the approval silently undone the moment they toggled offline.
"""

import itertools
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from servers.driver.models import Driver, Vehicle, VehicleType
from servers.ride.models import Trip, TripStatus

User = get_user_model()
pytestmark = pytest.mark.django_db


def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


def _driver(suffix=1):
    user = User.objects.create_user(
        phone_number=f'+91777100{suffix:04d}', role='driver',
    )
    driver = Driver.objects.create(user_id=user, approved=True, status='off')
    vtype, _ = VehicleType.objects.get_or_create(type='sedan')
    vehicle = Vehicle.objects.create(
        driver_id=driver, vehicle_type_id=vtype,
        vehicle_number=f'AP16QA{suffix:04d}',
    )
    driver.active_vehicle = vehicle
    driver.save(update_fields=['active_vehicle'])
    return driver


_rider_seq = itertools.count(1)


def _complete_a_trip_for(driver):
    """What the completion branch does to the counter: a database-side F()."""
    from django.db.models import F

    rider = User.objects.create_user(
        phone_number=f'+9177720{next(_rider_seq):05d}', role='rider',
    )
    Trip.objects.create(
        user_id=rider, driver_id=driver, status_id=_status('completed'),
        pickup_lat=Decimal('16.5062'), pickup_long=Decimal('80.6480'),
        destination_lat=Decimal('16.5190'), destination_long=Decimal('80.6305'),
        estimated_fare=Decimal('135.68'), currency='INR', payment_method='cash',
    )
    Driver.objects.filter(id=driver.id).update(total_trips=F('total_trips') + 1)


def _toggle(stale_driver, online):
    """Run the real consumer method against a connect-time instance."""
    from servers.consumers import DriverLocationConsumer

    consumer = DriverLocationConsumer.__new__(DriverLocationConsumer)
    consumer.driver = stale_driver
    method = (
        DriverLocationConsumer._active_the_driver
        if online
        else DriverLocationConsumer._deactive_the_driver
    )
    return method.__wrapped__(consumer)


def test_going_offline_does_not_undo_a_completed_trip():
    """The reproduction. This failed before the save was scoped."""
    driver = _driver(1)

    # 1. The driver goes online; the consumer caches this instance.
    stale = Driver.objects.get(id=driver.id)
    _toggle(stale, online=True)
    assert Driver.objects.get(id=driver.id).total_trips == 0

    # 2. A trip completes. The database row moves; `stale` does not know.
    _complete_a_trip_for(driver)
    assert Driver.objects.get(id=driver.id).total_trips == 1

    # 3. The driver goes offline, through the same cached instance.
    _toggle(stale, online=False)

    assert Driver.objects.get(id=driver.id).total_trips == 1, (
        'going offline reverted the completed-trip count to its value from '
        'when the socket opened'
    )


def test_several_trips_in_one_session_all_survive():
    driver = _driver(2)
    stale = Driver.objects.get(id=driver.id)
    _toggle(stale, online=True)

    for _ in range(3):
        _complete_a_trip_for(driver)

    _toggle(stale, online=False)
    assert Driver.objects.get(id=driver.id).total_trips == 3


def test_the_toggle_still_sets_the_status_it_is_for():
    """Scoping the save must not stop it doing its job."""
    driver = _driver(3)
    stale = Driver.objects.get(id=driver.id)

    _toggle(stale, online=True)
    assert Driver.objects.get(id=driver.id).status == 'online'

    _toggle(stale, online=False)
    assert Driver.objects.get(id=driver.id).status == 'off'


def test_an_approval_granted_mid_session_is_not_reverted():
    """The same stale write could undo a human's KYC decision.

    An operator approving a driver who happened to be online would have had
    the approval wiped by that driver's next offline toggle.
    """
    driver = _driver(4)
    Driver.objects.filter(id=driver.id).update(approved=False)
    stale = Driver.objects.get(id=driver.id)
    _toggle(stale, online=True)

    # An operator approves them in the console, mid-shift.
    Driver.objects.filter(id=driver.id).update(approved=True)

    _toggle(stale, online=False)
    assert Driver.objects.get(id=driver.id).approved is True, (
        'a driver going offline reverted a KYC approval made during their shift'
    )


def test_a_rating_written_mid_session_is_not_reverted():
    driver = _driver(5)
    stale = Driver.objects.get(id=driver.id)
    _toggle(stale, online=True)

    Driver.objects.filter(id=driver.id).update(ratings=Decimal('4.75'))

    _toggle(stale, online=False)
    assert Driver.objects.get(id=driver.id).ratings == Decimal('4.75')


def test_the_stored_counter_agrees_with_the_trip_table():
    """What reconciliation asks: does the counter match the record?"""
    driver = _driver(6)
    stale = Driver.objects.get(id=driver.id)
    _toggle(stale, online=True)
    for _ in range(4):
        _complete_a_trip_for(driver)
    _toggle(stale, online=False)

    stored = Driver.objects.get(id=driver.id).total_trips
    actual = Trip.objects.filter(
        driver_id=driver, status_id__status_code='completed',
    ).count()
    assert stored == actual == 4
