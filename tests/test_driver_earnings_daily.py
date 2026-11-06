"""The driver's last 7 days, for the earnings bar chart (2026-11-06).

A read-only aggregation of the same settlement ledger the summary reads, so
today's bar equals the summary's "today". Every day is present, zeros
included, oldest first.
"""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from servers.driver.models import Driver, Vehicle, VehicleType
from servers.driver.utils import credit_driver_wallet
from servers.ride.models import Trip, TripStatus

User = get_user_model()

pytestmark = pytest.mark.django_db

LAT, LNG = Decimal('17.4450000'), Decimal('78.3800000')


@pytest.fixture
def driver():
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    u = User.objects.create_user(phone_number='+919800000791', role='driver')
    d = Driver.objects.create(user_id=u, approved=True)
    Vehicle.objects.create(driver_id=d, vehicle_type_id=vt, vehicle_number='TS09GP0791')
    return d


def _settle(driver, fare, method):
    rider = User.objects.create_user(phone_number=f'+91970000{int(fare):04d}', role='rider')
    status, _ = TripStatus.objects.get_or_create(status_code='completed')
    t = Trip.objects.create(
        user_id=rider, status_id=status, pickup_lat=LAT, pickup_long=LNG,
        destination_lat=LAT, destination_long=LNG, estimated_fare=Decimal(fare),
        payment_method=method, commission_percent_snapshot=Decimal('15.00'),
    )
    t.driver_id = driver
    t.completed_at = timezone.now()
    t.save(update_fields=['driver_id', 'completed_at'])
    credit_driver_wallet(t)


def _api(driver):
    c = APIClient()
    c.force_authenticate(user=driver.user_id)
    return c


def _get(client, query=''):
    body = client.get('/api/v1/driver/earnings/daily/' + query).json()
    return body.get('data', body)


def test_seven_days_oldest_first_with_zeros(driver):
    days = _get(_api(driver))['days']
    assert len(days) == 7
    assert [d['date'] for d in days] == sorted(d['date'] for d in days)
    assert all(d['trips'] == 0 and Decimal(d['net']) == 0 for d in days)


def test_today_matches_the_summary(driver):
    _settle(driver, '200', 'online')
    _settle(driver, '185', 'cash')
    client = _api(driver)
    today = _get(client)['days'][-1]
    summary = _get_summary(client)
    assert today['trips'] == summary['today_trips'] == 2
    assert Decimal(today['gross']) == Decimal(summary['today_earned'])
    assert Decimal(today['net']) == Decimal(summary['today_net'])
    assert Decimal(today['commission']) == Decimal(summary['today_commission'])
    assert Decimal(today['gross']) == Decimal(today['net']) + Decimal(today['commission'])


def _get_summary(client):
    body = client.get('/api/v1/driver/earnings/summary/').json()
    return body.get('data', body)


def test_days_is_bounded(driver):
    client = _api(driver)
    assert len(_get(client, '?days=0')['days']) == 1
    assert len(_get(client, '?days=400')['days']) == 31
    assert len(_get(client, '?days=x')['days']) == 7


def test_riders_cannot_read_it():
    rider = User.objects.create_user(phone_number='+919700000792', role='rider')
    c = APIClient()
    c.force_authenticate(user=rider)
    assert c.get('/api/v1/driver/earnings/daily/').status_code in (401, 403)
