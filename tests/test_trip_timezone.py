"""Trips carry their market's time zone (2026-11-08), read-only, so the
rider and driver screens show a trip's times as they were where it happened."""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from servers.pricing.models import ServiceZone
from servers.ride.models import Trip, TripStatus

User = get_user_model()
pytestmark = pytest.mark.django_db
P = Decimal('17.4450000')


def _trip(rider, zone=None):
    status, _ = TripStatus.objects.get_or_create(status_code='completed')
    return Trip.objects.create(user_id=rider, status_id=status, pickup_lat=P, pickup_long=P,
                               destination_lat=P, destination_long=P,
                               estimated_fare=Decimal('118'), zone=zone)


def test_history_and_detail_carry_the_zone_time_zone():
    rider = User.objects.create_user(phone_number='+919811100801', role='rider')
    ring = [[-98, 30], [-97, 30], [-97, 31], [-98, 31], [-98, 30]]
    zone = ServiceZone.objects.create(
        code='TEST-TZ-AUS', name='Test Austin', zone_type='city', country='US',
        polygon_geojson={'type': 'Polygon', 'coordinates': [ring]}, priority=1, is_active=True)
    zone.refresh_from_db()
    assert zone.timezone_name.startswith('America/')
    t = _trip(rider, zone)
    c = APIClient()
    c.force_authenticate(user=rider)
    body = c.get('/api/v1/ride/ride-history/').json()
    rows = (body.get('data') or body)['results']
    assert rows[0]['timezone'] == zone.timezone_name
    detail = c.get(f'/api/v1/ride/trip/{t.id}/').json()
    assert (detail.get('data') or detail)['timezone'] == zone.timezone_name


def test_a_trip_without_a_zone_uses_the_deployment_default():
    rider = User.objects.create_user(phone_number='+919811100802', role='rider')
    _trip(rider)
    c = APIClient()
    c.force_authenticate(user=rider)
    body = c.get('/api/v1/ride/ride-history/').json()
    tz = (body.get('data') or body)['results'][0]['timezone']
    assert tz == 'Asia/Kolkata'
