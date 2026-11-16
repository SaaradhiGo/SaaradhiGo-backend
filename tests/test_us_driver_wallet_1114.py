"""QA, 14 Nov (USA journey): a US driver's earnings showed rupees, and the month list and the
monthly statement left out the tip the dashboard counted.

  * a new wallet opens in the driver's market currency, never the platform default (INR);
  * the month list and the statement include the month's tips, like the history;
  * the integrity repair relabels a driver wallet opened in the default only when every entry is
    already in the market's currency; anything mixed is reported and left alone.
"""
from decimal import Decimal

import pytest

from servers.driver.models import Driver
from servers.payments.models import Tip
from servers.rider.models import Wallet, WalletTransaction, get_wallet
from test_driver_earnings_period import _api, _settle, driver  # noqa: F401

pytestmark = pytest.mark.django_db


def _us_driver():
    from django.contrib.auth import get_user_model
    u = get_user_model().objects.create_user(phone_number='+12085550199', role='driver')
    return Driver.objects.create(user_id=u, approved=True)


def test_a_us_drivers_new_wallet_opens_in_dollars():
    d = _us_driver()
    assert get_wallet(d.user_id, scope=Wallet.SCOPE_DRIVER).currency == 'USD'


def test_an_india_drivers_new_wallet_opens_in_rupees(driver):  # noqa: F811
    assert get_wallet(driver.user_id, scope=Wallet.SCOPE_DRIVER).currency == 'INR'


def test_the_month_list_and_statement_count_the_tip(driver):  # noqa: F811
    Wallet.objects.get_or_create(user_id=driver.user_id, scope=Wallet.SCOPE_DRIVER, defaults={'currency': 'INR'})
    _settle(driver, '240', 'online')
    from servers.ride.models import Trip
    trip = Trip.objects.filter(driver_id=driver).latest('id')
    Tip.objects.create(trip=trip, rider=trip.user_id, driver=driver, amount_minor=4300, currency='INR',
                       status='succeeded')
    client = _api(driver)
    get = lambda p: (lambda b: b.get('data', b))(client.get('/api/v1/driver/earnings/' + p).json())  # noqa: E731
    months = get('months/')['months']
    key = months[0]['month']
    screen = get(f'history/?period=month&date={key}-01')['summary']
    assert Decimal(screen['tips']) == Decimal('43.00')
    assert months[0]['tips'] == screen['tips'] and months[0]['earned'] == screen['earned']
    st = get(f'statement/?month={key}')
    assert st['summary']['tips'] == screen['tips'] and st['summary']['earned'] == screen['earned']


def test_the_repair_relabels_only_a_wallet_whose_entries_are_all_in_the_market_currency():
    from servers.platform_ops.integrity import _wrong_currency_wallets

    clean = _us_driver()
    w = Wallet.objects.create(user_id=clean.user_id, scope=Wallet.SCOPE_DRIVER, currency='INR')
    WalletTransaction.objects.create(user_id=clean.user_id, amount_minor=2808, currency='USD', txn_type='credit',
                                     status='completed', purpose='trip')
    from django.contrib.auth import get_user_model
    u = get_user_model().objects.create_user(phone_number='+12085550198', role='driver')
    mixed = Driver.objects.create(user_id=u, approved=True)
    m = Wallet.objects.create(user_id=u, scope=Wallet.SCOPE_DRIVER, currency='INR')
    for cur in ('USD', 'INR'):
        WalletTransaction.objects.create(user_id=u, amount_minor=100, currency=cur, txn_type='credit',
                                         status='completed', purpose='trip')

    report = _wrong_currency_wallets(fix=False)
    assert {clean.id, mixed.id} <= set(report['drivers']) and report['fixed'] == 0
    w.refresh_from_db()
    assert w.currency == 'INR'

    report = _wrong_currency_wallets(fix=True)
    w.refresh_from_db()
    m.refresh_from_db()
    assert w.currency == 'USD' and m.currency == 'INR'
    assert mixed.id in report['mixed_left_alone']
