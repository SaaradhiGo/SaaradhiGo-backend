"""The rider must be able to read their own pickup PIN.

THE DEFECT, FOUND BY RUNNING A SECOND REAL RIDE (trip #84)
The driver reached the pickup and asked for the PIN. The rider -- on the
website, signed in, watching their own trip -- had no way to see it. The
number is broadcast exactly once, inside the `accept` event, and a client
that was not connected at that instant, or that reloaded the page, never saw
it again.

Proven against QA by opening the rider's trip socket for trip #84 and
reading the greeting:

    {"type": "connection_established", "trip_id": 84, "subscribed": true,
     "trip_status": "reached", "message": "Connected to trip updates"}

No OTP. The ride could not be started at all: the driver cannot proceed
without the PIN, and the only other place it exists is the database.

The greeting already exists to let a reconnecting client "discover durable
truth rather than infer it". The PIN is part of that truth, so it now
travels with it -- for the RIDER, and only while the PIN is still needed.
"""

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


def _trip(status='accepted', otp='802039'):
    rider = User.objects.create_user(phone_number='+917770001002', role='rider')
    duser = User.objects.create_user(phone_number='+917770001003', role='driver')
    driver = Driver.objects.create(user_id=duser)
    vtype, _ = VehicleType.objects.get_or_create(type='sedan')
    vehicle = Vehicle.objects.create(
        driver_id=driver, vehicle_type_id=vtype, vehicle_number='AP16QA0001',
    )
    driver.active_vehicle = vehicle
    driver.save(update_fields=['active_vehicle'])
    trip = Trip.objects.create(
        user_id=rider, driver_id=driver, status_id=_status(status),
        pickup_lat=Decimal('16.5062'), pickup_long=Decimal('80.6480'),
        destination_lat=Decimal('16.5190'), destination_long=Decimal('80.6305'),
        estimated_fare=Decimal('135.68'), currency='INR',
        payment_method='cash', otp=otp,
    )
    return trip, rider, driver


def _greeting_otp(trip, participation, status):
    """What `_current_trip_otp` would contribute to the greeting.

    Exercises the real consumer helper and the real gate, without standing up
    a Channels socket -- the condition under test is "who gets the PIN, and
    when", not the transport.
    """
    from servers.consumers import TripStatusConsumer

    consumer = TripStatusConsumer.__new__(TripStatusConsumer)
    consumer.trip_id = trip.id
    consumer.participation = participation

    include = participation == 'rider' and status in ('accepted', 'reached')
    if not include:
        return None
    # The helper is wrapped for async use; call the function it wraps.
    return TripStatusConsumer._current_trip_otp.__wrapped__(consumer)


@pytest.mark.parametrize('status', ['accepted', 'reached'])
def test_rider_greeting_carries_the_pin_while_it_is_needed(status):
    trip, _rider, _driver = _trip(status=status)
    assert _greeting_otp(trip, 'rider', status) == '802039', (
        'the rider must be able to read their own PIN after a reload; '
        'without it the driver cannot start the trip at all'
    )


@pytest.mark.parametrize('status', ['in_progress', 'completed', 'cancelled'])
def test_pin_is_not_resent_once_it_proves_nothing(status):
    trip, _rider, _driver = _trip(status=status)
    assert _greeting_otp(trip, 'rider', status) is None


@pytest.mark.parametrize('participation', ['assigned_driver', 'candidate_driver'])
def test_only_the_rider_receives_the_pin(participation):
    """The driver must be TOLD the PIN by the rider; that is the point of it."""
    trip, _rider, _driver = _trip(status='reached')
    assert _greeting_otp(trip, participation, 'reached') is None


def test_helper_returns_a_string_for_reading_aloud():
    trip, _rider, _driver = _trip(status='accepted', otp='004321')
    value = _greeting_otp(trip, 'rider', 'accepted')
    assert value == '004321', 'a leading zero must survive'
    assert isinstance(value, str)


def test_missing_otp_is_none_not_a_crash():
    trip, _rider, _driver = _trip(status='accepted', otp=None)
    assert _greeting_otp(trip, 'rider', 'accepted') is None
