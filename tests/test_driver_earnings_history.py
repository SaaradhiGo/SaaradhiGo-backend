"""Earnings history (10 Nov v2): any period, every month since joining, statements.

The numbers on screen equal the ledger for day, week, month, year, the Indian tax year and a
custom range; the statement CSV and PDF carry the same totals.
"""
import csv
import io
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from base.market_time import driver_timezone
from servers.driver import earnings_history as eh
from servers.rider.models import Wallet, WalletTransaction
from test_driver_earnings_period import _api, _settle, driver  # noqa: F401

pytestmark = pytest.mark.django_db


def _get(client, path):
    r = client.get('/api/v1/driver/earnings/' + path)
    assert r.status_code == 200, r.content[:300]
    body = r.json()
    return body.get('data', body)


def _fee(driver, minor):
    WalletTransaction.objects.create(user_id=driver.user_id, amount_minor=minor, currency='INR', txn_type='debit',
                                     status='completed', purpose='platform_fee', reference_id='DAY_20261110')


def test_period_bounds():
    d = date(2026, 11, 10)
    assert eh.bounds('week', d) == (date(2026, 11, 9), date(2026, 11, 15))
    assert eh.bounds('month', d) == (date(2026, 11, 1), date(2026, 11, 30))
    assert eh.bounds('taxyear', d) == (date(2026, 4, 1), date(2027, 3, 31))
    assert eh.bounds('taxyear', date(2026, 2, 1)) == (date(2025, 4, 1), date(2026, 3, 31))
    assert eh.shift('month', date(2026, 11, 1), date(2026, 11, 30), -1) == date(2026, 10, 1)
    with pytest.raises(eh.BadPeriod):
        eh.bounds('custom', d, date(2026, 11, 5), date(2026, 11, 1))


def test_screen_equals_ledger_for_every_period(driver):  # noqa: F811
    Wallet.objects.get_or_create(user_id=driver.user_id, scope=Wallet.SCOPE_DRIVER, defaults={'currency': 'INR'})
    _settle(driver, '200', 'online')
    _settle(driver, '185', 'cash')
    _fee(driver, 3000)
    client = _api(driver)
    today = timezone.localdate()
    queries = ['day', 'week', 'month', 'year', 'taxyear',
               f'custom&start={(today - timedelta(days=3)).isoformat()}&end={today.isoformat()}']
    for q in queries:
        h = _get(client, f'history/?period={q}')
        s = h['summary']
        assert s['trips'] == 2, q
        assert Decimal(s['fares']) - Decimal(s['platform_fee']) + Decimal(s['tips']) == Decimal(s['earned']), q
        tz = driver_timezone(driver)
        since = eh._start(tz, date.fromisoformat(h['start']))
        until = eh._start(tz, date.fromisoformat(h['end']) + timedelta(days=1))
        moved = Decimal(eh.ledger_movement(driver, since, until)) / 100
        assert Decimal(s['earned']) - Decimal(s['cash_collected']) == moved, q
        assert sum(Decimal(b['earned']) for b in h['chart']['bars']) == Decimal(s['earned']), q
        assert h['has_next'] is False
    assert _get(client, 'history/?period=day')['chart']['unit'] == 'hour'
    assert _get(client, 'history/?period=year')['chart']['unit'] == 'month'
    assert client.get('/api/v1/driver/earnings/history/?period=fortnight').status_code == 400


def test_months_and_statements_match_the_screen(driver):  # noqa: F811
    Wallet.objects.get_or_create(user_id=driver.user_id, scope=Wallet.SCOPE_DRIVER, defaults={'currency': 'INR'})
    _settle(driver, '240', 'online')
    _fee(driver, 1000)
    client = _api(driver)
    months = _get(client, 'months/')['months']
    assert months[0]['so_far'] is True and months[0]['trips'] == 1
    key = months[0]['month']
    screen = _get(client, f'history/?period=month&date={key}-01')['summary']
    assert months[0]['earned'] == screen['earned']

    st = _get(client, f'statement/?month={key}')
    assert st['summary']['earned'] == screen['earned'] and len(st['trips']) == 1

    r = client.get(f'/api/v1/driver/earnings/statement/?month={key}&as=csv')
    assert r['Content-Type'].startswith('text/csv')
    rows = {row[0]: row[1:] for row in csv.reader(io.StringIO(r.content.decode())) if row}
    assert rows['You earned'][0] == screen['earned'] and rows['Trip fares'][0] == screen['fares']

    pdf = client.get(f'/api/v1/driver/earnings/statement/?month={key}&as=pdf')
    assert pdf['Content-Type'] == 'application/pdf' and pdf.content[:4] == b'%PDF'
    assert client.get('/api/v1/driver/earnings/statement/?month=2026-13').status_code == 400


def test_the_trips_of_one_day(driver):  # noqa: F811
    _settle(driver, '150', 'online')
    client = _api(driver)
    today = timezone.localdate(timezone=driver_timezone(driver)).isoformat()
    got = _get(client, f'?date={today}&page_size=50')
    assert len(got['results']) == 1
    assert _get(client, '?date=2001-01-01&page_size=50')['results'] == []
    assert client.get('/api/v1/driver/earnings/?date=yesterday').status_code == 400
