"""The driver's totals for today, this week and this month (2026-11-08).

Read-only, over the same settlement ledger as /summary/ and /daily/: the
week's net equals the sum of its days, net = gross - commission, and the
range is reported for the label ("Mon 2 - Sun 8 Nov").
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
    u = User.objects.create_user(phone_number='+919800000792', role='driver')
    d = Driver.objects.create(user_id=u, approved=True)
    Vehicle.objects.create(driver_id=d, vehicle_type_id=vt, vehicle_number='TS09GP0792')
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


def _get(client, path):
    body = client.get('/api/v1/driver/earnings/' + path).json()
    return body.get('data', body)


def test_week_runs_monday_to_sunday(driver):
    from datetime import date
    w = _get(_api(driver), 'period/?period=week')
    start, end = date.fromisoformat(w['start']), date.fromisoformat(w['end'])
    assert start.weekday() == 0 and (end - start).days == 6
    assert w['trips'] == 0 and Decimal(w['net']) == 0
    assert w['currency'] and w['timezone']


def test_lines_add_up_and_match_the_days(driver):
    _settle(driver, '200', 'online')
    _settle(driver, '185', 'cash')
    client = _api(driver)
    for period in ('today', 'week', 'month'):
        p = _get(client, f'period/?period={period}')
        assert p['trips'] == 2
        assert Decimal(p['gross']) - Decimal(p['commission']) == Decimal(p['net'])
    today = _get(client, 'period/?period=today')
    day = _get(client, 'daily/?days=1')['days'][-1]
    assert Decimal(today['net']) == Decimal(day['net'])
    assert Decimal(today['cash_collected']) > 0


def test_unknown_period_is_refused(driver):
    r = _api(driver).get('/api/v1/driver/earnings/period/?period=year')
    assert r.status_code == 400


def test_riders_cannot_read_it(auth_client_rider):
    client, _ = auth_client_rider
    assert client.get('/api/v1/driver/earnings/period/').status_code in (401, 403)
