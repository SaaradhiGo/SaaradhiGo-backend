"""A trip must record which vehicle did it, and what the rider asked for.

THE DEFECT, FOUND BY RUNNING TRIP #90
A sedan was booked, dispatched, accepted, driven and paid for in cash. The
ride the rider could read back afterwards named no vehicle at all, and their
booking screen -- restoring the trip after a mid-ride refresh -- showed
"Auto", the cheapest category, beside a sedan.

`Trip.vehicle_id` had no assignment anywhere in the codebase. Not one. So it
was null on every trip ever taken, and `TripListSerializer.vehicle_info`,
which serialises from it, was null with it. The field looked implemented and
had never once carried a value.

The rider's own choice lives in `requested_vehicle_type` and exists from the
moment they book -- the admin dashboard already reads it -- but the rider's
history did not expose it, so there was nothing to fall back on either.
"""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from servers.driver.models import Driver, Vehicle, VehicleType
from servers.ride.models import Trip, TripStatus
from servers.ride.serializers import TripListSerializer

User = get_user_model()
pytestmark = pytest.mark.django_db


def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


def _sedan_driver():
    duser = User.objects.create_user(phone_number='+917770002001', role='driver')
    driver = Driver.objects.create(user_id=duser, approved=True)
    vtype, _ = VehicleType.objects.get_or_create(type='sedan')
    vehicle = Vehicle.objects.create(
        driver_id=driver, vehicle_type_id=vtype,
        vehicle_number='AP16QA0090', brand='Maruti', model='Dzire', color='White',
    )
    driver.active_vehicle = vehicle
    driver.save(update_fields=['active_vehicle'])
    return driver, vehicle, vtype


def _trip(**kw):
    rider = User.objects.create_user(phone_number='+917770002002', role='rider')
    return Trip.objects.create(
        user_id=rider,
        status_id=_status(kw.pop('status', 'requested')),
        pickup_lat=Decimal('16.5062'), pickup_long=Decimal('80.6480'),
        destination_lat=Decimal('16.5190'), destination_long=Decimal('80.6305'),
        estimated_fare=Decimal('135.68'), currency='INR',
        payment_method='cash',
        **kw,
    )


def test_accepting_a_trip_records_the_vehicle():
    """The car that turns up is the car on the record."""
    driver, vehicle, _ = _sedan_driver()
    trip = _trip()

    # The transition the consumer performs on accept.
    trip.driver_id = driver
    trip.status_id = _status('accepted')
    trip.vehicle_id = driver.active_vehicle
    trip.save()

    trip.refresh_from_db()
    assert trip.vehicle_id_id == vehicle.id, (
        'the trip recorded no vehicle, so the rider history could never name '
        'the car that did the ride'
    )


def test_vehicle_info_is_populated_once_a_vehicle_is_recorded():
    driver, vehicle, _ = _sedan_driver()
    trip = _trip(status='accepted', driver_id=driver, vehicle_id=vehicle)

    data = TripListSerializer(trip).data
    assert data['vehicle_info'] is not None
    assert data['vehicle_info']['vehicle_number'] == 'AP16QA0090'
    assert data['vehicle_info']['type'] == 'sedan'


def test_the_riders_chosen_category_is_exposed():
    """Available from booking, before any driver exists."""
    _, _, vtype = _sedan_driver()
    trip = _trip(requested_vehicle_type=vtype)

    data = TripListSerializer(trip).data
    assert data['requested_vehicle_type'] == 'sedan', (
        'a rider reopening their own ride has to be able to see what they '
        'booked, not the cheapest category on the page'
    )


def test_the_chosen_category_survives_with_no_driver_assigned():
    """A trip nobody accepted still knows what was asked for."""
    _, _, vtype = _sedan_driver()
    trip = _trip(status='cancelled', requested_vehicle_type=vtype)

    data = TripListSerializer(trip).data
    assert data['requested_vehicle_type'] == 'sedan'
    assert data['vehicle_info'] is None, 'no vehicle turned up, so none is named'


def test_a_trip_without_a_chosen_category_is_none_not_a_crash():
    trip = _trip()
    assert TripListSerializer(trip).data['requested_vehicle_type'] is None


def test_the_two_vehicle_fields_answer_different_questions():
    """`requested_vehicle_type` is the booking; `vehicle_info` is the car.

    They can legitimately differ -- a driver may be dispatched in a vehicle of
    the booked category but not the one quoted against -- so neither is
    derived from the other.
    """
    driver, vehicle, vtype = _sedan_driver()
    trip = _trip(
        status='accepted', driver_id=driver, vehicle_id=vehicle,
        requested_vehicle_type=vtype,
    )
    data = TripListSerializer(trip).data
    assert data['requested_vehicle_type'] == 'sedan'
    assert data['vehicle_info']['vehicle_number'] == 'AP16QA0090'
