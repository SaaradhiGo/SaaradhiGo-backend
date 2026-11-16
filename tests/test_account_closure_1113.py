"""Closing an account (13 Nov): riders go at once; a driver who is owed earnings waits for them.

Play Store policy needs in-app deletion; Srini's rule is that pending payouts settle before the
account closes. Uses the US driver of the fraud-money world: $5 is below the $10 minimum.
"""
from datetime import datetime, timezone as dt_tz

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from fraud_money_support import credit, geo, seed_money, us_driver, world  # noqa: F401


@pytest.fixture
def finance_world_fixture(geo, settings):  # noqa: F811
    from test_finance_actions import world as finance_world
    return finance_world.__wrapped__(geo, settings)

pytestmark = pytest.mark.django_db
User = get_user_model()
URL = '/api/v1/auth/me/delete/'


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def test_a_rider_is_closed_at_once():
    rider = User.objects.create_user(phone_number='+919700000101', password='x-Long-pass-1', role='rider')
    c = _client(rider)
    preview = c.get(URL).json()['data']
    assert preview['pending_payouts'] is None and preview['closure'] is None
    assert any('Trip records' in k for k in preview['kept']) and preview['web_url'].endswith('/delete-account')
    r = c.post(URL, {'confirm': 'DELETE'}, format='json')
    assert r.status_code == 200 and r.json()['data']['status'] == 'closed'
    rider.refresh_from_db()
    assert not rider.is_active and rider.phone_number.startswith('deleted-')


def test_a_driver_owed_money_closes_only_after_the_payout(world):  # noqa: F811
    from servers.auth_user.closure import sweep
    from servers.driver.models import WithdrawalRequest
    from servers.driver.qa_eligibility import may_operate
    from servers.payments.models import PayoutCountryConfig
    from servers.payments.weekly_payout import run_country
    from servers.rider.models import Wallet

    seed_money()
    drv = us_driver(world, 'C1')
    credit(drv.user_id, 500, currency='USD')          # $5.00, below the $10 minimum
    c = _client(drv.user_id)
    assert c.get(URL).json()['data']['pending_payouts'] == {'balance': '5.00', 'currency': 'USD', 'in_flight': 0}

    r = c.post(URL, {'confirm': 'DELETE', 'reason': 'moving on'}, format='json')
    assert r.status_code == 202 and r.json()['data']['status'] == 'waiting_payouts'
    drv.refresh_from_db()
    assert drv.status == 'offline' and not may_operate(drv)
    assert c.get(URL).json()['data']['closure']['status'] == 'waiting_payouts'
    assert sweep() == 0                                 # still owed: stays open

    config = PayoutCountryConfig.objects.get(country='US', is_active=True)
    result = run_country(config, datetime(2026, 11, 2, 18, 0, tzinfo=dt_tz.utc))
    assert result['withdrawals'] == 1                   # paid with no minimum
    w = WithdrawalRequest.objects.get(driver=drv)
    assert w.amount_minor == 500
    assert sweep() == 0                                 # in flight: stays open

    WithdrawalRequest.objects.filter(pk=w.pk).update(status='completed')
    assert Wallet.objects.get(user_id=drv.user_id, scope=Wallet.SCOPE_DRIVER).balance_minor == 0
    assert sweep() == 1
    drv.user_id.refresh_from_db()
    assert not drv.user_id.is_active and drv.user_id.phone_number.startswith('deleted-')


# --- the driver OWES SaaradhiGo (13 Nov follow-up) ---------------------------------------------

def test_a_driver_who_owes_stays_open_until_finance_writes_it_off(finance_world_fixture):
    """A negative balance (e.g. commission on cash trips): the confirm screen says so, the amount is
    kept on record, and the account closes only once it is paid or written off through maker-checker."""
    from servers.auth_user.closure import sweep
    from servers.auth_user.models import AccountClosure
    from servers.rider.models import Wallet
    from test_finance_actions import BASE, approve_request, body_of, checker, post

    w = finance_world_fixture
    drv = w.in_driver
    from servers.driver.models import WithdrawalRequest
    WithdrawalRequest.objects.filter(driver=drv).update(status='completed')   # nothing in flight
    Wallet.objects.filter(user_id=drv.user_id, scope='driver').update(balance_minor=-50000)
    c = _client(drv.user_id)
    owed = c.get(URL).json()['data']['owed_to_saaradhigo']
    assert owed['amount'] == '500.00' and 'writes it off' in owed['message']

    r = c.post(URL, {'confirm': 'DELETE'}, format='json')
    assert r.status_code == 202 and r.json()['data']['status'] == 'waiting_settlement'
    row = AccountClosure.objects.get(user=drv.user_id)
    assert (row.owed_minor, row.currency) == (50000, 'INR')
    drv.refresh_from_db()
    assert drv.account_closing and drv.status == 'offline'
    assert sweep() == 0

    maker, _ = w.finance
    r = post(maker, f'{BASE}/drivers/{drv.id}/adjust/', {
        'direction': 'credit', 'amount_minor': 50000, 'currency': 'INR',
        'reason_code': 'closure_write_off', 'note': 'Driver left; dues below collection cost',
        'idempotency_key': 'wo-1'}, expect=202)
    assert body_of(r)['state'] == 'pending_approval'   # always a checker, whatever the limit
    assert sweep() == 0                                 # not written off yet

    chk, _ = checker()
    assert approve_request(chk, body_of(r)['approval_request']['id']).status_code == 200
    assert Wallet.objects.get(user_id=drv.user_id, scope='driver').balance_minor == 0
    assert sweep() == 1
    drv.user_id.refresh_from_db()
    assert not drv.user_id.is_active
    row.refresh_from_db()
    assert row.status == 'closed' and row.owed_minor == 50000   # still on record
