"""INR fares are whole rupees from the quote on (owner, 2026-11-05); USD keeps cents.

The locked fare, what the trip charges, the driver's settlement and the receipt
must be one number. Runs the real money path the parity test uses.
"""

from decimal import Decimal

import pytest

from base.money import round_fare
from tests.test_money_minor_units_parity import _people, _promos, _run_trip, market  # noqa: F401

pytestmark = pytest.mark.django_db

WHOLE = Decimal('1')


def _whole(amount):
    return Decimal(amount) == Decimal(amount).to_integral_value()


@pytest.mark.parametrize('label, payment, promo', [
    ('cash', 'cash', None),
    ('online', 'online', None),
    ('flat promo', 'cash', 'PARITYFLAT'),
    ('percent promo', 'wallet', 'PARITYPCT'),
])
def test_quoted_charged_and_receipt_are_the_same_whole_rupees(market, label, payment, promo):  # noqa: F811
    _promos(market)
    rider, driver = _people()
    out = _run_trip(market, rider, driver,
                    (label, 'IN', '3.33', '11', '1.00', False, payment, promo))
    quoted = out['quote']['total_fare']
    charged = out['trip']['final_fare'] or out['trip']['estimated_fare']
    assert _whole(quoted), f'{label}: quote {quoted} is not whole rupees'
    assert out['trip']['estimated_fare'] == quoted
    assert _whole(charged)
    assert out['settlement']['gross_fare'] == charged
    assert out['receipt']['total_fare'] == charged
    if promo:
        assert Decimal(out['promo']['discount']) == Decimal(quoted) - Decimal(charged)


def test_usd_keeps_cents(market):  # noqa: F811
    _promos(market)
    rider, driver = _people()
    out = _run_trip(market, rider, driver,
                    ('us', 'US', '3.33', '11', '1.00', False, 'online', None))
    assert out['quote']['currency'] == 'USD'
    assert not _whole(out['quote']['total_fare'])
    assert out['receipt']['total_fare'] == out['trip']['estimated_fare']


def test_round_fare_is_half_up():
    assert round_fare(Decimal('121.50'), 'INR') == Decimal('122.00')
    assert round_fare(Decimal('121.49'), 'INR') == Decimal('121.00')
    assert round_fare(Decimal('12.345'), 'USD') == Decimal('12.35')
