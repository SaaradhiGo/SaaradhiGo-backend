"""15 Nov (US round 4): driver screens date a trip by when it ENDED, not when its money settled
(trip 219 ended on the 14th and settled on the 15th), and the trip detail shows when its earnings
were paid out."""
from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone

from servers.driver.models import WithdrawalRequest
from servers.payments import unpaid
from test_driver_earnings_period import _api, _get, driver  # noqa: F401
from test_unpaid_trips_1115 import _failed_trip, _rider

pytestmark = pytest.mark.django_db


def _late_settled(driver, ended):  # noqa: F811
    trip = _failed_trip(driver, _rider())
    trip.completed_at = ended
    trip.save(update_fields=['completed_at'])
    with mock.patch('servers.payments.card_hold._gateway', return_value=None):
        unpaid.sweep()  # settles now, two days after the trip ended
    return trip


def test_a_late_settled_trip_is_listed_and_dated_on_the_day_it_ended(driver):  # noqa: F811
    from base.market_time import driver_timezone

    ended = timezone.now() - timedelta(days=2)
    trip = _late_settled(driver, ended)
    day = ended.astimezone(driver_timezone(driver)).date()
    today = timezone.now().astimezone(driver_timezone(driver)).date()

    rows = _get(_api(driver), f'?date={day}')['results']
    row = next(r for r in rows if r['trip_id_val'] == trip.id)
    assert row['created_at'] == ended.isoformat()
    assert row['settled_at'] > row['created_at']
    assert trip.id not in [r['trip_id_val'] for r in _get(_api(driver), f'?date={today}')['results']]


def test_the_trip_detail_shows_the_payout_that_carried_it(driver):  # noqa: F811
    trip = _late_settled(driver, timezone.now() - timedelta(hours=3))
    c = _api(driver)

    def detail():
        body = c.get(f'/api/v1/driver/trips/{trip.id}/').json()
        return body.get('data', body)

    assert detail()['paid_out_at'] is None and detail()['settled_at']
    # A payout smaller than what the trip brought in does not cover it; the next one does.
    paid = timezone.now() - timedelta(minutes=30)
    WithdrawalRequest.objects.create(driver=driver, amount_minor=100, status='completed',
                                     processed_at=paid - timedelta(minutes=5), currency=trip.currency)
    assert detail()['paid_out_at'] is None
    WithdrawalRequest.objects.create(driver=driver, amount_minor=20400, status='completed',
                                     processed_at=paid, currency=trip.currency)
    WithdrawalRequest.objects.create(driver=driver, amount_minor=99999, status='failed',
                                     processed_at=paid - timedelta(minutes=10), currency=trip.currency)
    from base.market_time import driver_timezone

    assert detail()['paid_out_at'][:16] == paid.astimezone(driver_timezone(driver)).isoformat()[:16]
