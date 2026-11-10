"""A withdrawal's ledger debit reaches a final state with its payout (AUDIT §35, item 18 reversed).

Paid -> the debit row is completed; failed or reversed -> it is failed and the reversal row
returns the money. In the same transaction as the payout status, safe twice, in any order. The
balance never moves: it equals the signed sum of the counted rows throughout.
"""
import importlib
from datetime import timedelta
from unittest import mock

import pytest
from django.apps import apps as django_apps
from django.utils import timezone

from finance_world import client_for
from servers.driver.models import DriverUPIContact, WithdrawalRequest
from servers.payments import ledger
from servers.payments import views as payment_views
from servers.payments.models import ReconciliationItem
from servers.rider.models import Wallet, WalletTransaction
from test_leftovers_9nov import _india_instant, hyd  # noqa: F401
from test_pricing_module import geo, world  # noqa: F401

pytestmark = [pytest.mark.django_db, pytest.mark.no_default_commission]


@pytest.fixture
def driver(hyd, settings):  # noqa: F811
    from integrations.payouts import fake

    settings.PAYOUT_FAKE_ALLOWED = True
    _india_instant()
    d = hyd.driver
    d.upi_id = fake.SUCCESS_VPA
    d.save(update_fields=['upi_id'])
    DriverUPIContact.objects.create(driver=d, upi_id=fake.SUCCESS_VPA, is_active=True)
    # An opening balance with its own ledger row, so balance == rows from the start.
    Wallet.objects.filter(user_id=d.user_id, scope='driver').update(balance_minor=50000)
    WalletTransaction.objects.create(user_id=d.user_id, amount_minor=50000, txn_type='credit',
                                     status='completed', purpose='adjustment',
                                     reference_id='adjustment_opening', idempotency_key='adjustment_opening',
                                     currency='INR')
    return d


def withdraw(d):
    scheduled = []
    with mock.patch('servers.payments.tasks.fake_payout_settle.apply_async',
                    side_effect=lambda args, countdown: scheduled.append(args)):
        r = client_for(d.user_id).post('/api/v1/driver/withdrawals/request/',
                                       {'amount': '200', 'instant': True}, format='json')
    assert r.status_code == 201, r.content[:300]
    return WithdrawalRequest.objects.get(origin='instant'), scheduled[0]


def webhook(event, ref, gateway_ref):
    from integrations.payouts.fake import FakePayoutGateway

    return payment_views._handle_cashfree_payout_webhook({
        'event': event, 'transfer_id': ref, 'cf_transfer_id': gateway_ref,
        'status': {'TRANSFER_SUCCESS': 'processed', 'TRANSFER_FAILED': 'failed'}.get(event, 'reversed'),
        'failureReason': 'test'}, FakePayoutGateway())


def debit_rows(w):
    return WalletTransaction.objects.filter(purpose='withdrawal', txn_type='debit', reference_id=f'withdrawal_{w.id}')


def balance(d):
    return Wallet.objects.get(user_id=d.user_id, scope='driver').balance_minor


def signed(qs):
    return ledger.signed_total(qs)


def assert_balance_matches(d):
    rows = ledger.driver_rows(d, 'INR')
    assert balance(d) == signed(rows)


def no_row_pending_after_final():
    """No withdrawal debit is pending while its withdrawal is final (so never 24 h after it)."""
    from servers.payments.withdrawal_ledger import FINAL

    final_ids = set(WithdrawalRequest.objects.filter(status__in=FINAL).values_list('id', flat=True))
    pending = WalletTransaction.objects.filter(purpose='withdrawal', status='pending')
    return not [r for r in pending if int(r.reference_id.split('_')[1]) in final_ids]


class TestPaid:
    def test_paid_completes_the_row_and_the_balance_is_the_completed_rows(self, driver):
        w, (ref, gref) = withdraw(driver)
        assert w.status == 'processed' and set(debit_rows(w).values_list('status', flat=True)) == {'pending'}
        assert_balance_matches(driver)                                   # in flight: pending counts
        webhook('TRANSFER_SUCCESS', ref, gref)
        w.refresh_from_db()
        assert w.status == 'completed'
        assert set(debit_rows(w).values_list('status', flat=True)) == {'completed'}
        assert no_row_pending_after_final()
        # Nothing in flight: the balance is exactly the sum of the completed rows.
        completed = WalletTransaction.objects.filter(user_id=driver.user_id, status='completed',
                                                     purpose__in=ledger.DRIVER_LEDGER_PURPOSES)
        assert balance(driver) == signed(completed) == 50000 - 20000

    def test_the_same_webhook_twice_changes_nothing(self, driver):
        w, (ref, gref) = withdraw(driver)
        webhook('TRANSFER_SUCCESS', ref, gref)
        before = list(WalletTransaction.objects.order_by('id').values_list('id', 'status', 'amount_minor'))
        webhook('TRANSFER_SUCCESS', ref, gref)
        assert list(WalletTransaction.objects.order_by('id').values_list('id', 'status', 'amount_minor')) == before
        assert_balance_matches(driver)

    def test_status_and_row_change_in_one_transaction(self, driver):
        w, (ref, gref) = withdraw(driver)
        with mock.patch('servers.payments.views.TransactionHistory.objects.get_or_create',
                        side_effect=RuntimeError('boom')):
            with pytest.raises(RuntimeError):
                webhook('TRANSFER_SUCCESS', ref, gref)
        w.refresh_from_db()
        assert w.status == 'processed'                                   # rolled back together
        assert set(debit_rows(w).values_list('status', flat=True)) == {'pending'}


class TestFailedAndReversed:
    def test_failed_marks_the_row_failed_and_returns_the_money_once(self, driver):
        w, (ref, gref) = withdraw(driver)
        webhook('TRANSFER_FAILED', ref, gref)
        webhook('TRANSFER_FAILED', ref, gref)
        w.refresh_from_db()
        assert w.status == 'failed'
        assert set(debit_rows(w).values_list('status', flat=True)) == {'failed'}
        assert WalletTransaction.objects.filter(purpose='refund_failed_withdrawal').count() == 1
        assert balance(driver) == 50000
        assert_balance_matches(driver)
        assert no_row_pending_after_final()

    def test_paid_then_reversed(self, driver):
        w, (ref, gref) = withdraw(driver)
        webhook('TRANSFER_SUCCESS', ref, gref)
        webhook('TRANSFER_REVERSED', ref, gref)
        w.refresh_from_db()
        assert w.status == 'reversed'
        assert set(debit_rows(w).values_list('status', flat=True)) == {'failed'}
        assert WalletTransaction.objects.filter(purpose='refund_reversed_withdrawal').count() == 1
        assert balance(driver) == 50000
        assert_balance_matches(driver)

    def test_a_late_success_after_a_failure_never_completes_the_row(self, driver):
        w, (ref, gref) = withdraw(driver)
        webhook('TRANSFER_FAILED', ref, gref)
        webhook('TRANSFER_SUCCESS', ref, gref)                          # out of order
        w.refresh_from_db()
        assert w.status != 'completed'
        assert set(debit_rows(w).values_list('status', flat=True)) == {'failed'}
        assert_balance_matches(driver)


class TestReconciliationAndBackfill:
    def _legacy_pending(self, driver):
        """A withdrawal paid before this change: its row left pending (bypasses the signal)."""
        w, (ref, gref) = withdraw(driver)
        webhook('TRANSFER_SUCCESS', ref, gref)
        debit_rows(w).update(status='pending')
        return w

    def test_reconciliation_flags_a_pending_row_whose_payout_is_final(self, driver):
        from servers.payments.reconciliation import run_one

        w = self._legacy_pending(driver)
        run_one('IN', 'razorpay', timezone.localdate(), force=True)
        item = ReconciliationItem.objects.get(kind=ReconciliationItem.WITHDRAWAL_ROW_PENDING)
        assert item.withdrawal_id == w.id and item.expected == 'completed'

    def test_the_backfill_completes_rows_of_paid_payouts(self, driver):
        w = self._legacy_pending(driver)
        migration = importlib.import_module('servers.payments.migrations.0029_backfill_withdrawal_rows_final')
        migration.forwards(django_apps, None)
        assert set(debit_rows(w).values_list('status', flat=True)) == {'completed'}
        assert no_row_pending_after_final()
        assert_balance_matches(driver)


class TestEndToEnd:
    def test_withdraw_now_ends_with_a_completed_row(self, driver):
        from integrations.payouts import fake

        w, scheduled = withdraw(driver)
        fake.settle(*scheduled)
        w.refresh_from_db()
        row = debit_rows(w).get()
        assert w.status == 'completed' and row.status == 'completed' and row.amount_minor == 20000


class TestInstantGuards:
    def test_a_payout_pause_stops_withdraw_now(self, driver):
        with mock.patch('servers.fraud.money.active_payout_hold', return_value=object()):
            r = client_for(driver.user_id).post('/api/v1/driver/withdrawals/request/',
                                                {'amount': '200', 'instant': True}, format='json')
        assert r.status_code == 400 and r.json()['error']['code'] == 'PAYOUT_ON_HOLD'
        assert not WithdrawalRequest.objects.exists()

    def test_withdraw_now_is_refused_while_another_payout_awaits_finance(self, driver):
        with mock.patch('servers.payments.payout_conflict.has_open_conflict', return_value=True):
            r = client_for(driver.user_id).post('/api/v1/driver/withdrawals/request/',
                                                {'amount': '200', 'instant': True}, format='json')
        assert r.status_code == 400 and r.json()['error']['code'] == 'PAYOUT_ON_HOLD'


def test_rows_older_than_a_day_are_not_left_pending(driver):
    """The 24-hour rule, with the clock moved on a day past the final status."""
    w, (ref, gref) = withdraw(driver)
    webhook('TRANSFER_SUCCESS', ref, gref)
    later = timezone.now() + timedelta(hours=24, minutes=1)
    with mock.patch('django.utils.timezone.now', return_value=later):
        assert no_row_pending_after_final()
