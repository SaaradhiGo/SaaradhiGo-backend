"""India money in whole rupees end to end (owner ruling, 23 Nov):

    "driver 59 shows '₹8,112.29' earned and 'Avg ₹176.35'. Find where paise come from (fares,
    adjustments, averages), fix the source and the display, and correct stored QA data. Averages
    display rounded to whole rupees."

Where the paise came from, and what each test pins:
  * fare lines and inclusive GST in `quote_fare` were rounded to the paisa beside a whole-rupee total;
  * the settlement commission (`settlement_amounts`, `operator_end`) was rounded to the paisa, so the
    driver's net (fare - commission) carried paise into every earnings figure;
  * the daily platform fee's legal ceiling was floored to the paisa;
  * the cancellation penalty was a percent rounded to the paisa;
  * the earnings average was quantized to the paisa;
  * and stored QA rows written before all this: `round_inr_money_to_rupees`.
USD keeps cents everywhere.
"""
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from base.money import round_minor, round_money
from finance_world import make_driver, make_rider, make_trip, settle
from servers.admin_audit.models import AdminAuditLog
from servers.driver import earnings_history as eh
from servers.driver.earnings import EarningRow
from servers.payments.models import TransactionHistory, TripSettlement
from servers.pricing import fee_plans
from servers.pricing.fee_plan_models import DriverDailyFee, DriverFeePlan
from servers.ride.cancellation_penalty import amount_for
from servers.ride.models import Trip
from servers.rider.models import Wallet, WalletTransaction
from test_fare_decomposition_sums import _fare
from test_pricing_module import geo, make_rule, world  # noqa: F401

pytestmark = [pytest.mark.django_db, pytest.mark.no_default_commission]

User = get_user_model()
ACTOR = 'ops.rupees@saaradhigo.test'


def _whole(value):
    d = Decimal(str(value))
    return d == d.to_integral_value()


@pytest.fixture
def india(world):  # noqa: F811
    DriverFeePlan.objects.all().delete()
    make_rule('IN', '15.00', node=world.geo['IN'])
    world.driver = make_driver(world.geo, 'IN-TG-HYD', 'r', currency='INR', calling='91')
    world.rider = make_rider('r', '91')
    return world


@pytest.fixture
def actor():
    return User.objects.create_user(phone_number='+919600000771', username='+919600000771', email=ACTOR,
                                    full_name='Ops Rupees', role='admin', is_staff=True)


def _balance(driver):
    return Wallet.objects.get(user_id=driver.user_id, scope=Wallet.SCOPE_DRIVER).balance_minor


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------

def test_round_minor_is_whole_rupees_half_up_and_leaves_cents_alone():
    assert round_minor(17635, 'INR') == 17600
    assert round_minor(17650, 'INR') == 17700
    assert round_minor(2250, 'INR', ROUND_FLOOR) == 2200
    assert round_minor(17635, 'USD') == 17635
    assert round_money(Decimal('176.35'), 'INR') == Decimal('176.00')
    assert round_money(Decimal('176.35'), 'USD') == Decimal('176.35')


# ---------------------------------------------------------------------------
# Fare parts
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('km,minutes', [('2.4', '8'), ('3.33', '11'), ('7.77', '23'), ('12.5', '40'),
                                        ('33.333', '95')])
def test_every_inr_fare_line_and_the_gst_are_whole_rupees_and_add_up(km, minutes):
    fare = _fare(km, minutes)
    assert fare['currency'] == 'INR'
    lines = [fare['base_fare'], fare['distance_fare'], fare['time_fare']]
    lines += [a['amount'] for a in fare.get('fare_adjustments', [])]
    assert all(_whole(v) for v in lines), lines
    assert _whole(fare['tax_amount']) and _whole(fare['total_fare'])
    assert sum(Decimal(str(v)) for v in lines) == Decimal(str(fare['total_fare']))


def test_the_invoice_lines_of_a_whole_rupee_fare_are_whole_and_add_up():
    from servers.ride import invoice_lines

    inv = invoice_lines.build(
        components=[('base', 'Base fare', '60'), ('distance_time', 'Distance and time', '123')],
        total='183', tax_amount='9', tax_mode='inclusive', tax_percent='5.000', tax_label='GST',
        currency='INR')
    amounts = [Decimal(x['amount']) for x in inv['lines']]
    assert all(_whole(a) for a in amounts) and sum(amounts) == Decimal(inv['taxable']) == Decimal('174')


# ---------------------------------------------------------------------------
# Driver earnings
# ---------------------------------------------------------------------------

def test_the_commission_and_the_drivers_net_are_whole_rupees(india):
    trip = make_trip(india.hyd, india.rider, india.driver, fare='137')     # 15% = 20.55
    settle(trip)
    s = TripSettlement.objects.get(trip=trip)
    assert (s.gross_fare_minor, s.commission_amount_minor, s.driver_net_minor) == (13700, 2100, 11600)
    earning = WalletTransaction.objects.get(user_id=india.driver.user_id, purpose='trip_earnings')
    assert earning.amount_minor == 11600 and _balance(india.driver) == 11600


def test_a_cash_trip_owes_a_whole_rupee_commission(india):
    trip = make_trip(india.hyd, india.rider, india.driver, fare='137', method='cash')
    settle(trip)
    debit = WalletTransaction.objects.get(user_id=india.driver.user_id, purpose='trip_commission')
    assert debit.amount_minor == 2100 and _balance(india.driver) == -2100


def test_the_daily_fee_is_whole_rupees_under_the_legal_ceiling():
    plan = DriverFeePlan(country='IN', plan_type='fair_share', currency='INR', version=1,
                         amounts={'sedan': {'per_trip_fee_minor': 1050}}, earnings_floor_minor=0)
    # Rs 33.33 at the 20% ceiling is Rs 6.66: floored to Rs 6, never rounded up past the cap.
    got = fee_plans.day_fee(plan, 'sedan', [3333, 20000])
    assert got.per_trip_minor == [600, 1100] and got.fee_minor == 1700
    usd = DriverFeePlan(country='US', plan_type='fair_share', currency='USD', version=1,
                        amounts={'sedan': {'per_trip_fee_minor': 1050}}, earnings_floor_minor=0)
    assert fee_plans.day_fee(usd, 'sedan', [3333]).per_trip_minor == [666]


def test_the_cancellation_penalty_is_whole_rupees():
    from servers.pricing.regulation import CancelPolicy

    policy = CancelPolicy(percent=Decimal('10'), cap_minor=10050, free_seconds=300)
    assert amount_for(policy, 43700, 'INR') == 4400
    assert amount_for(policy, 150000, 'INR') == 10000                     # the cap, floored
    assert amount_for(policy, 43700, 'USD') == 4370


def _row(gross, net, i):
    return EarningRow(ledger_id=i, trip_id=i, gross=Decimal(gross), commission=Decimal(gross) - Decimal(net),
                      net=Decimal(net), method='online', created_at=None, rider_name='', driver_holds_cash=False)


def test_the_average_is_worked_out_then_rounded_to_whole_rupees():
    rows = [_row('176', '150', 1), _row('176', '150', 2), _row('177', '151', 3)]   # 176.33...
    assert eh.summarise_period(rows, [], [], None, 'INR')['avg_fare'] == Decimal('176.00')
    assert eh.summarise_period(rows, [], [], None, 'USD')['avg_fare'] == Decimal('176.33')


def test_the_drivers_month_is_whole_rupees(india):
    for fare in ('137', '211', '99'):
        settle(make_trip(india.hyd, india.rider, india.driver, fare=fare))
    summary = eh.history(india.driver, 'month')['summary']
    assert _whole(summary['earned']) and _whole(summary['avg_fare']) and _whole(summary['platform_fee'])
    assert Decimal(summary['avg_fare']) == Decimal('149')                  # 447 / 3


# ---------------------------------------------------------------------------
# The data fix
# ---------------------------------------------------------------------------

def _legacy_paise_trip(world):
    """A trip settled before 23 Nov: fare Rs 137.45, 15% commission 20.62, net 116.83."""
    trip = make_trip(world.hyd, world.rider, world.driver, fare='137.45')
    settle(trip)
    TripSettlement.objects.filter(trip=trip).update(
        gross_fare_minor=13745, commission_amount_minor=2062, driver_net_minor=11683)
    WalletTransaction.objects.filter(reference_id=f'TRIP_{trip.id}', purpose='trip_earnings').update(amount_minor=11683)
    TransactionHistory.objects.filter(trip_id=trip).update(amount_minor=11683)
    Wallet.objects.filter(user_id=world.driver.user_id, scope=Wallet.SCOPE_DRIVER).update(balance_minor=11683)
    return trip


def _legacy_paise_fee(driver):
    plan = DriverFeePlan.objects.create(country='IN', plan_type='fair_share', version=1, status='active',
                                        effective_from=date(2026, 1, 1), amounts={'sedan': {'per_trip_fee_minor': 1000}})
    txn = WalletTransaction.objects.create(user_id=driver.user_id, currency='INR', amount_minor=14925,
                                           txn_type='debit', status='completed', purpose='platform_fee',
                                           reference_id='DAY_20261120', idempotency_key=f'PF_{driver.id}')
    Wallet.objects.filter(user_id=driver.user_id, scope=Wallet.SCOPE_DRIVER).update(
        balance_minor=Wallet.objects.get(user_id=driver.user_id, scope=Wallet.SCOPE_DRIVER).balance_minor - 14925)
    return DriverDailyFee.objects.create(driver=driver, day=date(2026, 11, 20), plan=plan, plan_version=1,
                                         currency='INR', trips=12, fares_minor=180000, fee_minor=14925,
                                         reason='charged', explanation='x', wallet_transaction_id=txn.id)


def test_the_dry_run_changes_nothing(india):
    trip = _legacy_paise_trip(india)
    out = StringIO()
    call_command('round_inr_money_to_rupees', stdout=out)
    assert 'Dry run' in out.getvalue() and f'trip {trip.id}' in out.getvalue()
    trip.refresh_from_db()
    assert trip.final_fare_minor == 13745 and _balance(india.driver) == 11683


def test_the_fix_rounds_the_trip_and_its_settlement_so_they_add_up(india, actor):
    trip = _legacy_paise_trip(india)
    day = _legacy_paise_fee(india.driver)
    us_driver = make_driver(india.geo, 'US-TX-AUS', 'u', currency='USD', calling='1')
    us_trip = make_trip(india.aus, india.rider, us_driver, fare='12.34')
    call_command('round_inr_money_to_rupees', '--confirm', '--actor-email', ACTOR, stdout=StringIO())

    trip.refresh_from_db()
    assert (trip.estimated_fare_minor, trip.final_fare_minor) == (13700, 13700)
    s = TripSettlement.objects.get(trip=trip)
    assert (s.gross_fare_minor, s.commission_amount_minor, s.driver_net_minor) == (13700, 2100, 11600)
    assert s.gross_fare_minor == s.commission_amount_minor + s.driver_net_minor
    # The ledger is append-only: the original rows stand and one correction line per trip and day
    # takes them to whole rupees.
    earning = WalletTransaction.objects.get(reference_id=f'TRIP_{trip.id}', purpose='trip_earnings')
    assert earning.amount_minor == 11683
    fix = WalletTransaction.objects.get(reference_id=f'TRIP_{trip.id}', purpose='whole_rupee_correction')
    assert (fix.txn_type, fix.amount_minor) == ('debit', 83)
    day.refresh_from_db()
    assert day.fee_minor == 14900
    day_fix = WalletTransaction.objects.get(reference_id='DAY_20261120', purpose='whole_rupee_correction')
    assert (day_fix.txn_type, day_fix.amount_minor) == ('credit', 25)
    # The balance is still exactly the ledger: 116 earned less a Rs 149 fee.
    assert _balance(india.driver) == 11600 - 14900
    # And every screen reads whole rupees: the trip row, the month, the statement's movement.
    from servers.driver.earnings import settlement_rows

    (row,) = [r for r in settlement_rows(india.driver) if r.trip_id == trip.id]
    assert (row.gross, row.commission, row.net) == (Decimal('137.00'), Decimal('21.00'), Decimal('116.00'))
    month = eh.statement(india.driver, 2026, 11)['summary']
    assert Decimal(month['daily_fee']) == Decimal('149.00')
    # USD is never touched.
    us_trip.refresh_from_db()
    assert us_trip.final_fare_minor == 1234
    # Audited, by name.
    rows = AdminAuditLog.objects.filter(action__startswith='inr_')
    assert {r.action for r in rows} >= {'inr_trip_money_rounded', 'inr_platform_fee_rounded',
                                         'inr_money_rounded_to_rupees'}
    assert all(r.actor_label == f'{ACTOR} via round_inr_money_to_rupees' for r in rows)
    # And it is done: a second run finds nothing.
    out = StringIO()
    call_command('round_inr_money_to_rupees', stdout=out)
    assert '0 trip(s), 0 fee day(s)' in out.getvalue()
