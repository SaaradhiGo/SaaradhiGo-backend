"""A payout reported PAID after it failed and was refunded (owner ruling, AUDIT 27.72).

The exact sequence: failed -> refunded to the wallet -> a late "paid" webhook. It must never mark the
payout paid and never credit twice. The payout is frozen as `conflict`, Finance gets a critical
alert, and it is resolved through a Finance adjustment (maker-checker). Every webhook test runs on
both payout adapters (Cashfree and RazorpayX).
"""

from decimal import Decimal as D
from io import StringIO

import pytest
from django.core.management import call_command

from notifications_support import geo  # noqa: F401
from test_payout_provider_contract import world  # noqa: F401
from finance_world import make_driver, operator
from test_finance_actions import approve_request, body_of, checker, error_code, post, seed_actor
from servers.command_centre.models import FINANCE_ALERT_KINDS, OperatorAlert
from servers.driver.models import WithdrawalRequest
from servers.payments.models import TransactionHistory
from servers.rider.models import Wallet, WalletTransaction, get_wallet

pytestmark = pytest.mark.django_db
ADJUST = '/api/v1/payments/admin/drivers/{}/adjust/'


@pytest.fixture
def driver(geo):  # noqa: F811
    call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=StringIO())
    d = make_driver(geo, 'IN-TG-HYD', 'CF', currency='INR', calling='91')
    wallet = get_wallet(d.user_id, Wallet.SCOPE_DRIVER)
    wallet.balance = D('5000.00')
    wallet.currency = 'INR'
    wallet.save(update_fields=['balance_minor', 'currency'])
    return d


def _balance(driver):
    return get_wallet(driver.user_id, Wallet.SCOPE_DRIVER).balance_minor


def _deliver(client, world, kind, reference, event_id):  # noqa: F811
    body, headers = world.webhook(kind, reference, event_id)
    return client.post(world.webhook_path, data=body, content_type='application/json', **headers)


def _failed_and_refunded(client, world, driver):  # noqa: F811
    """Request (balance debited) -> dispatched -> failed webhook (refunded)."""
    wallet = get_wallet(driver.user_id, Wallet.SCOPE_DRIVER)
    wallet.balance_minor -= 100_000
    wallet.save(update_fields=['balance_minor'])
    w = WithdrawalRequest.objects.create(driver=driver, amount=D('1000.00'), payout_method='upi',
                                         status='processed', currency='INR')
    WalletTransaction.objects.create(
        user_id=driver.user_id, amount_minor=100_000, currency='INR', txn_type='debit',
        status='pending', purpose='withdrawal', reference_id=f'withdrawal_{w.id}',
        idempotency_key=f'withdrawal_{w.id}')
    w.payout_reference_id = f'withdrawal_{w.id}_driver_{driver.id}'
    w.save(update_fields=['payout_reference_id'])
    assert _deliver(client, world, 'failed', w.payout_reference_id, 'evt_fail').status_code == 200
    w.refresh_from_db()
    assert w.status == 'failed'
    assert WalletTransaction.objects.filter(purpose='refund_failed_withdrawal',
                                            reference_id=f'refund_withdrawal_{w.id}').count() == 1
    return w


def _paid_history(w):
    return TransactionHistory.objects.filter(withdrawal_request=w, status='completed')


class TestTheLatePaidWebhook:
    def test_failed_refunded_then_paid_is_frozen_with_no_money_moved(self, world, driver,  # noqa: F811
                                                                    api_client):
        w = _failed_and_refunded(api_client, world, driver)
        after_refund = _balance(driver)
        assert after_refund == 500_000
        response = _deliver(api_client, world, 'success', w.payout_reference_id, 'evt_late')
        assert response.status_code == 200 and response.json()['status'] == 'conflict'
        w.refresh_from_db()
        assert w.status == 'conflict'
        assert _balance(driver) == after_refund
        assert not _paid_history(w).exists()
        assert WalletTransaction.objects.filter(user_id=driver.user_id,
                                                purpose='refund_failed_withdrawal').count() == 1

    def test_one_critical_finance_alert_without_pii(self, world, driver, api_client):  # noqa: F811
        w = _failed_and_refunded(api_client, world, driver)
        _deliver(api_client, world, 'success', w.payout_reference_id, 'evt_late')
        alert = OperatorAlert.objects.get(kind='finance.payout_conflict')
        assert alert.kind in FINANCE_ALERT_KINDS and alert.severity == 'critical'
        assert (alert.payload['withdrawal_id'], alert.payload['amount_minor'],
                alert.payload['status_before']) == (w.id, 100_000, 'failed')
        assert alert.acknowledged_at is None
        assert '+91' not in alert.title and driver.user_id.full_name not in alert.title

    def test_repeated_late_webhooks_change_nothing(self, world, driver, api_client):  # noqa: F811
        w = _failed_and_refunded(api_client, world, driver)
        for i in range(3):
            _deliver(api_client, world, 'success', w.payout_reference_id, f'evt_late{i}')
        _deliver(api_client, world, 'failed', w.payout_reference_id, 'evt_fail_again')
        w.refresh_from_db()
        assert w.status == 'conflict' and _balance(driver) == 500_000
        assert OperatorAlert.objects.filter(kind='finance.payout_conflict').count() == 1
        assert WalletTransaction.objects.filter(user_id=driver.user_id,
                                                purpose='refund_failed_withdrawal').count() == 1

    def test_a_reversal_after_the_conflict_leaves_the_refund_standing(self, world, driver,  # noqa: F811
                                                                     api_client):
        w = _failed_and_refunded(api_client, world, driver)
        _deliver(api_client, world, 'success', w.payout_reference_id, 'evt_late')
        _deliver(api_client, world, 'reversed', w.payout_reference_id, 'evt_rev')
        w.refresh_from_db()
        assert w.status == 'failed' and _balance(driver) == 500_000
        assert not WalletTransaction.objects.filter(purpose='refund_reversed_withdrawal').exists()
        assert OperatorAlert.objects.get(kind='finance.payout_conflict').acknowledged_at

    def test_a_conflict_holds_the_next_payout(self, world, driver, api_client):  # noqa: F811
        from servers.payments.weekly_payout import OPEN_STATUSES

        w = _failed_and_refunded(api_client, world, driver)
        _deliver(api_client, world, 'success', w.payout_reference_id, 'evt_late')
        assert 'conflict' in OPEN_STATUSES


class TestFinanceResolvesIt:
    def _conflict(self, api_client, world, driver):  # noqa: F811
        w = _failed_and_refunded(api_client, world, driver)
        _deliver(api_client, world, 'success', w.payout_reference_id, 'evt_late')
        return w

    def _ask(self, maker, driver, w, **over):
        body = {'direction': 'debit', 'amount_minor': 100_000, 'currency': 'INR',
                'reason_code': 'correction', 'note': 'Payout reported paid after its refund',
                'idempotency_key': f'conflict-{w.id}', 'withdrawal_id': w.id}
        body.update(over)
        return post(maker, ADJUST.format(driver.id), body)

    def test_it_always_waits_for_a_checker_then_takes_the_refund_back(self, world, driver,  # noqa: F811
                                                                      api_client, geo):  # noqa: F811
        w = self._conflict(api_client, world, driver)
        maker, _ = operator('finance', geo)
        r = self._ask(maker, driver, w)
        assert r.status_code == 202 and body_of(r)['state'] == 'pending_approval'
        assert _balance(driver) == 500_000
        chk, _ = checker()
        assert approve_request(chk, body_of(r)['approval_request']['id']).status_code == 200
        w.refresh_from_db()
        assert w.status == 'completed'
        assert _balance(driver) == 400_000          # the refund is taken back exactly once
        assert _paid_history(w).count() == 1
        assert OperatorAlert.objects.get(kind='finance.payout_conflict').acknowledged_at
        debit = WalletTransaction.objects.get(user_id=driver.user_id, purpose='adjustment')
        assert (debit.txn_type, debit.amount_minor) == ('debit', 100_000)

    def test_approving_twice_debits_once(self, world, driver, api_client, geo):  # noqa: F811
        w = self._conflict(api_client, world, driver)
        maker, _ = operator('finance', geo)
        request_id = body_of(self._ask(maker, driver, w))['approval_request']['id']
        chk, _ = checker()
        approve_request(chk, request_id)
        approve_request(chk, request_id)
        assert _balance(driver) == 400_000
        assert WalletTransaction.objects.filter(purpose='adjustment').count() == 1

    @pytest.mark.parametrize('over, code', [
        ({'amount_minor': 50_000}, 'RESOLUTION_MISMATCH'),
        ({'direction': 'credit'}, 'RESOLUTION_MISMATCH'),
    ])
    def test_anything_but_a_debit_of_the_payout_amount_is_refused(self, world, driver,  # noqa: F811
                                                                  api_client, geo, over, code):  # noqa: F811
        w = self._conflict(api_client, world, driver)
        maker, _ = operator('finance', geo)
        r = self._ask(maker, driver, w, **over)
        assert r.status_code == 409 and error_code(r) == code

    def test_a_payout_not_in_conflict_cannot_be_named(self, world, driver, api_client,  # noqa: F811
                                                      geo):  # noqa: F811
        w = _failed_and_refunded(api_client, world, driver)
        maker, _ = operator('finance', geo)
        r = self._ask(maker, driver, w)
        assert r.status_code == 409 and error_code(r) == 'PAYOUT_NOT_IN_CONFLICT'

    def test_a_reversal_before_approval_makes_the_approval_refuse(self, world, driver,  # noqa: F811
                                                                  api_client, geo):  # noqa: F811
        w = self._conflict(api_client, world, driver)
        maker, _ = operator('finance', geo)
        request_id = body_of(self._ask(maker, driver, w))['approval_request']['id']
        _deliver(api_client, world, 'reversed', w.payout_reference_id, 'evt_rev')
        chk, _ = checker()
        approve_request(chk, request_id)
        w.refresh_from_db()
        assert w.status == 'failed' and _balance(driver) == 500_000
        assert not WalletTransaction.objects.filter(purpose='adjustment').exists()


class TestNothingMoreIsPaidWhileInConflict:
    """Independent review HIGH-1 (AUDIT 27.75): the driver must not be able to take the refund out
    again while the payout is undecided, and if they already did, the resolution must still work."""

    def _conflict(self, api_client, world, driver):  # noqa: F811
        w = _failed_and_refunded(api_client, world, driver)
        _deliver(api_client, world, 'success', w.payout_reference_id, 'evt_late')
        w.refresh_from_db()
        assert w.status == 'conflict'
        return w

    def test_a_driver_withdrawal_is_refused(self, world, driver, api_client):  # noqa: F811
        from finance_world import client_for, seed_in_payout_config

        seed_in_payout_config()
        self._conflict(api_client, world, driver)
        r = client_for(driver.user_id).post('/api/v1/driver/withdrawals/request/',
                                            {'amount': '500.00', 'payout_method': 'upi'},
                                            format='json')
        assert r.status_code == 400 and r.json()['error']['code'] == 'PAYOUT_ON_HOLD'

    def test_a_pending_withdrawal_cannot_be_approved_or_released(self, world, driver,  # noqa: F811
                                                                 api_client, geo):  # noqa: F811
        from servers.approvals.executors import _approve_one

        self._conflict(api_client, world, driver)
        pending = WithdrawalRequest.objects.create(driver=driver, amount=D('500.00'),
                                                   payout_method='upi', status='pending')
        maker, _ = operator('finance', geo)
        r = post(maker, f'/api/v1/payments/admin/payouts/{pending.id}/approve/',
                 {'reason': 'weekly'})
        assert r.status_code == 409 and error_code(r) == 'PAYOUT_CONFLICT_OPEN'
        assert _approve_one(pending.id, 'checker') is None       # the executor re-checks
        pending.refresh_from_db()
        assert pending.status == 'pending'

    def test_the_refund_already_withdrawn_is_still_recovered(self, world, driver,  # noqa: F811
                                                             api_client, geo):  # noqa: F811
        """The reviewer's sequence: fail -> refund -> the driver withdraws it again (paid) -> the
        late "paid" for the first payout -> Finance resolves it. The debit takes the balance below
        zero rather than being refused, and later earnings recover it."""
        w = _failed_and_refunded(api_client, world, driver)
        wallet = get_wallet(driver.user_id, Wallet.SCOPE_DRIVER)
        wallet.balance_minor = 30_000                  # the refund has gone out again
        wallet.save(update_fields=['balance_minor'])
        _deliver(api_client, world, 'success', w.payout_reference_id, 'evt_late')
        maker, _ = operator('finance', geo)
        r = post(maker, ADJUST.format(driver.id), {
            'direction': 'debit', 'amount_minor': 100_000, 'currency': 'INR',
            'reason_code': 'correction', 'note': 'Payout reported paid after its refund',
            'idempotency_key': f'conflict-{w.id}', 'withdrawal_id': w.id})
        assert r.status_code == 202, r.content
        chk, _ = checker()
        assert approve_request(chk, body_of(r)['approval_request']['id']).status_code == 200
        w.refresh_from_db()
        assert w.status == 'completed'
        assert _balance(driver) == -70_000

    def test_any_other_debit_still_cannot_go_below_zero(self, world, driver, geo):  # noqa: F811
        maker, _ = operator('finance', geo)
        r = post(maker, ADJUST.format(driver.id), {
            'direction': 'debit', 'amount_minor': _balance(driver) + 1, 'currency': 'INR',
            'reason_code': 'correction', 'note': 'An ordinary correction',
            'idempotency_key': 'plain-debit'})
        assert r.status_code == 409 and error_code(r) == 'BALANCE_WOULD_GO_NEGATIVE'
