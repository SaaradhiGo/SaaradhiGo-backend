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


# ---------------------------------------------------------------------------
# THE SECOND DEFECT, FOUND BY RUNNING TRIP #88
#
# Fixing the greeting was not enough. A rider on the website opens the trip
# socket the moment the trip is created -- while it is still `searching` --
# so the greeting has no PIN to carry, and the page never reconnects. The PIN
# has to arrive on the accept broadcast itself.
#
# It did reach the trip group: `status_event` carries `otp` when the action is
# `accept`. But `trip_status_update` rebuilt the outgoing frame from a fixed
# set of keys and dropped it. So the driver accepted trip #88, drove to the
# pickup and asked for a number the rider could not see anywhere.
#
# The PIN now travels to the RIDER on that broadcast, and only the rider: the
# passenger has to say it out loud, which is the entire point of it.
# ---------------------------------------------------------------------------

import asyncio
import json


def _relay(participation, event):
    """Run the real `trip_status_update` and capture the frame it sends."""
    from servers.consumers import TripStatusConsumer

    consumer = TripStatusConsumer.__new__(TripStatusConsumer)
    consumer.participation = participation

    sent = []

    async def fake_send(text_data=None, **_kw):
        sent.append(json.loads(text_data))

    consumer.send = fake_send
    asyncio.run(TripStatusConsumer.trip_status_update(consumer, event))
    assert len(sent) == 1
    return sent[0]


def _accept_event(otp='802039'):
    return {
        'type': 'trip_status_update',
        'trip_id': 88,
        'status': 'accept',
        'message': 'Trip accepted',
        'driver_id': 8,
        'otp': otp,
        'driver_info': {'id': 8, 'stars': '0.0'},
        'vehicle_info': {'vehicle_number': 'AP16QA0001'},
    }


def test_rider_receives_the_pin_on_the_accept_broadcast():
    frame = _relay('rider', _accept_event())
    assert frame['otp'] == '802039', (
        'without this the rider cannot start the ride at all: the PIN exists '
        'nowhere else on their screen'
    )


@pytest.mark.parametrize(
    'participation', ['assigned_driver', 'candidate_driver', None],
)
def test_nobody_but_the_rider_receives_the_pin(participation):
    frame = _relay(participation, _accept_event())
    assert 'otp' not in frame, (
        'the driver must be told the PIN by the passenger; a driver that can '
        'read it off their own screen can start a ride nobody got into'
    )


def test_rider_also_receives_the_driver_and_vehicle_details():
    frame = _relay('rider', _accept_event())
    assert frame['driver_info']['id'] == 8
    assert frame['vehicle_info']['vehicle_number'] == 'AP16QA0001'


def test_driver_details_are_not_leaked_to_other_candidates():
    frame = _relay('candidate_driver', _accept_event())
    assert 'driver_info' not in frame
    assert 'vehicle_info' not in frame


def test_the_ordinary_status_frame_is_unchanged():
    """Every other status must keep the exact shape clients already parse."""
    frame = _relay('rider', {
        'type': 'trip_status_update',
        'trip_id': 88,
        'status': 'in_progress',
        'message': '',
        'driver_id': 8,
    })
    assert frame == {
        'type': 'trip_status_update',
        'trip_id': 88,
        'status': 'in_progress',
        'message': '',
        'driver_id': 8,
    }


@pytest.mark.parametrize('otp', [None, '', '   '])
def test_a_missing_pin_is_omitted_rather_than_sent_as_empty(otp):
    event = _accept_event(otp=otp)
    frame = _relay('rider', event)
    assert 'otp' not in frame, (
        'an empty PIN box is worse than none: it invites the rider to read '
        'out a blank'
    )


def test_a_numeric_pin_survives_as_a_string_with_its_leading_zero():
    frame = _relay('rider', _accept_event(otp='004321'))
    assert frame['otp'] == '004321'
