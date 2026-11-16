"""15 Nov (US polish round): every driver money screen counts tips as the cards do; dates in the
market's style (US "Nov 14, 2026", India keeps "14 Nov"); a fee-plan market with no approved plan
says "No platform fee"."""
from datetime import date, datetime, timezone as dt_tz
from decimal import Decimal

import pytest

from base.market_dates import clock, date_range, date_time, full_date, weekday_date
from servers.payments.models import Tip
from test_driver_earnings_period import _api, _settle, driver  # noqa: F401

pytestmark = pytest.mark.django_db


def _data(client, path):
    body = client.get('/api/v1/driver/' + path).json()
    return body.get('data', body)


def _tipped(driver):  # noqa: F811
    from servers.ride.models import Trip

    _settle(driver, '240', 'online')
    trip = Trip.objects.filter(driver_id=driver).latest('id')
    Tip.objects.create(trip=trip, rider=trip.user_id, driver=driver, amount_minor=4300, currency='INR',
                       status='succeeded')
    return trip


def test_dates_in_the_market_style():
    d = date(2026, 11, 14)
    assert weekday_date(d, 'US') == 'Sat, Nov 14' and weekday_date(d, 'IN') == 'Sat, 14 Nov'
    assert full_date(d, 'US') == 'Nov 14, 2026' and full_date(d, 'IN') == '14 Nov 2026'
    assert date_range(date(2026, 11, 9), d, 'US') == 'Nov 9 – 14'
    assert date_range(date(2026, 11, 9), date(2026, 11, 15), 'IN') == '9 – 15 Nov'
    at = datetime(2026, 11, 14, 19, 30, tzinfo=dt_tz.utc)
    assert clock(at, 'US') == '7:30 PM' and clock(at, 'IN') == '19:30'
    assert date_time(at, 'US') == 'Nov 14, 2026, 7:30 PM'
    assert clock(datetime(2026, 11, 14, 0, 5), 'US') == '12:05 AM'


def test_the_seven_day_chart_counts_tips(driver):  # noqa: F811
    _tipped(driver)
    client = _api(driver)
    today = _data(client, 'earnings/period/?period=today')
    day = _data(client, 'earnings/daily/?days=7')['days'][-1]
    assert Decimal(day['tips']) == Decimal('43.00')
    assert Decimal(day['earned']) == Decimal(today['earned'])


def test_each_trip_row_carries_its_tip(driver):  # noqa: F811
    trip = _tipped(driver)
    rows = _data(_api(driver), 'earnings/')['results']
    row = next(r for r in rows if r['trip_id_val'] == trip.id)
    assert Decimal(row['tip']) == Decimal('43.00')


def test_history_bars_and_the_statement_count_tips(driver):  # noqa: F811
    from servers.driver.earnings_history import history, statement, statement_csv

    trip = _tipped(driver)
    h = history(driver, 'week')
    assert sum(Decimal(b['tips']) for b in h['chart']['bars']) == Decimal('43.00')
    assert sum(Decimal(b['earned']) for b in h['chart']['bars']) == Decimal(h['summary']['earned'])
    from base.market_time import driver_timezone

    today = datetime.now(driver_timezone(driver)).date()
    st = statement(driver, today.year, today.month)
    row = next(t for t in st['trips'] if t['trip_id'] == trip.id)
    assert Decimal(row['tip']) == Decimal('43.00')
    # No daily fee here, so the rows add up to the card.
    assert sum(Decimal(t['earned']) for t in st['trips']) == Decimal(st['summary']['earned'])
    assert 'Date and time,Trip,Paid by,Fare,Fee,Tip,You earned' in statement_csv(st)


def test_a_us_statement_writes_us_dates():
    from servers.driver.earnings_history import _day, _when

    st = {'country': 'US'}
    assert _day(st, '2026-11-14') == 'Nov 14, 2026'
    assert _when(st, '2026-11-14T19:30:00-07:00') == 'Nov 14, 2026, 7:30 PM'
    assert _day({'country': 'IN'}, '2026-11-14') == '14/11/2026'


def test_no_plan_in_a_fee_plan_market_says_no_platform_fee(driver):  # noqa: F811
    from unittest import mock

    from servers.pricing import fee_plans

    with mock.patch.object(fee_plans, 'resolve', return_value=None), \
            mock.patch.object(fee_plans, '_fee_plan_market', return_value=True):
        assert fee_plans.fee_words(driver) == 'No platform fee'
    with mock.patch.object(fee_plans, 'resolve', return_value=None), \
            mock.patch.object(fee_plans, '_fee_plan_market', return_value=False):
        assert fee_plans.fee_words(driver) == 'A commission on each trip, shown on every trip'


def test_a_completed_trip_whose_payment_failed_is_listed_apart(driver):  # noqa: F811
    from servers.ride.models import Trip

    _settle(driver, '240', 'online')
    settled = Trip.objects.filter(driver_id=driver).latest('id')
    failed = Trip.objects.get(pk=settled.pk)
    failed.pk = None
    failed.payment_status = 'failed'
    failed.save()
    body = _data(_api(driver), 'earnings/')
    assert [r['trip_id_val'] for r in body['unsettled']] == [failed.id]
    assert body['unsettled'][0]['net_amount'] is None
    assert failed.id not in [r['trip_id_val'] for r in body['results']]
    detail = _data(_api(driver), f'trips/{failed.id}/')
    assert detail['payment'] == 'not_collected'
