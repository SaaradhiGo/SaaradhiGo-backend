"""The rider's "driver arriving" card: the driver's photo and completed trips (2026-11-06).

Both already exist (the user's avatar, Driver.total_trips, incremented when a
trip completes). The trip payload now carries them to the trip's own rider
only; anyone else gets None. The photo is a short-lived signed URL or None,
never a permanent link.
"""

from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from servers.driver.models import Driver
from servers.ride.models import Trip, TripStatus
from servers.ride.serializers import TripDetailSerializer
from servers.rider.models import Rider

User = get_user_model()

pytestmark = pytest.mark.django_db


def _trip():
    rider = User.objects.create_user(phone_number='+919521100001', role='rider',
                                     username='+919521100001')
    Rider.objects.create(user_id=rider)
    du = User.objects.create_user(phone_number='+919531100001', role='driver',
                                  username='+919531100001')
    driver = Driver.objects.create(user_id=du, approved=True, total_trips=212)
    status, _ = TripStatus.objects.get_or_create(status_code='accepted')
    trip = Trip.objects.create(
        user_id=rider, driver_id=driver, status_id=status,
        pickup_lat=Decimal('17.445'), pickup_long=Decimal('78.38'),
        destination_lat=Decimal('17.455'), destination_long=Decimal('78.39'),
        estimated_fare=Decimal('169'), payment_method='cash', accepted_at=timezone.now(),
    )
    return trip, rider, du


def _as(user):
    return {'request': SimpleNamespace(user=user)}


def test_the_trips_rider_sees_the_driver_card():
    trip, rider, _ = _trip()
    data = TripDetailSerializer(trip, context=_as(rider)).data
    assert data['driver_total_trips'] == 212
    # No photo uploaded: None, never a placeholder.
    assert data['driver_photo_url'] is None


def test_a_photo_is_a_signed_url():
    trip, rider, _ = _trip()
    with mock.patch('servers.driver.document_access.signed_url', return_value='https://signed.example/x?sig=1'):
        data = TripDetailSerializer(trip, context=_as(rider)).data
    assert data['driver_photo_url'] == 'https://signed.example/x?sig=1'


def test_anyone_else_gets_nothing():
    trip, _, _ = _trip()
    stranger = User.objects.create_user(phone_number='+919521100099', role='rider',
                                        username='+919521100099')
    data = TripDetailSerializer(trip, context=_as(stranger)).data
    assert data['driver_total_trips'] is None
    assert data['driver_photo_url'] is None


def test_no_driver_yet_is_none():
    trip, rider, _ = _trip()
    trip.driver_id = None
    data = TripDetailSerializer(trip, context=_as(rider)).data
    assert data['driver_total_trips'] is None
    assert data['driver_photo_url'] is None
