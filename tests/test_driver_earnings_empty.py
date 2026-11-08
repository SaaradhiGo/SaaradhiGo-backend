"""A driver with no trips, no ledger rows and no payouts yet (2026-11-08).

Every earnings and payout read answers 200 with zeros, never 404, 500 or
null, and says which currency the zeros are in, so the driver's screens show
"₹0.00" and seven empty days instead of waiting forever.
"""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from servers.driver.models import Driver

User = get_user_model()

pytestmark = pytest.mark.django_db


@pytest.fixture
def client():
    u = User.objects.create_user(phone_number='+919800001108', role='driver')
    Driver.objects.create(user_id=u)
    c = APIClient()
    c.force_authenticate(user=u)
    return c


def _get(client, path):
    r = client.get('/api/v1/driver/' + path)
    assert r.status_code == 200, (path, r.status_code)
    body = r.json()
    return body.get('data', body)


def test_period_is_zero(client):
    for period in ('today', 'week', 'month'):
        p = _get(client, f'earnings/period/?period={period}')
        assert p['trips'] == 0
        assert Decimal(p['gross']) == Decimal(p['net']) == Decimal(p['cash_collected']) == 0
        assert p['currency'] and p['timezone']


def test_seven_empty_days_with_currency(client):
    d = _get(client, 'earnings/daily/?days=7')
    assert len(d['days']) == 7
    assert all(x['trips'] == 0 and Decimal(x['net']) == 0 for x in d['days'])
    assert d['currency'] and d['timezone']


def test_summary_is_zero_with_currency(client):
    s = _get(client, 'earnings/summary/')
    assert s['today_trips'] == 0 and Decimal(s['today_net']) == 0 and Decimal(s['total_net']) == 0
    assert s['currency']


def test_lists_and_balance_are_empty_not_missing(client):
    assert _get(client, 'earnings/?page_size=50')['count'] == 0
    assert _get(client, 'withdrawals/history/')['count'] == 0
    assert Decimal(str(_get(client, 'withdrawals/balance/')['available_balance'])) == 0
