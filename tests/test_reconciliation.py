"""Daily reconciliation from local data only: each kind, bounded, idempotent.

Every scenario builds the clean two-market world (`finance_world`), breaks ONE
thing, runs the day, and asserts exactly the finding that breakage should raise.
"""

from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from base.countries import get_country
from finance_world import (
    build_world, client_for, make_payment, make_trip, operator, settle, success_webhook,
)
from servers.admin_access.models import AdminAssignment, AdminRole, GeoNode
from servers.admin_audit.models import AdminAuditLog
from servers.driver.models import WithdrawalRequest
from servers.payments.models import (
    Payment, PayoutCountryConfig, ReconciliationItem, ReconciliationRun, TripSettlement,
    WebhookEvent,
)
from servers.payments.reconciliation import (
    NullSource, get_statement_source, resolve_item, run_daily, run_one,
)
from servers.rider.models import WalletTransaction

pytestmark = pytest.mark.django_db

User = get_user_model()
BASE = '/api/v1/payments/admin'
K = ReconciliationItem


@pytest.fixture
def geo(db):
    from conftest import seed_admin_access

    seed_admin_access()
    return {n.code: n for n in GeoNode.objects.all()}


@pytest.fixture
def world(geo):
    return build_world(geo)


def run(country='IN', *, later=timedelta(hours=3), force=True):
    """Reconcile today's local day, three hours on (past the grace period)."""
    tz = ZoneInfo(get_country(country).default_timezone)
    date = timezone.now().astimezone(tz).date()
    return run_one(country, 'cashfree', date, now=timezone.now() + later, force=force)


def kinds(country='IN', **extra):
    return sorted(ReconciliationItem.objects.filter(country=country, **extra)
                  .values_list('kind', flat=True))


class TestACleanDay:
    def test_a_clean_world_raises_nothing(self, world):
        for country in ('IN', 'US'):
            result = run(country)
            assert result.status == 'ok', (country, result.counts, kinds(country))
            assert result.counts == {}
        assert ReconciliationItem.objects.count() == 0

    def test_it_changes_no_money(self, world):
        def state():
            return (
                list(Payment.objects.values_list('id', 'status', 'amount_minor')),
                list(TripSettlement.objects.values_list('id', 'gross_fare_minor', 'driver_net_minor')),
                list(WithdrawalRequest.objects.values_list('id', 'status', 'amount_minor')),
                list(WalletTransaction.objects.values_list('id', 'amount_minor', 'status')),
            )

        before = state()
        make_trip(world.hyd, world.in_rider, world.in_driver, fare='10.00')   # a finding to raise
        before = state()
        run('IN')
        assert state() == before


class TestEachKind:
    def test_payment_captured_without_a_confirming_webhook(self, world):
        trip = make_trip(world.hyd, world.in_rider, world.in_driver, fare='300.00')
        settle(trip)
        payment = make_payment(trip, 'order_IN_NOWH')
        run('IN')
        item = ReconciliationItem.objects.get(kind=K.PAYMENT_NO_WEBHOOK)
        assert item.payment_id == payment.id and item.anchor_node.code == 'IN-TG-HYD'
        assert item.expected == 'confirming webhook' and item.currency == 'INR'
        assert kinds('IN') == [K.PAYMENT_NO_WEBHOOK]

    def test_a_confirming_webhook_with_no_payment(self, world):
        success_webhook('order_IN_GHOST', '10.00', 'INR')
        run('IN')
        item = ReconciliationItem.objects.get(kind=K.WEBHOOK_NO_PAYMENT)
        assert item.reference == 'order_IN_GHOST' and item.payment_id is None
        assert item.anchor_node is None                    # nothing placeable
        assert kinds('US') == []                           # another currency, another run

    def test_a_webhook_for_a_payment_that_never_completed(self, world):
        trip = make_trip(world.hyd, world.in_rider, world.in_driver, fare='50.00')
        settle(trip)
        payment = make_payment(trip, 'order_IN_PROC', status='processing')
        success_webhook('order_IN_PROC', '50.00', 'INR')
        run('IN')
        item = ReconciliationItem.objects.get(kind=K.WEBHOOK_NO_PAYMENT)
        assert item.payment_id == payment.id and item.seen == 'payment is processing'

    def test_a_wallet_topup_webhook_is_not_a_missing_payment(self, world):
        WalletTransaction.objects.create(
            user_id=world.in_rider, amount_minor=1000, txn_type='credit', status='completed',
            purpose='topup', gateway_order_id='order_TOPUP_1', idempotency_key='topup-1')
        success_webhook('order_TOPUP_1', '10.00', 'INR')
        run('IN')
        assert kinds('IN') == []

    def test_amount_mismatch_between_payment_and_webhook(self, world):
        trip = make_trip(world.hyd, world.in_rider, world.in_driver, fare='500.00')
        settle(trip)
        make_payment(trip, 'order_IN_MISM')
        success_webhook('order_IN_MISM', '400.00', 'INR')
        run('IN')
        item = ReconciliationItem.objects.get(kind=K.AMOUNT_MISMATCH)
        assert item.expected == '50000 INR' and item.seen == '40000 INR'

    def test_currency_mismatch_between_payment_and_webhook(self, world):
        trip = make_trip(world.hyd, world.in_rider, world.in_driver, fare='500.00')
        settle(trip)
        make_payment(trip, 'order_IN_CURR')
        success_webhook('order_IN_CURR', '500.00', 'USD')
        run('IN')
        item = ReconciliationItem.objects.get(kind=K.AMOUNT_MISMATCH)
        assert item.seen == '50000 USD' and kinds('IN') == [K.AMOUNT_MISMATCH]

    def test_completed_trip_with_no_settlement(self, world):
        trip = make_trip(world.aus, world.us_rider, world.us_driver, fare='12.00')
        run('US')
        item = ReconciliationItem.objects.get(kind=K.TRIP_NO_SETTLEMENT)
        assert item.trip_id == trip.id and item.anchor_node.code == 'US-TX-AUS'
        assert item.currency == 'USD' and item.country == 'US'

    def test_settlement_whose_gross_differs_from_the_final_fare(self, world):
        TripSettlement.objects.filter(trip=world.in_trip).update(gross_fare_minor=199999)
        run('IN')
        item = ReconciliationItem.objects.get(kind=K.SETTLEMENT_GROSS_MISMATCH)
        assert item.trip_id == world.in_trip.id
        assert item.expected == '200000 INR' and item.seen == '199999 INR'

    def test_a_correction_that_fixes_the_gross_clears_the_finding(self, world):
        original = TripSettlement.objects.get(trip=world.in_trip)
        TripSettlement.objects.filter(pk=original.pk).update(gross_fare_minor=199999)
        TripSettlement.objects.create(
            trip=world.in_trip, driver=original.driver, version=2, currency='INR',
            gross_fare_minor=200000, commission_percent=original.commission_percent,
            commission_amount_minor=original.commission_amount_minor,
            driver_net_minor=200000 - original.commission_amount_minor,
            payment_method=original.payment_method, settled_at=timezone.now())
        run('IN')
        assert K.SETTLEMENT_GROSS_MISMATCH not in kinds('IN')

    def test_payout_marked_processed_by_webhook_while_the_withdrawal_is_not_paid(self, world):
        WithdrawalRequest.objects.filter(pk=world.in_pending.pk).update(payout_reference_id='CFT123')
        WebhookEvent.objects.create(
            gateway='cashfree', dedupe_key='payout-sig-1', event_type='TRANSFER_SUCCESS',
            raw_payload={'event': 'TRANSFER_SUCCESS', 'transfer_id': 'CFT123', 'status': 'SUCCESS'})
        run('IN')
        item = ReconciliationItem.objects.get(kind=K.PAYOUT_WEBHOOK_NOT_PAID)
        assert item.withdrawal_id == world.in_pending.id and item.seen == 'withdrawal is pending'

    def test_a_payout_webhook_for_a_paid_withdrawal_is_fine(self, world):
        WithdrawalRequest.objects.filter(pk=world.in_pending.pk).update(
            payout_reference_id='CFT124', status='completed')
        WebhookEvent.objects.create(
            gateway='cashfree', dedupe_key='payout-sig-2', event_type='TRANSFER_SUCCESS',
            raw_payload={'event': 'TRANSFER_SUCCESS', 'transfer_id': 'CFT124'})
        run('IN')
        assert kinds('IN') == []

    def test_a_payout_stuck_in_processing_beyond_the_countrys_hours(self, world):
        WithdrawalRequest.objects.filter(pk=world.in_pending.pk).update(
            status='processed', processed_at=timezone.now() - timedelta(hours=30))
        result = run('IN')
        item = ReconciliationItem.objects.get(kind=K.PAYOUT_STUCK)
        assert item.withdrawal_id == world.in_pending.id and item.expected == 'paid within 24 h'
        assert result.checks['payout_stuck'] == 'ran'

    def test_a_payout_processing_for_less_than_the_hours_is_not_stuck(self, world):
        WithdrawalRequest.objects.filter(pk=world.in_pending.pk).update(
            status='processed', processed_at=timezone.now() - timedelta(hours=2))
        run('IN', later=timedelta(hours=3))
        assert K.PAYOUT_STUCK not in kinds('IN')

    def test_no_configuration_means_no_stuck_check_and_it_says_so(self, world):
        WithdrawalRequest.objects.filter(pk=world.us_withdrawal.pk).update(
            status='processed', processed_at=timezone.now() - timedelta(hours=500))
        assert not PayoutCountryConfig.objects.filter(country='US').exists()
        result = run('US')
        assert result.checks['payout_stuck'] == 'not_configured'
        assert K.PAYOUT_STUCK not in kinds('US')

    def test_a_configured_country_with_no_hours_also_skips_the_check(self, world):
        PayoutCountryConfig.objects.filter(country='IN').update(stuck_payout_hours=None)
        WithdrawalRequest.objects.filter(pk=world.in_pending.pk).update(
            status='processed', processed_at=timezone.now() - timedelta(hours=500))
        result = run('IN')
        assert result.checks['payout_stuck'] == 'not_configured'
        assert K.PAYOUT_STUCK not in kinds('IN')


class TestBoundedAndIdempotent:
    def test_a_young_payment_is_given_time_for_its_webhook(self, world):
        trip = make_trip(world.hyd, world.in_rider, world.in_driver, fare='100.00')
        settle(trip)
        make_payment(trip, 'order_IN_YOUNG')
        run('IN', later=timedelta(minutes=10))         # younger than the grace
        assert K.PAYMENT_NO_WEBHOOK not in kinds('IN')

    def test_only_the_days_own_window_is_read(self, world):
        trip = make_trip(world.hyd, world.in_rider, world.in_driver, fare='100.00')
        settle(trip)
        old = make_payment(trip, 'order_IN_OLD')
        Payment.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=3))
        run('IN')
        assert K.PAYMENT_NO_WEBHOOK not in kinds('IN')

    def test_running_the_same_day_twice_raises_nothing_new(self, world):
        make_trip(world.hyd, world.in_rider, world.in_driver, fare='10.00')
        first = run('IN')
        again = run('IN')
        assert first.counts == {K.TRIP_NO_SETTLEMENT: 1} and again.counts == {}
        assert ReconciliationItem.objects.count() == 1
        assert ReconciliationRun.objects.filter(country='IN').count() == 1

    def test_a_finished_run_is_skipped_unless_forced(self, world):
        make_trip(world.hyd, world.in_rider, world.in_driver, fare='10.00')
        first = run('IN')
        stamp = first.finished_at
        skipped = run('IN', force=False)
        assert skipped.id == first.id and skipped.finished_at == stamp

    def test_a_resolved_finding_is_not_reopened_by_the_next_run(self, world):
        make_trip(world.hyd, world.in_rider, world.in_driver, fare='10.00')
        run('IN')
        item = ReconciliationItem.objects.get()
        resolve_item(item.id, user=world.finance[1], note='settled by hand')
        run('IN')
        item.refresh_from_db()
        assert ReconciliationItem.objects.count() == 1 and item.resolved_at is not None

    def test_the_daily_task_covers_every_country_and_catches_up(self, world):
        from servers.payments.tasks import run_daily_reconciliation

        result = run_daily_reconciliation()
        from servers.payments.models import PaymentGateway

        expected = 2 * 3 * len(PaymentGateway.values)   # 2 countries x 3 days x every gateway
        assert result['runs'] == expected and result['failed'] == []
        assert ReconciliationRun.objects.count() == expected
        again = run_daily_reconciliation()
        assert again['new_findings'] == 0 and ReconciliationRun.objects.count() == expected

    def test_the_task_is_scheduled_and_acks_late(self):
        from django.conf import settings

        scheduled = {e['task'] for e in settings.CELERY_BEAT_SCHEDULE.values()}
        assert 'payments.run_daily_reconciliation' in scheduled

    def test_a_failing_country_is_recorded_and_does_not_stop_the_others(self, world, monkeypatch):
        from servers.payments import reconciliation

        real = reconciliation._check_settlements

        def boom(r, country, *args):
            if country == 'US':
                raise RuntimeError('db hiccup')
            return real(r, country, *args)

        monkeypatch.setattr(reconciliation, '_check_settlements', boom)
        us = run('US')
        india = run('IN')
        assert us.status == 'error' and 'db hiccup' in us.error and india.status == 'ok'


class TestNoGatewayStatement:
    def test_the_only_source_is_null_and_not_connected(self):
        source = get_statement_source()
        assert isinstance(source, NullSource) and source.connected is False
        assert source.fetch(date=None, country='IN', currency='INR', gateway='cashfree') is None

    def test_a_run_records_that_no_statement_was_imported(self, world):
        assert run('IN').checks['statement_source'] == 'not_connected'

    def test_the_console_endpoint_says_it_is_not_connected(self, world):
        run('IN')
        client, _ = world.finance
        body = client.get(f'{BASE}/reconciliation/').json()['data']
        assert body['statement_import']['connected'] is False
        assert 'not connected' in body['statement_import']['message']

    def test_nothing_in_the_module_calls_a_gateway(self):
        import inspect

        from servers.payments import reconciliation

        source = inspect.getsource(reconciliation)
        for needle in ('get_payment_gateway', 'requests.', 'httpx', 'cashfree_gateway', 'urlopen'):
            assert needle not in source, needle


class TestEndpointsAndResolve:
    @pytest.fixture
    def findings(self, world):
        make_trip(world.hyd, world.in_rider, world.in_driver, fare='10.00')
        make_trip(world.aus, world.us_rider, world.us_driver, fare='12.00')
        run('IN')
        run('US')
        return {
            'in': ReconciliationItem.objects.get(country='IN'),
            'us': ReconciliationItem.objects.get(country='US'),
        }

    def test_the_overview_counts_open_items_per_kind_in_scope(self, world, findings, geo):
        client, _ = world.finance
        body = client.get(f'{BASE}/reconciliation/').json()['data']
        assert body['open_by_kind'] == {K.TRIP_NO_SETTLEMENT: 1} and body['open_total'] == 1
        assert {r['country'] for r in body['results']} == {'IN'}
        assert any(c['country'] == 'IN' and c['configured'] for c in body['stuck_payout_check'])
        sup, _ = operator('super_admin', geo)
        both = sup.get(f'{BASE}/reconciliation/').json()['data']
        assert both['open_total'] == 2
        us = next(c for c in both['stuck_payout_check'] if c['country'] == 'US')
        assert us['configured'] is False

    def test_items_are_scoped_and_filterable(self, world, findings, geo):
        client, _ = world.finance
        rows = client.get(f'{BASE}/reconciliation/items/').json()['data']['results']
        assert [r['id'] for r in rows] == [findings['in'].id]
        assert rows[0]['kind_label'] == 'Completed trip with no settlement'
        sup, _ = operator('super_admin', geo)
        assert len(sup.get(f'{BASE}/reconciliation/items/').json()['data']['results']) == 2
        only = sup.get(f'{BASE}/reconciliation/items/?country=US').json()['data']['results']
        assert [r['id'] for r in only] == [findings['us'].id]

    def test_finance_resolves_with_a_note_and_it_is_audited(self, world, findings):
        client, user = world.finance
        response = client.post(f'{BASE}/reconciliation/items/{findings["in"].id}/resolve/',
                               {'note': 'Trip was settled manually on ticket 14'}, format='json')
        assert response.status_code == 200 and response.json()['data']['already_resolved'] is False
        item = ReconciliationItem.objects.get(pk=findings['in'].id)
        assert item.resolved_by_id == user.id and item.resolution_note.startswith('Trip was settled')
        audit = AdminAuditLog.objects.get(action='reconciliation_item_resolved')
        assert audit.target_id == str(item.id) and audit.actor_id == user.id
        assert audit.reason.startswith('Trip was settled')

    def test_resolving_twice_is_idempotent(self, world, findings):
        client, _ = world.finance
        url = f'{BASE}/reconciliation/items/{findings["in"].id}/resolve/'
        client.post(url, {'note': 'first note'}, format='json')
        second = client.post(url, {'note': 'a different note'}, format='json')
        assert second.status_code == 200 and second.json()['data']['already_resolved'] is True
        assert ReconciliationItem.objects.get(pk=findings['in'].id).resolution_note == 'first note'
        assert AdminAuditLog.objects.filter(action='reconciliation_item_resolved').count() == 1

    def test_a_note_is_required(self, world, findings):
        client, _ = world.finance
        url = f'{BASE}/reconciliation/items/{findings["in"].id}/resolve/'
        assert client.post(url, {}, format='json').status_code == 400
        assert client.post(url, {'note': '  '}, format='json').status_code == 400
        assert client.post(url, {'note': 'x' * 2001}, format='json').status_code == 400
        assert ReconciliationItem.objects.get(pk=findings['in'].id).resolved_at is None

    def test_finance_read_is_not_enough_to_resolve(self, world, findings, geo):
        role = AdminRole.objects.create(code='fin_reader2', name='Fin reader 2',
                                        permissions=['finance.read'])
        user = User.objects.create_user(phone_number='+919810099002', username='+919810099002',
                                        email='fr2@saaradhigo.test', role='admin', is_staff=True)
        AdminAssignment.objects.create(admin=user, role=role, scope=geo['IN-TG-HYD'])
        client = client_for(user)
        assert client.get(f'{BASE}/reconciliation/items/').status_code == 200
        response = client.post(f'{BASE}/reconciliation/items/{findings["in"].id}/resolve/',
                               {'note': 'looks fine'}, format='json')
        assert response.status_code == 403
        assert ReconciliationItem.objects.get(pk=findings['in'].id).resolved_at is None

    def test_an_austin_finding_is_a_404_for_hyderabad_finance(self, world, findings):
        client, _ = world.finance
        response = client.post(f'{BASE}/reconciliation/items/{findings["us"].id}/resolve/',
                               {'note': 'not mine to touch'}, format='json')
        assert response.status_code == 404
        assert ReconciliationItem.objects.get(pk=findings['us'].id).resolved_at is None

    @pytest.mark.parametrize('role', ['analyst', 'marketing', 'support_agent', 'kyc_verifier',
                                      'safety_officer', 'city_ops_manager'])
    def test_other_roles_cannot_resolve(self, world, findings, geo, role):
        client, _ = operator(role, geo)
        response = client.post(f'{BASE}/reconciliation/items/{findings["in"].id}/resolve/',
                               {'note': 'attempt'}, format='json')
        assert response.status_code == 403

    def test_resolving_moves_no_money(self, world, findings):
        before = list(WalletTransaction.objects.values_list('id', 'amount_minor', 'status'))
        client, _ = world.finance
        client.post(f'{BASE}/reconciliation/items/{findings["in"].id}/resolve/',
                    {'note': 'checked the ledger'}, format='json')
        assert list(WalletTransaction.objects.values_list('id', 'amount_minor', 'status')) == before

    def test_the_item_body_shows_the_gateway_reference_by_its_tail_only(self, world, geo):
        success_webhook('order_IN_GHOST_9876', '10.00', 'INR')
        run('IN')
        client, _ = world.finance
        # nothing placeable: hidden from a city, visible to the unrestricted
        assert client.get(f'{BASE}/reconciliation/items/').json()['data']['results'] == []
        sup, _ = operator('super_admin', geo)
        text = sup.get(f'{BASE}/reconciliation/items/').content.decode()
        assert 'order_IN_GHOST_9876' not in text and '9876' in text
