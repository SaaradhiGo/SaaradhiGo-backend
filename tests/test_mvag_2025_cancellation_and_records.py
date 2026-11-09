"""MVAG 2025 (owner brief, 2026-11-09): the cancellation penalty and the driver records gate.

Fixed example trips: a 437.00 INR fare (10% = 43.70) and a 1,500.00 INR fare (10% = 150.00, held
to the 100.00 cap). India's values come from the seed (pricing 0020): 10%, 100.00, 5 minutes.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from finance_world import client_for, make_driver, make_rider, make_trip, settle
from servers.admin_audit.models import AdminAuditLog
from servers.driver import records
from servers.driver.models import Driver
from servers.driver.tasks import block_expired_driver_licenses
from servers.payments import ledger
from servers.platform_ops.models import FeatureFlag, FeatureFlagOverride
from servers.pricing.regulation_models import MarketRegulation
from servers.promotions.rider_promos import payable_minor
from servers.ride import cancellation_penalty as cp
from servers.ride.models import CancellationPenalty, TripStatus
from servers.rider.models import Wallet, WalletTransaction
from test_pricing_module import geo, make_rule, world  # noqa: F401

pytestmark = [pytest.mark.django_db, pytest.mark.no_default_commission]


@pytest.fixture
def market(world):
    world.hyd.state_code = 'TG'
    world.hyd.save(update_fields=['state_code'])
    flag = FeatureFlag.objects.get(key=cp.FLAG)
    FeatureFlagOverride.objects.get_or_create(flag=flag, node=world.geo['IN'],
                                              defaults={'enabled': True})
    make_rule('IN', '15.00', node=world.geo['IN'])
    world.driver = make_driver(world.geo, 'IN-TG-HYD', 'c', currency='INR', calling='91')
    world.rider = make_rider('c', '91')
    return world


def cancelled(world, *, by, fare='437', after_min=6, reason='other', reached=False):
    trip = make_trip(world.hyd, world.rider, world.driver, fare=fare, completed=False)
    now = timezone.now()
    trip.accepted_at = now - timedelta(minutes=after_min)
    trip.reached_at = now - timedelta(minutes=1) if reached else None
    trip.status_id, _ = TripStatus.objects.get_or_create(status_code='cancelled')
    trip.cancelled_at, trip.cancelled_by, trip.cancellation_reason = now, by, reason
    trip.save()
    return trip, cp.assess(trip, now=now)


def balance(driver):
    w = Wallet.objects.filter(user_id=driver.user_id, scope=Wallet.SCOPE_DRIVER).first()
    return w.balance_minor if w else 0


class TestTheRule:
    def test_rider_after_the_window_owes_ten_percent(self, market):
        trip, row = cancelled(market, by='rider')
        assert (row.party, row.status, row.amount_minor) == ('rider', 'due', 4370)
        trip.refresh_from_db()
        assert trip.cancellation_fee_minor == 4370
        assert balance(market.driver) == 0          # nothing moves until the next trip settles

    def test_the_cap_holds_both_sides(self, market):
        _, rider_row = cancelled(market, by='rider', fare='1500')
        _, driver_row = cancelled(market, by='driver', fare='1500')
        assert rider_row.amount_minor == driver_row.amount_minor == 10000

    def test_driver_pays_a_ledger_debit_at_once(self, market):
        trip, row = cancelled(market, by='driver', reason='personal')
        assert (row.party, row.status, row.amount_minor) == ('driver', 'charged', 4370)
        assert balance(market.driver) == -4370
        txn = WalletTransaction.objects.get(user_id=market.driver.user_id,
                                            purpose='cancellation_penalty')
        assert txn.txn_type == 'debit' and ledger.ENTRY_LABELS[txn.purpose].startswith(
            'Cancellation fee')
        assert cp.assess(trip) == row and balance(market.driver) == -4370   # idempotent

    @pytest.mark.parametrize('by,reason,reached,after_min', [
        ('rider', 'other', False, 4),        # inside the 5-minute free window
        ('driver', 'personal', False, 4),
        ('rider', 'safety', True, 10),       # safety, either side
        ('driver', 'safety', True, 10),
        ('rider', 'no_show', False, 10),     # the driver never reached: driver no-show
        ('driver', 'no_show', True, 10),     # the rider never came: rider no-show
    ])
    def test_exemptions(self, market, by, reason, reached, after_min):
        _, row = cancelled(market, by=by, reason=reason, reached=reached, after_min=after_min)
        assert row is None and not CancellationPenalty.objects.exists()
        assert balance(market.driver) == 0

    def test_a_rider_claiming_no_show_after_the_driver_reached_still_pays(self, market):
        _, row = cancelled(market, by='rider', reason='no_show', reached=True)
        assert row is not None and row.amount_minor == 4370

    def test_nothing_when_the_flag_is_off(self, market):
        FeatureFlagOverride.objects.filter(flag__key=cp.FLAG).update(enabled=False)
        _, row = cancelled(market, by='rider')
        assert row is None

    def test_nothing_where_no_penalty_is_configured(self, market):
        MarketRegulation.objects.filter(country='IN').update(cancel_penalty_percent=None)
        _, row = cancelled(market, by='rider')
        assert row is None

    def test_a_state_value_overrides_the_country(self, market):
        MarketRegulation.objects.filter(country='IN', state_code='TG').update(
            cancel_penalty_percent=Decimal('5.00'))
        _, row = cancelled(market, by='rider')
        assert row.amount_minor == 2185


class TestTheNextBooking:
    def test_shown_before_booking_carried_and_paid_to_the_driver_let_down(self, market):
        old, row = cancelled(market, by='rider')
        due = cp.due_for_rider(market.rider)
        assert due['amount_minor'] == 4370 and due['trip_ids'] == [old.id]
        other = make_driver(market.geo, 'IN-TG-HYD', 'n', currency='INR', calling='91')
        nxt = make_trip(market.hyd, market.rider, other, fare='200', method='online')
        assert cp.attach_to_trip(nxt) == 4370
        assert payable_minor(nxt) == 20000 + 4370
        assert cp.due_for_rider(market.rider) is None
        settle(nxt)
        row.refresh_from_db()
        assert row.status == 'collected' and row.collected_on_trip_id == nxt.id
        comp = WalletTransaction.objects.get(purpose='cancellation_compensation')
        assert comp.user_id_id == market.driver.user_id_id and comp.amount_minor == 4370
        assert balance(market.driver) == 4370
        # The statement counts it and names it.
        start = timezone.now() - timedelta(days=1)
        stmt = ledger.build_statement(market.driver, start, timezone.now() + timedelta(minutes=1))
        assert stmt['credits_minor'] == 4370
        assert ledger.ENTRY_LABELS['cancellation_compensation'] == (
            'Cancellation fee paid by the rider (to you)')
        settle(nxt)                                   # a second settlement pays nothing twice
        assert WalletTransaction.objects.filter(purpose='cancellation_compensation').count() == 1

    def test_a_cash_driver_who_collects_it_owes_it_on(self, market):
        cancelled(market, by='rider')
        other = make_driver(market.geo, 'IN-TG-HYD', 'k', currency='INR', calling='91')
        nxt = make_trip(market.hyd, market.rider, other, fare='200', method='cash')
        cp.attach_to_trip(nxt)
        settle(nxt)
        held = WalletTransaction.objects.get(purpose='cancellation_fee_collected')
        assert held.user_id_id == other.user_id_id and held.amount_minor == 4370
        assert balance(market.driver) == 4370

    def test_a_due_carried_by_a_cancelled_trip_is_free_again(self, market):
        cancelled(market, by='rider')
        nxt = make_trip(market.hyd, market.rider, market.driver, fare='200', completed=False)
        cp.attach_to_trip(nxt)
        nxt.status_id, _ = TripStatus.objects.get_or_create(status_code='cancelled')
        nxt.save(update_fields=['status_id'])
        assert cp.due_for_rider(market.rider)['amount_minor'] == 4370

    def test_the_receipt_shows_the_line(self, market):
        from servers.ride.receipts import _due_rows_pdf

        old, _ = cancelled(market, by='rider')
        nxt = make_trip(market.hyd, market.rider, market.driver, fare='200')
        cp.attach_to_trip(nxt)
        rows = _due_rows_pdf(nxt, 'INR')
        assert rows[0][0] == f'Cancellation fee (trip {old.id})' and rows[0][1] == '+43.70'
        assert rows[-1] == ['Amount charged', '243.70']


class TestDriverRecordsGate:
    def valid(self, driver):
        future = date.today() + timedelta(days=200)
        for key, amount in (('health_insurance', 50_000_000), ('term_insurance', 100_000_000)):
            setattr(driver, f'{key}_policy_number', f'P-{key[:4]}-1')
            setattr(driver, f'{key}_insurer', 'Insurer Ltd')
            setattr(driver, f'{key}_sum_insured_minor', amount)
            setattr(driver, f'{key}_expiry', future)
        driver.induction_training_hours = Decimal('40.0')
        driver.induction_training_date = date.today() - timedelta(days=30)
        driver.save()

    def test_off_by_default_changes_nothing(self, market):
        assert records.status(market.driver)['gate_enabled'] is False
        assert records.blocking_problems(market.driver) == []
        block_expired_driver_licenses()
        market.driver.refresh_from_db()
        assert market.driver.status != 'blocked'

    def test_on_it_blocks_the_missing_and_the_expired_only(self, market):
        MarketRegulation.objects.filter(country='IN', state_code='').update(driver_records_gate=True)
        good = make_driver(market.geo, 'IN-TG-HYD', 'g', currency='INR', calling='91')
        self.valid(good)
        expired = make_driver(market.geo, 'IN-TG-HYD', 'x', currency='INR', calling='91')
        self.valid(expired)
        expired.health_insurance_expiry = date.today() - timedelta(days=1)
        expired.save(update_fields=['health_insurance_expiry'])
        short = make_driver(market.geo, 'IN-TG-HYD', 's', currency='INR', calling='91')
        self.valid(short)
        short.induction_training_hours = Decimal('20.0')
        short.save(update_fields=['induction_training_hours'])
        assert records.blocking_problems(expired) == ['Health insurance']
        result = block_expired_driver_licenses()
        assert set(result['driver_ids']) >= {market.driver.id, expired.id, short.id}
        assert good.id not in result['driver_ids']
        assert Driver.objects.get(pk=good.pk).status != 'blocked'
        assert Driver.objects.get(pk=expired.pk).status == 'blocked'

    def test_operations_record_them_in_the_console(self, market):
        _, user = market.super
        client = client_for(user)
        url = f'/api/v1/driver/admin/{market.driver.id}/records/'
        r = client.patch(url, {
            'health_insurance_policy_number': 'H-123', 'health_insurance_insurer': 'Insurer Ltd',
            'health_insurance_sum_insured_minor': 50_000_000,
            'health_insurance_expiry': '2027-06-30', 'induction_training_hours': '40',
            'induction_training_date': '2026-10-01', 'ais140_device_id': 'AIS-77',
            'ais140_fitted': True, 'panic_button_fitted': False, 'reason': 'Papers checked'},
            format='json')
        assert r.status_code == 200, r.content[:300]
        body = r.json()['data']
        assert body['vehicle']['ais140_fitted'] is True
        items = {i['key']: i for i in body['status']['items']}
        assert items['health_insurance']['ok'] and items['induction_training']['ok']
        assert not items['term_insurance']['ok']
        assert AdminAuditLog.objects.filter(action='driver_records_updated').exists()
        assert client.patch(url, {'health_insurance_expiry': '30/06/2027'},
                            format='json').status_code == 400
