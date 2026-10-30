"""Finance read side (module 7, part A): scope, currencies, ledger, exports, payouts.

Everything here runs through the real endpoints and the real settlement and
withdrawal code (see `finance_world`), so the figures asserted are the ones
production would produce.
"""

import csv
import io
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone

from conftest import seed_actor
from finance_world import (
    build_world, client_for, make_driver, make_payment, make_rider, make_trip, operator,
    seed_in_payout_config, settle, success_webhook, zone,
)
from servers.admin_access.masking import RAW_PII_PATTERNS
from servers.admin_access.models import AdminAssignment, AdminRole, GeoNode
from servers.admin_audit.models import AdminAuditLog
from servers.driver.models import WithdrawalRequest
from servers.driver.services import calculate_available_balance
from servers.payments import ledger
from servers.payments.models import (
    Payment, PayoutCountryConfig, TransactionHistory, TripSettlement,
)
from servers.payments.scope import money_visible_to
from servers.ride.models import TripMoneyAction
from servers.rider.models import Wallet, WalletTransaction

pytestmark = pytest.mark.django_db

User = get_user_model()
BASE = '/api/v1/payments/admin'


@pytest.fixture
def geo(db):
    from conftest import seed_admin_access

    seed_admin_access()
    return {n.code: n for n in GeoNode.objects.all()}


@pytest.fixture
def world(geo):
    return build_world(geo)


def data(response):
    assert response.status_code == 200, response.content[:400]
    return response.json()['data']


def ids(payload):
    return {row['id'] for row in payload['results']}


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

class TestScopePerRole:
    def test_finance_at_hyderabad_sees_hyderabad_money_only(self, world):
        client, _ = world.finance
        assert ids(data(client.get(f'{BASE}/payments/'))) == {world.in_payment.id}
        assert ids(data(client.get(f'{BASE}/payouts/'))) == {world.in_pending.id, world.in_rejected.id}
        settlement_trips = {r['trip_id'] for r in data(client.get(f'{BASE}/settlements/'))['results']}
        assert settlement_trips == {world.in_trip.id, world.in_cash.id}

    def test_an_austin_payout_is_a_404_for_hyderabad_finance(self, world):
        client, _ = world.finance
        assert client.get(f'{BASE}/payouts/{world.us_withdrawal.id}/').status_code == 404
        assert client.get(f'{BASE}/payments/{world.us_payment.id}/').status_code == 404
        assert client.get(f'{BASE}/drivers/{world.us_driver.id}/ledger/').status_code == 404

    def test_a_missing_row_and_an_out_of_scope_row_answer_identically(self, world):
        client, _ = world.finance
        missing = client.get(f'{BASE}/payouts/999999/')
        hidden = client.get(f'{BASE}/payouts/{world.us_withdrawal.id}/')
        assert missing.status_code == hidden.status_code == 404
        assert missing.json()['error']['message'] == hidden.json()['error']['message']

    def test_super_admin_reaches_both_markets(self, world, geo):
        client, _ = operator('super_admin', geo)
        assert ids(data(client.get(f'{BASE}/payments/'))) == {world.in_payment.id, world.us_payment.id}
        assert client.get(f'{BASE}/payouts/{world.us_withdrawal.id}/').status_code == 200
        assert client.get(f'{BASE}/payments/{world.us_payment.id}/').status_code == 200
        assert client.get(f'{BASE}/drivers/{world.us_driver.id}/ledger/').status_code == 200

    @pytest.mark.parametrize('role', ['marketing', 'analyst', 'kyc_verifier', 'support_agent',
                                      'safety_officer', 'city_ops_manager', 'support_lead'])
    @pytest.mark.parametrize('path', [
        'payments/', 'settlements/', 'payouts/', 'overview/', 'reconciliation/',
        'reconciliation/items/', 'payout-config/', 'transactions/',
    ])
    def test_roles_without_finance_read_are_refused(self, world, geo, role, path):
        client, _ = operator(role, geo)
        assert client.get(f'{BASE}/{path}').status_code == 403

    @pytest.mark.parametrize('role', ['marketing', 'analyst', 'kyc_verifier', 'support_agent', 'safety_officer'])
    def test_those_roles_cannot_open_a_detail_ledger_or_export_either(self, world, geo, role):
        client, _ = operator(role, geo)
        for path in (f'payments/{world.in_payment.id}/', f'payouts/{world.in_pending.id}/',
                     f'drivers/{world.in_driver.id}/ledger/', 'export/settlements/'):
            assert client.get(f'{BASE}/{path}').status_code == 403, path

    def test_no_role_template_but_finance_and_super_admin_holds_finance_read(self):
        from servers.admin_access.roles import ROLE_TEMPLATES

        holders = {t['code'] for t in ROLE_TEMPLATES if 'finance.read' in t['permissions']}
        assert holders == {'finance', 'super_admin'}
        exporters = {t['code'] for t in ROLE_TEMPLATES if 'finance.export' in t['permissions']}
        assert exporters == {'finance', 'super_admin'}

    def test_a_list_shows_only_where_finance_is_actually_held(self, world, geo):
        """Finance in Hyderabad plus Support in Austin: Austin's money is in the
        assignment scope but the capability is not held there, so the list does
        not show it and the detail is a 403, not a 404."""
        client, user = operator('finance', geo)
        AdminAssignment.objects.create(
            admin=user, role=AdminRole.objects.get(code='support_agent'), scope=geo['US-TX-AUS'])
        assert ids(data(client.get(f'{BASE}/payments/'))) == {world.in_payment.id}
        assert client.get(f'{BASE}/payments/{world.us_payment.id}/').status_code == 403

    def test_the_scope_parameter_narrows_never_widens(self, world, geo):
        client, _ = operator('finance', geo)
        assert client.get(f'{BASE}/payments/?scope=US-TX-AUS').status_code == 403
        sup, _ = operator('super_admin', geo)
        only_us = data(sup.get(f'{BASE}/payments/?scope=US-TX-AUS'))
        assert ids(only_us) == {world.us_payment.id}

    def test_the_old_transactions_list_follows_the_same_rule(self, world, geo):
        TransactionHistory.objects.create(
            trip_id=world.in_trip, user_id=world.in_rider, driver_id=world.in_driver,
            amount=Decimal('10.00'), method='online', status='completed')
        TransactionHistory.objects.create(
            trip_id=world.us_trip, user_id=world.us_rider, driver_id=world.us_driver,
            amount=Decimal('10.00'), method='online', status='completed')
        client, _ = world.finance
        rows = data(client.get(f'{BASE}/transactions/'))['results']
        assert {r['driver_id'] for r in rows} == {world.in_driver.id}

    def test_a_rider_wallet_row_follows_the_rider_rule_and_a_driver_row_the_driver(self, world, geo):
        for rider in (world.in_rider, world.us_rider):
            WalletTransaction.objects.create(
                user_id=rider, amount_minor=500, txn_type='credit', status='completed',
                purpose='refund', reference_id=f'R{rider.id}', idempotency_key=f'k-{rider.id}')
        hyd_user = User.objects.get(pk=world.finance[1].pk)
        rows = money_visible_to(hyd_user, WalletTransaction, permission='finance.read')
        owners = set(rows.values_list('user_id', flat=True))
        assert world.in_rider.id in owners and world.us_rider.id not in owners
        assert world.in_driver.user_id_id in owners        # driver ledger rows, Hyderabad driver
        assert world.us_driver.user_id_id not in owners

    def test_an_unplaceable_payment_is_for_the_unrestricted_only(self, world, geo):
        orphan_trip = make_trip(None, world.in_rider, world.in_driver, fare='10.00')
        orphan = make_payment(orphan_trip, 'order_ORPHAN_1')
        city, _ = world.finance
        assert orphan.id not in ids(data(city.get(f'{BASE}/payments/')))
        sup, _ = operator('super_admin', geo)
        assert orphan.id in ids(data(sup.get(f'{BASE}/payments/')))


# ---------------------------------------------------------------------------
# Currencies never add up
# ---------------------------------------------------------------------------

class TestCurrencyGrouping:
    def test_settlement_totals_are_one_entry_per_currency(self, world, geo):
        client, _ = operator('super_admin', geo)
        totals = data(client.get(f'{BASE}/settlements/'))['totals']
        by = {t['currency']: t for t in totals}
        assert set(by) == {'INR', 'USD'} and len(totals) == 2
        assert by['INR']['gross_minor'] == 210000 and by['INR']['count'] == 2
        assert by['USD']['gross_minor'] == 3000 and by['USD']['count'] == 1
        # the invariant: no figure anywhere is the cross-currency sum
        assert 213000 not in {t['gross_minor'] for t in totals}

    def test_payment_totals_are_per_currency(self, world, geo):
        client, _ = operator('super_admin', geo)
        totals = {t['currency']: t['amount_minor'] for t in data(client.get(f'{BASE}/payments/'))['totals']}
        assert totals == {'INR': 200000, 'USD': 3000}

    def test_the_overview_never_has_a_cross_currency_total(self, world, geo):
        client, _ = operator('super_admin', geo)
        today = timezone.now().date()
        payload = data(client.get(f'{BASE}/overview/?from={today - timedelta(days=1)}&to={today + timedelta(days=1)}'))
        totals = {t['currency']: t for t in payload['totals']}
        assert set(totals) == {'INR', 'USD'}
        assert totals['INR']['gross_minor'] == 210000 and totals['USD']['gross_minor'] == 3000
        for row in payload['rows']:
            assert row['currency'] in ('INR', 'USD')
        assert len(payload['rows']) == len({(r['city']['code'], r['day'], r['currency']) for r in payload['rows']})

    def test_the_overview_has_the_payout_liability_by_currency(self, world, geo):
        client, _ = operator('super_admin', geo)
        today = timezone.now().date()
        payload = data(client.get(f'{BASE}/overview/?from={today - timedelta(days=1)}&to={today + timedelta(days=1)}'))
        totals = {t['currency']: t for t in payload['totals']}
        assert totals['INR']['payouts_pending_minor'] == 50000
        assert totals['USD']['payouts_pending_minor'] == 2000


class TestTheCitysOwnDay:
    def test_a_late_evening_austin_settlement_is_on_the_austin_day(self, world, geo):
        # 03:30 UTC on the 5th is 21:30 on the 4th in Chicago (CST, UTC-6).
        moment = datetime(2026, 3, 5, 3, 30, tzinfo=dt_timezone.utc)
        TripSettlement.objects.filter(trip=world.us_trip).update(settled_at=moment)
        client, _ = operator('super_admin', geo)
        on_4th = data(client.get(f'{BASE}/settlements/?from=2026-03-04&to=2026-03-04&scope=US-TX-AUS'))
        on_5th = data(client.get(f'{BASE}/settlements/?from=2026-03-05&to=2026-03-05&scope=US-TX-AUS'))
        assert [r['trip_id'] for r in on_4th['results']] == [world.us_trip.id]
        assert on_5th['results'] == []

    def test_a_late_evening_hyderabad_settlement_is_on_the_next_hyderabad_day(self, world, geo):
        # 20:00 UTC on the 4th is 01:30 on the 5th in Hyderabad (IST, UTC+5:30).
        moment = datetime(2026, 3, 4, 20, 0, tzinfo=dt_timezone.utc)
        TripSettlement.objects.filter(trip=world.in_trip).update(settled_at=moment)
        client, _ = operator('super_admin', geo)
        on_5th = data(client.get(f'{BASE}/settlements/?from=2026-03-05&to=2026-03-05&scope=IN-TG-HYD'))
        assert world.in_trip.id in {r['trip_id'] for r in on_5th['results']}
        on_4th = data(client.get(f'{BASE}/settlements/?from=2026-03-04&to=2026-03-04&scope=IN-TG-HYD'))
        assert world.in_trip.id not in {r['trip_id'] for r in on_4th['results']}

    def test_the_overview_groups_by_the_city_day(self, world, geo):
        moment = datetime(2026, 3, 5, 3, 30, tzinfo=dt_timezone.utc)
        TripSettlement.objects.filter(trip=world.us_trip).update(settled_at=moment)
        client, _ = operator('super_admin', geo)
        rows = data(client.get(f'{BASE}/overview/?from=2026-03-01&to=2026-03-10&currency=USD'))['rows']
        gross_rows = [r for r in rows if r['gross_minor']]
        assert [str(r['day']) for r in gross_rows] == ['2026-03-04']

    def test_a_bad_date_and_an_oversized_range_are_400(self, world):
        client, _ = world.finance
        assert client.get(f'{BASE}/settlements/?from=nope').status_code == 400
        assert client.get(f'{BASE}/overview/?from=2026-01-01&to=2026-12-31').status_code == 400
        assert client.get(f'{BASE}/overview/?from=2026-03-05&to=2026-03-01').status_code == 400


# ---------------------------------------------------------------------------
# The driver ledger
# ---------------------------------------------------------------------------

class TestLedger:
    def test_closing_balance_equals_the_available_balance(self, world):
        client, _ = world.finance
        body = data(client.get(f'{BASE}/drivers/{world.in_driver.id}/ledger/'))
        balance = calculate_available_balance(world.in_driver)
        assert Decimal(body['closing_minor']) == balance * 100
        assert body['closing_minor'] == body['balance_minor']
        assert body['unexplained_minor'] == 0
        assert body['currency'] == 'INR'

    def test_the_statement_is_opening_plus_movements(self, world):
        client, _ = world.finance
        body = data(client.get(f'{BASE}/drivers/{world.in_driver.id}/ledger/'))
        assert body['opening_minor'] + body['credits_minor'] - body['debits_minor'] == body['closing_minor']
        kinds = [e['kind'] for e in body['entries']['results']]
        assert kinds.count('settlement') == 2
        assert kinds.count('withdrawal_requested') == 2
        assert kinds.count('withdrawal_rejected') == 1
        # running balance ends where the statement closes
        assert body['entries']['results'][-1]['running_balance_minor'] == body['closing_minor']

    def test_a_range_in_the_future_has_an_opening_equal_to_the_balance(self, world):
        client, _ = world.finance
        later = (timezone.now() + timedelta(days=3)).date()
        body = data(client.get(f'{BASE}/drivers/{world.in_driver.id}/ledger/?from={later}&to={later}'))
        assert body['opening_minor'] == body['balance_minor'] == body['closing_minor']
        assert body['entries']['results'] == []

    def test_a_range_splits_the_history_without_losing_a_paisa(self, world):
        client, _ = world.finance
        txn = WalletTransaction.objects.filter(
            user_id=world.in_driver.user_id, purpose='trip_earnings').first()
        WalletTransaction.objects.filter(pk=txn.pk).update(created_at=timezone.now() - timedelta(days=10))
        today = timezone.now().date()
        body = data(client.get(f'{BASE}/drivers/{world.in_driver.id}/ledger/?from={today - timedelta(days=1)}&to={today + timedelta(days=1)}'))
        assert body['opening_minor'] == txn.amount_minor
        assert body['closing_minor'] == body['balance_minor']
        assert body['unexplained_minor'] == 0

    def test_a_wallet_changed_without_a_ledger_row_is_reported_not_hidden(self, world):
        Wallet.objects.filter(user_id=world.in_driver.user_id, scope=Wallet.SCOPE_DRIVER).update(
            balance_minor=Wallet.objects.get(
                user_id=world.in_driver.user_id, scope=Wallet.SCOPE_DRIVER).balance_minor + 777)
        client, _ = world.finance
        body = data(client.get(f'{BASE}/drivers/{world.in_driver.id}/ledger/'))
        assert body['unexplained_minor'] == 777
        assert body['balance_minor'] - body['closing_minor'] == 777

    def test_the_balance_function_is_untouched_and_agrees_on_the_fixture(self, world):
        wallet = Wallet.objects.get(user_id=world.in_driver.user_id, scope=Wallet.SCOPE_DRIVER)
        assert calculate_available_balance(world.in_driver) == wallet.balance
        stmt = ledger.build_statement(
            world.in_driver, timezone.now() - timedelta(days=1), timezone.now() + timedelta(days=1))
        assert stmt['closing_minor'] == wallet.balance_minor

    def test_a_driver_with_no_wallet_is_an_empty_statement_not_an_error(self, world, geo):
        d = make_driver(geo, 'IN-TG-HYD', 'NW', currency='INR', calling='91')
        Wallet.objects.filter(user_id=d.user_id).delete()
        client, _ = world.finance
        body = data(client.get(f'{BASE}/drivers/{d.id}/ledger/'))
        assert body['has_wallet'] is False and body['entries']['results'] == []

    def test_the_paid_event_is_a_memo_that_moves_no_balance(self, world):
        TransactionHistory.objects.create(
            user_id=world.in_driver.user_id, driver_id=world.in_driver, amount_minor=50000,
            method='upi', status='completed', txn_type='credit', currency='INR',
            withdrawal_request=world.in_pending, gateway_transaction_id='gw1')
        client, _ = world.finance
        body = data(client.get(f'{BASE}/drivers/{world.in_driver.id}/ledger/'))
        assert any(m['kind'] == 'withdrawal_paid' and m['affects_balance'] is False for m in body['memo'])
        assert body['closing_minor'] == body['balance_minor']


# ---------------------------------------------------------------------------
# Rows, masking, details
# ---------------------------------------------------------------------------

class TestMaskingAndDetail:
    def test_gateway_ids_show_only_their_tail_and_nothing_raw_is_returned(self, world):
        client, _ = world.finance
        body = client.get(f'{BASE}/payments/').content.decode()
        assert 'order_IN_0001' not in body and 'cfpayorder_IN_0001' not in body
        assert '0001' in body                       # the masked tail
        assert 'gateway_metadata' not in body and 'gateway_signature' not in body
        assert '+919876543210' not in body and 'x.y@example.test' not in body

    def test_the_payment_detail_has_chain_refunds_and_a_masked_webhook_summary(self, world):
        nxt = make_payment(world.in_trip, 'order_IN_0002', status='failed')
        Payment.objects.filter(pk=world.in_payment.pk).update(replaced_by=nxt, switch_reason='user_requested')
        TripMoneyAction.objects.create(
            trip=world.in_trip, kind='refund', amount_minor=2500, currency='INR',
            method='wallet_credit', status='completed', reason='late pickup')
        client, _ = world.finance
        body = data(client.get(f'{BASE}/payments/{world.in_payment.id}/'))
        assert [c['id'] for c in body['chain']] == [world.in_payment.id, nxt.id]
        assert body['refunds'][0]['amount_minor'] == 2500
        assert body['webhooks'][0]['event_type'] == 'PAYMENT_SUCCESS_WEBHOOK'
        assert body['webhooks'][0]['result'] == 'processed'
        raw = client.get(f'{BASE}/payments/{world.in_payment.id}/').content.decode()
        assert 'card_number' not in raw and 'xxxx1111' not in raw and 'raw_payload' not in raw

    def test_no_raw_personal_pattern_in_any_finance_body(self, world, geo):
        client, _ = operator('super_admin', geo)
        for path in ('payments/', 'settlements/', 'payouts/', 'overview/', 'reconciliation/',
                     f'payments/{world.in_payment.id}/', f'payouts/{world.in_pending.id}/',
                     f'drivers/{world.in_driver.id}/ledger/', 'payout-config/'):
            body = client.get(f'{BASE}/{path}').content.decode()
            for name, rx in RAW_PII_PATTERNS.items():
                assert not rx.search(body), (path, name, rx.findall(body)[:2])

    def test_a_payout_names_its_rail_masked_and_links_the_approval(self, world, geo):
        from servers.approvals.models import ApprovalRequest

        maker = world.finance[1]
        approval = ApprovalRequest(
            action_key='finance.payout', country='IN', scope_node=geo['IN-TG-HYD'], amount=50000,
            currency='INR', target_type='withdrawal_request', target_id=str(world.in_pending.id),
            payload={}, request_reason='over the limit',
            expires_at=timezone.now() + timedelta(days=1), idempotency_key='fin-test-1')
        approval.set_maker(maker)
        approval.save()
        client, _ = world.finance
        rows = {r['id']: r for r in data(client.get(f'{BASE}/payouts/'))['results']}
        row = rows[world.in_pending.id]
        assert row['approval']['id'] == approval.id and row['approval']['status'] == 'requested'
        assert row['approval']['maker']['label']
        assert 'driverIN' not in str(row) and row['upi_id'].endswith('@okaxis')
        assert row['age_seconds'] is not None and rows[world.in_rejected.id]['age_seconds'] is None

    def test_the_payout_tabs_count_every_status_per_currency(self, world, geo):
        client, _ = operator('super_admin', geo)
        tabs = {t['status']: t for t in data(client.get(f'{BASE}/payouts/'))['tabs']}
        assert tabs['pending']['count'] == 2           # IN pending + US pending
        assert {a['currency'] for a in tabs['pending']['amounts']} == {'INR', 'USD'}
        assert tabs['rejected']['count'] == 1
        assert set(tabs) == {'pending', 'approved', 'processed', 'completed', 'failed',
                             'unresolved', 'rejected'}
        only_pending = data(client.get(f'{BASE}/payouts/?status=pending'))
        assert all(r['status'] == 'pending' for r in only_pending['results'])
        assert tabs['rejected']['count'] == 1          # tabs ignore the status filter

    def test_a_failed_payout_shows_its_count_and_reason(self, world):
        WithdrawalRequest.objects.filter(pk=world.in_pending.pk).update(
            status='failed', failure_count=2, failure_reason='Beneficiary bank offline')
        client, _ = world.finance
        row = data(client.get(f'{BASE}/payouts/{world.in_pending.id}/'))
        assert row['failure_count'] == 2 and row['failure_reason'] == 'Beneficiary bank offline'
        assert row['status_label'] == 'Failed'

    def test_lists_are_paginated(self, world):
        client, _ = world.finance
        body = data(client.get(f'{BASE}/payments/?page_size=1'))
        assert len(body['results']) == 1 and body['count'] == 1


# ---------------------------------------------------------------------------
# Payout configuration
# ---------------------------------------------------------------------------

class TestPayoutConfigRead:
    def test_hyderabad_finance_sees_india_configured_only(self, world):
        client, _ = world.finance
        countries = {c['country']: c for c in data(client.get(f'{BASE}/payout-config/'))['countries']}
        assert set(countries) == {'IN'} and countries['IN']['configured'] is True
        cfg = countries['IN']['config']
        assert cfg['fee_percent'] == '2.00' and cfg['minimum_withdrawal_minor'] == 50000
        assert cfg['stuck_payout_hours'] == 24 and cfg['currency'] == 'INR'

    def test_super_admin_sees_the_us_as_not_configured(self, world, geo):
        client, _ = operator('super_admin', geo)
        payload = data(client.get(f'{BASE}/payout-config/'))
        us = next(c for c in payload['countries'] if c['country'] == 'US')
        assert us['configured'] is False and us['config'] is None and 'Not configured' in us['message']
        assert payload['editable'] is False

    def test_there_is_no_write_route(self, world):
        client, _ = world.finance
        assert client.post(f'{BASE}/payout-config/', {}, format='json').status_code == 405


# ---------------------------------------------------------------------------
# No currency default
# ---------------------------------------------------------------------------

class TestNoCurrencyDefault:
    MODELS = [
        ('payments', 'Payment'), ('payments', 'TransactionHistory'), ('payments', 'TripSettlement'),
        ('driver', 'WithdrawalRequest'), ('rider', 'WalletTransaction'), ('ride', 'Trip'),
        ('ride', 'FarePricing'), ('ride', 'Receipt'), ('ride', 'PromoCode'),
        ('ride', 'PromoRedemption'), ('pricing', 'TripFareShadow'),
    ]

    @pytest.mark.parametrize('app,model', MODELS)
    def test_the_field_has_no_default(self, app, model):
        from django.apps import apps

        assert apps.get_model(app, model)._meta.get_field('currency').has_default() is False

    def test_a_us_trip_stamps_usd_on_everything_written_for_it(self, world):
        assert world.us_trip.currency == 'USD'
        assert world.us_payment.currency == 'USD'
        assert TripSettlement.objects.get(trip=world.us_trip).currency == 'USD'
        assert WalletTransaction.objects.get(reference_id=f'TRIP_{world.us_trip.id}').currency == 'USD'
        txn = TransactionHistory.objects.create(
            trip_id=world.us_trip, user_id=world.us_rider, driver_id=world.us_driver,
            amount_minor=100, method='online', status='completed')
        assert txn.currency == 'USD'

    def test_a_us_withdrawal_stamps_usd_from_the_drivers_wallet(self, world):
        assert world.us_withdrawal.currency == 'USD'

    def test_an_indian_trip_still_stamps_inr(self, world):
        assert world.in_trip.currency == 'INR' and world.in_payment.currency == 'INR'


# ---------------------------------------------------------------------------
# Payout configuration: India unchanged, a country with none is refused
# ---------------------------------------------------------------------------

class TestIndiaPayoutsAreUnchanged:
    LEGACY_BALANCE_KEYS = ['available_balance', 'blocked', 'block_remaining_seconds',
                           'fee_percent', 'minimum_withdrawal']

    def test_the_balance_endpoint_has_the_same_shape_and_values(self, world):
        client = client_for(world.in_driver.user_id)
        body = client.get('/api/v1/driver/withdrawals/balance/').json()['data']
        assert list(body) == self.LEGACY_BALANCE_KEYS
        assert body['fee_percent'] == 2.0 and body['minimum_withdrawal'] == 500.0
        assert isinstance(body['fee_percent'], float) and isinstance(body['minimum_withdrawal'], float)
        assert body['blocked'] is False and body['block_remaining_seconds'] is None
        assert body['available_balance'] == float(calculate_available_balance(world.in_driver))

    def test_the_minimum_is_still_500_rupees_in_rupees(self, world):
        client = client_for(world.in_driver.user_id)
        refused = client.post('/api/v1/driver/withdrawals/request/', {'amount': '499.00'}, format='json')
        assert refused.status_code == 400
        assert 'Minimum withdrawal amount is ₹500.00' in str(refused.json())

    def test_a_valid_request_creates_a_pending_withdrawal_in_inr(self, world):
        w = world.in_pending
        assert w.status == 'pending' and w.amount_minor == 50000 and w.currency == 'INR'

    def test_a_us_driver_with_no_configuration_still_reads_a_balance(self, world):
        client = client_for(world.us_driver.user_id)
        body = client.get('/api/v1/driver/withdrawals/balance/').json()['data']
        assert body['withdrawals_available'] is False
        assert body['message'] == 'Withdrawals are not available in your country yet.'
        assert body['fee_percent'] is None and body['minimum_withdrawal'] is None
        assert 'available_balance' in body

    def test_a_us_withdrawal_request_is_refused_cleanly_and_moves_nothing(self, world):
        wallet = Wallet.objects.get(user_id=world.us_driver.user_id, scope=Wallet.SCOPE_DRIVER)
        before = (wallet.balance_minor, WithdrawalRequest.objects.count(),
                  WalletTransaction.objects.count())
        client = client_for(world.us_driver.user_id)
        response = client.post('/api/v1/driver/withdrawals/request/', {'amount': '5.00'}, format='json')
        assert response.status_code == 400
        assert response.json()['error']['code'] == 'WITHDRAWALS_NOT_AVAILABLE'
        wallet.refresh_from_db()
        assert (wallet.balance_minor, WithdrawalRequest.objects.count(),
                WalletTransaction.objects.count()) == before

    def test_a_new_indian_driver_with_no_home_city_and_no_wallet_still_gets_indias_rules(self, geo):
        """Home city is set by operators, not by sign-up, and a wallet appears
        at the first settlement. Before either exists the driver is still placed:
        by the country of their phone number."""
        seed_in_payout_config()
        d = make_driver(geo, 'IN-TG-HYD', 'NEW', currency='INR', calling='91', city_node=False)
        Wallet.objects.filter(user_id=d.user_id).delete()
        body = client_for(d.user_id).get('/api/v1/driver/withdrawals/balance/').json()['data']
        assert list(body) == self.LEGACY_BALANCE_KEYS
        assert body['fee_percent'] == 2.0 and body['minimum_withdrawal'] == 500.0

    def test_a_us_driver_is_never_given_indias_rules_whatever_their_wallet_says(self, geo):
        """A wallet written without a currency was labelled INR. The phone number
        is the better witness."""
        seed_in_payout_config()
        d = make_driver(geo, 'US-TX-AUS', 'USW', currency='INR', calling='1', city_node=False)
        body = client_for(d.user_id).get('/api/v1/driver/withdrawals/balance/').json()['data']
        assert body['withdrawals_available'] is False and body['fee_percent'] is None

    def test_a_driver_who_can_be_placed_nowhere_has_no_rules(self, geo):
        seed_in_payout_config()
        d = make_driver(geo, 'IN-TG-HYD', 'NOW', currency='XXX', calling='91', city_node=False)
        User.objects.filter(pk=d.user_id_id).update(phone_number='+99912345678')
        Wallet.objects.filter(user_id=d.user_id).delete()
        d.refresh_from_db()
        body = client_for(d.user_id).get('/api/v1/driver/withdrawals/balance/').json()['data']
        assert body['withdrawals_available'] is False

    def test_a_us_row_makes_the_us_work_with_the_owners_numbers(self, world):
        PayoutCountryConfig.objects.create(
            country='US', fee_percent=Decimal('1.50'), minimum_withdrawal_minor=1000,
            stuck_payout_hours=12)
        client = client_for(world.us_driver.user_id)
        body = client.get('/api/v1/driver/withdrawals/balance/').json()['data']
        assert body['fee_percent'] == 1.5 and body['minimum_withdrawal'] == 10.0
        assert 'withdrawals_available' not in body


class TestSeedPayoutConfig:
    def test_it_creates_india_only_and_never_a_us_row(self, geo):
        seed_in_payout_config()
        rows = list(PayoutCountryConfig.objects.all())
        assert [(r.country, r.fee_percent, r.minimum_withdrawal_minor, r.stuck_payout_hours)
                for r in rows] == [('IN', Decimal('2.00'), 50000, 24)]
        assert not PayoutCountryConfig.objects.filter(country='US').exists()

    def test_a_minimum_is_an_amount_so_it_carries_its_countrys_currency(self, geo):
        seed_in_payout_config()
        assert PayoutCountryConfig.objects.get(country='IN').currency == 'INR'
        us = PayoutCountryConfig.objects.create(
            country='US', fee_percent=Decimal('1.00'), minimum_withdrawal_minor=1000)
        assert us.currency == 'USD'
        # whatever a caller passes, the country decides
        wrong = PayoutCountryConfig(country='US', currency='INR', fee_percent=Decimal('1.00'),
                                    minimum_withdrawal_minor=1, is_active=False, version=9)
        wrong.save()
        assert wrong.currency == 'USD'

    def test_the_fee_helper_has_no_default_percentage(self):
        from servers.driver.services import calculate_platform_fee

        assert calculate_platform_fee(Decimal('500.00'), Decimal('2.00')) == Decimal('10.00')
        with pytest.raises(TypeError):
            calculate_platform_fee(Decimal('500.00'))

    def test_a_second_run_writes_nothing_and_never_modifies_a_row(self, geo):
        seed_in_payout_config()
        PayoutCountryConfig.objects.filter(country='IN').update(fee_percent=Decimal('3.00'))
        audits = AdminAuditLog.objects.filter(action='payout_config_seeded').count()
        seed_in_payout_config()
        row = PayoutCountryConfig.objects.get(country='IN')
        assert row.fee_percent == Decimal('3.00')
        assert PayoutCountryConfig.objects.count() == 1
        assert AdminAuditLog.objects.filter(action='payout_config_seeded').count() == audits

    def test_it_is_audited_with_the_actor(self, geo):
        seed_in_payout_config()
        row = AdminAuditLog.objects.get(action='payout_config_seeded')
        assert row.actor_email == seed_actor().email

    def test_it_refuses_without_an_actor(self, geo):
        from django.core.management.base import CommandError

        with pytest.raises(CommandError):
            call_command('seed_payout_config', stdout=StringIO())


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

def rows_of(response):
    assert response.status_code == 200, response.content[:300]
    assert response['Content-Type'].startswith('text/csv')
    return list(csv.DictReader(io.StringIO(response.content.decode())))


class TestExports:
    @pytest.mark.parametrize('kind', ['settlements', 'payouts', 'payments'])
    def test_an_export_is_scoped(self, world, kind):
        client, _ = world.finance
        rows = rows_of(client.get(f'{BASE}/export/{kind}/'))
        assert rows
        currencies = {r['currency'] for r in rows}
        assert currencies == {'INR'}

    def test_super_admin_exports_both_markets(self, world, geo):
        client, _ = operator('super_admin', geo)
        rows = rows_of(client.get(f'{BASE}/export/settlements/'))
        assert {r['currency'] for r in rows} == {'INR', 'USD'}

    @pytest.mark.parametrize('kind', ['settlements', 'payouts', 'payments'])
    def test_an_export_is_masked_exactly_like_the_json(self, world, geo, kind):
        client, _ = operator('super_admin', geo)
        text = client.get(f'{BASE}/export/{kind}/').content.decode()
        for name, rx in RAW_PII_PATTERNS.items():
            assert not rx.search(text), (kind, name)
        assert 'order_IN_0001' not in text and 'driverIN@' not in text and 'driverUS@' not in text

    def test_reveal_is_not_honoured_for_an_export(self, world, geo):
        client, _ = operator('super_admin', geo)
        text = client.get(f'{BASE}/export/payouts/?reveal=1').content.decode()
        assert 'driverIN@' not in text
        assert not AdminAuditLog.objects.filter(action='pii_export_revealed').exists()

    def test_every_export_is_audited_with_its_filters_and_row_count(self, world):
        client, _ = world.finance
        rows = rows_of(client.get(f'{BASE}/export/settlements/?method=cash&currency=INR'))
        audit = AdminAuditLog.objects.get(action='finance_export')
        assert audit.target_id == 'settlements'
        assert audit.after['row_count'] == len(rows) == 1
        assert audit.after['filters'] == {'method': 'cash', 'currency': 'INR'}
        assert audit.actor_id == world.finance[1].id

    def test_the_row_cap_is_a_clear_400_and_writes_no_audit(self, world, settings):
        settings.FINANCE_EXPORT_MAX_ROWS = 1
        client, _ = world.finance
        response = client.get(f'{BASE}/export/settlements/')
        assert response.status_code == 400
        assert response.json()['error']['code'] == 'EXPORT_TOO_LARGE'
        assert not AdminAuditLog.objects.filter(action='finance_export').exists()
        assert rows_of(client.get(f'{BASE}/export/settlements/?method=cash'))

    def test_the_default_cap_is_fifty_thousand(self):
        from servers.payments.admin_finance import export_max_rows

        assert export_max_rows() == 50_000

    def test_finance_read_alone_cannot_export(self, world, geo):
        role = AdminRole.objects.create(code='fin_reader', name='Fin reader',
                                        permissions=['finance.read'])
        user = User.objects.create_user(phone_number='+919810099001', username='+919810099001',
                                        email='fr@saaradhigo.test', role='admin', is_staff=True)
        AdminAssignment.objects.create(admin=user, role=role, scope=geo['IN-TG-HYD'])
        client = client_for(user)
        assert client.get(f'{BASE}/payments/').status_code == 200
        assert client.get(f'{BASE}/export/settlements/').status_code == 403

    def test_an_unknown_kind_is_404(self, world):
        client, _ = world.finance
        assert client.get(f'{BASE}/export/everything/').status_code == 404

    def test_a_formula_in_a_cell_cannot_run_in_a_spreadsheet(self):
        from servers.payments.finance_common import csv_safe

        assert csv_safe('=HYPERLINK("http://x")') == "'=HYPERLINK(\"http://x\")"
        assert csv_safe('+1') == "'+1" and csv_safe('@a') == "'@a" and csv_safe('-1') == "'-1"
        assert csv_safe('plain') == 'plain' and csv_safe(5) == 5

    def test_a_failed_audit_write_means_no_export(self, world, monkeypatch):
        from servers.payments import admin_finance

        monkeypatch.setattr(admin_finance, 'record_admin_action', lambda *a, **k: None)
        client, _ = world.finance
        assert client.get(f'{BASE}/export/settlements/').status_code == 503
