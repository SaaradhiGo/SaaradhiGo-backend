"""No float reaches money, and every stored amount is integer minor units.

Kept from the original 1(c) brief ("a test that fails if a float reaches any
money field or calculation") and carried over to the minor-units decision.
Four layers, cheapest first:

1. The helper refuses float, bool, NaN and infinity -- the one place
   that rounds is also the one place that checks.
2. EVERY `MinorUnitsAmount` on EVERY model refuses a float, found by walking
   the app registry, so a money attribute added tomorrow is covered the moment
   it exists.
3. The schema: no money-named column is a DecimalField or FloatField (bar the
   pricing configuration, listed by name), every `_minor` column is a
   BigIntegerField, and every model holding one has a `currency`.
4. The ledger modules contain no `float(` at all.

What this caught on its first run: `wallet_payment` in rider/views.py and
base/utils.py computed `float(wallet.balance) - float(amount)` and wrote the
result to the rider's wallet.
"""

import ast
from decimal import Decimal
from pathlib import Path

import pytest
from django.apps import apps
from django.db import models

from base import money
from base.money import MinorUnitsAmount, MoneyTypeError

ROOT = Path(__file__).resolve().parent.parent

# Pricing CONFIGURATION: rates and the inputs a quote multiplies. Decimal by
# decision -- a stored amount is produced from them by the money helper.
DECIMAL_BY_DESIGN = {
    ('pricing', 'RateCard'): {'base_fare', 'per_km_fare', 'per_min_fare', 'min_fare'},
    ('ride', 'VehicleFarePricing'): {'base_fare', 'per_km_fare', 'per_min_fare', 'min_fare'},
    ('pricing', 'ServiceZone'): {'per_trip_fee'},
}

MONEY_WORDS = ('fare', 'amount', 'balance', 'fee', 'net', 'payable', 'commission_amount')

# Modules where money is computed or moved. None of them has any business
# calling float().
LEDGER_MODULES = [
    'base/money_migration.py',
    'servers/driver/utils.py',
    'servers/ride/promos.py',
    'servers/ride/receipts.py',
    'servers/rider/credits.py',
    'servers/payments/tasks.py',
]

OUR_APPS = {'ride', 'payments', 'driver', 'rider', 'pricing'}


def _money_properties():
    for model in apps.get_models():
        if model._meta.app_label not in OUR_APPS:
            continue
        for name in dir(model):
            attr = getattr(model, name, None)
            if isinstance(attr, MinorUnitsAmount):
                yield model, name, attr


# --- 1. the helper --------------------------------------------------------

@pytest.mark.parametrize('bad', [135.68, 0.1 + 0.2, True, float('nan')])
def test_the_helper_refuses_a_float_or_bool(bad):
    with pytest.raises(MoneyTypeError):
        money.to_minor(bad, 'INR')


@pytest.mark.parametrize('bad', [Decimal('NaN'), Decimal('Infinity'), 'abc', [1]])
def test_the_helper_refuses_what_is_not_a_finite_number(bad):
    with pytest.raises(MoneyTypeError):
        money.to_minor(bad, 'INR')


def test_from_minor_refuses_anything_but_an_int():
    for bad in (135.68, Decimal('13568'), '13568', True):
        with pytest.raises(MoneyTypeError):
            money.from_minor(bad, 'INR')


def test_a_float_is_accepted_only_where_a_request_is_parsed():
    # JSON numbers arrive as floats; their text is what the client sent.
    assert money.parse_input(45.5) == Decimal('45.5')
    with pytest.raises(MoneyTypeError):
        money.as_decimal(45.5)


def test_rounding_is_half_up_and_happens_once():
    assert money.to_minor(Decimal('60.125'), 'INR') == 6013   # was 60.12 under round()
    assert money.to_minor(Decimal('60.135'), 'INR') == 6014
    assert money.to_minor(Decimal('-1.005'), 'INR') == -101   # half away from zero
    assert money.to_minor(Decimal('135.68'), 'INR') == 13568  # exact stays exact
    assert money.from_minor(13568, 'INR') == Decimal('135.68')
    assert str(money.from_minor(12300, 'USD')) == '123.00'


def test_digits_come_from_the_currency_table(monkeypatch):
    monkeypatch.setitem(money.CURRENCIES, 'JPY', money.Currency('JPY', 0, '¥'))
    monkeypatch.setitem(money.CURRENCIES, 'KWD', money.Currency('KWD', 3, 'KD'))
    assert money.to_minor(Decimal('500'), 'JPY') == 500
    assert money.to_minor(Decimal('1.2345'), 'KWD') == 1235
    assert str(money.from_minor(1235, 'KWD')) == '1.235'
    with pytest.raises(ValueError, match='unsupported currency'):
        money.to_minor(Decimal('1'), 'XYZ')


def test_a_currency_change_that_would_rescale_stored_amounts_is_refused(monkeypatch):
    from servers.rider.models import Wallet

    monkeypatch.setitem(money.CURRENCIES, 'JPY', money.Currency('JPY', 0, '¥'))
    w = Wallet(currency='INR', balance=Decimal('135.68'))
    with pytest.raises(ValueError, match='Set the currency before'):
        money.assert_currency_change_keeps_scale(w, 'INR', 'JPY')
    money.assert_currency_change_keeps_scale(w, 'INR', 'USD')  # same scale: fine


def test_sum_by_currency_refuses_a_major_unit_column():
    from servers.ride.models import Trip

    with pytest.raises(ValueError, match='minor units'):
        money.sum_by_currency(Trip.objects.none(), 'estimated_fare')


# --- 2. every money attribute on every model ------------------------------

def test_there_are_money_attributes_to_check():
    found = {(m.__name__, n) for m, n, _ in _money_properties()}
    assert ('Trip', 'estimated_fare') in found
    assert ('Wallet', 'balance') in found
    assert len(found) >= 25


@pytest.mark.django_db  # Trip's default status is a database lookup
@pytest.mark.parametrize('model,name,prop', list(_money_properties()),
                         ids=lambda v: getattr(v, '__name__', v) if not isinstance(v, MinorUnitsAmount) else '')
def test_every_money_attribute_refuses_a_float(model, name, prop):
    instance = model(currency='INR') if hasattr(model, 'currency') else model()
    with pytest.raises(MoneyTypeError):
        setattr(instance, name, 12.5)
    setattr(instance, name, Decimal('12.50'))
    assert getattr(instance, prop.minor_field) == 1250
    assert getattr(instance, name) == Decimal('12.50')


def test_a_flat_promo_amount_refuses_a_float_too():
    from servers.ride.models import PromoCode

    promo = PromoCode(discount_type='flat', currency='INR')
    with pytest.raises(MoneyTypeError):
        promo.discount_value = 25.5
    promo.discount_value = Decimal('25.50')
    assert promo.discount_flat_minor == 2550 and promo.discount_percent is None


# --- 3. the schema --------------------------------------------------------

def test_no_money_column_is_decimal_or_float():
    offenders = []
    for model in apps.get_models():
        label = (model._meta.app_label, model.__name__)
        if label[0] not in OUR_APPS:
            continue
        allowed = DECIMAL_BY_DESIGN.get(label, set())
        for field in model._meta.concrete_fields:
            if not isinstance(field, (models.DecimalField, models.FloatField)):
                continue
            if field.name in allowed or 'percent' in field.name or 'multiplier' in field.name:
                continue
            if any(word in field.name for word in MONEY_WORDS):
                offenders.append(f'{label[1]}.{field.name}')
    assert offenders == []


def test_every_minor_column_is_a_big_integer_beside_a_currency():
    seen = 0
    for model in apps.get_models():
        minor = [f for f in model._meta.concrete_fields if f.name.endswith('_minor')]
        if not minor:
            continue
        names = {f.name for f in model._meta.concrete_fields}
        assert 'currency' in names, f'{model.__name__} holds money with no currency'
        for f in minor:
            assert isinstance(f, models.BigIntegerField), f'{model.__name__}.{f.name}'
            seen += 1
    assert seen >= 25


# --- 4. the ledger modules ------------------------------------------------

@pytest.mark.parametrize('relpath', LEDGER_MODULES)
def test_no_float_call_in_a_ledger_module(relpath):
    tree = ast.parse((ROOT / relpath).read_text(encoding='utf-8'))
    calls = [node.lineno for node in ast.walk(tree)
             if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == 'float']
    assert calls == [], f'{relpath}: float() at lines {calls}'
