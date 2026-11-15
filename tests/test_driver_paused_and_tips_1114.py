"""14 Nov (owner): a suspended driver is told the account is paused, why, and since when (the
dashboard said "Finish setting up"); and "Today you earned" includes tips."""
from decimal import Decimal

import pytest

from servers.payments.models import Tip
from test_driver_earnings_period import _api, _settle, driver  # noqa: F401

pytestmark = pytest.mark.django_db


def _data(client, path):
    body = client.get('/api/v1/driver/' + path).json()
    return body.get('data', body)


def test_a_paused_driver_is_told_why(driver):  # noqa: F811
    from servers.driver.admin_drivers import operator_suspend

    client = _api(driver)
    assert _data(client, 'requirements/')['paused'] is None
    outcome, _ = operator_suspend(driver.id, reason_code='zero_tolerance', note='operator note, never shown')
    assert outcome == 'suspended'
    paused = _data(client, 'requirements/')['paused']
    assert paused['reason_code'] == 'zero_tolerance' and paused['permanent'] is False
    assert 'zero tolerance' in paused['reason'] and 'investigates' in paused['reason']
    assert 'operator note' not in str(paused)
    assert paused['since']


def test_today_you_earned_includes_tips(driver):  # noqa: F811
    from servers.ride.models import Trip

    _settle(driver, '240', 'online')
    trip = Trip.objects.filter(driver_id=driver).latest('id')
    Tip.objects.create(trip=trip, rider=trip.user_id, driver=driver, amount_minor=4300, currency='INR',
                       status='succeeded')
    today = _data(_api(driver), 'earnings/period/?period=today')
    assert Decimal(today['tips']) == Decimal('43.00')
    assert Decimal(today['earned']) == Decimal(today['net']) + Decimal('43.00')
