"""Neither side of a trip receives the other's real number (masked calls, 2026-11-09).

The rider app used to dial `driver_phone` from the trip details; calls now go through
/calls/ (servers/callbridge). The keys stay, always null, for installed apps.
"""

from decimal import Decimal as D

import pytest

from servers.ride.models import Trip, TripStatus

pytestmark = pytest.mark.django_db


def _trip(rider, driver, code='accepted'):
    st, _ = TripStatus.objects.get_or_create(status_code=code)
    return Trip.objects.create(
        user_id=rider, driver_id=driver.driver, pickup_lat=D('17.4'), pickup_long=D('78.4'),
        destination_lat=D('17.45'), destination_long=D('78.36'), estimated_fare=D('150.00'),
        status_id=st)


def _digits(user):
    return user.phone_number.lstrip('+')[-10:]


def test_trip_details_carry_no_driver_number(auth_client_rider, auth_client_driver):
    client, rider = auth_client_rider
    driver = auth_client_driver[1]
    trip = _trip(rider, driver)
    for _ in range(2):  # database path, then whatever the cache holds
        r = client.get(f'/api/v1/ride/trip/{trip.id}/details/')
        assert r.status_code == 200
        assert r.json()['data']['driver_phone'] is None
        assert _digits(driver) not in r.content.decode()


def test_rider_name_for_the_driver_never_falls_back_to_the_number(auth_client_rider):
    from asgiref.sync import async_to_sync

    from servers.consumers import RideRequestConsumer

    rider = auth_client_rider[1]
    rider.full_name = ''
    rider.save()
    consumer = RideRequestConsumer()
    consumer.user = rider
    assert async_to_sync(consumer._get_rider_name)() == ''
