"""A retried payout that then fails at the provider is refunded (independent review HIGH-2, AUDIT 27.75).

First attempt fails before reaching the provider and is refunded (`refund_withdrawal_<id>`); Finance
retries, which debits the wallet again (`withdrawal_retry_<id>_1`) and dispatches; the provider then
fails or reverses it. The webhook used the fixed reference `refund_withdrawal_<id>`, found the first
attempt's refund, skipped the refund, and still marked the payout failed: the driver lost the amount.
Every test runs on both payout adapters.
"""

from decimal import Decimal as D

import pytest

from test_payout_provider_contract import _balance, driver, world  # noqa: F401
from servers.driver.models import WithdrawalRequest
from servers.rider.models import Wallet, WalletTransaction, get_wallet

pytestmark = pytest.mark.django_db


def _deliver(client, world, kind, reference, event_id):  # noqa: F811
    body, headers = world.webhook(kind, reference, event_id)
    return client.post(world.webhook_path, data=body, content_type='application/json', **headers)


def _retried_and_dispatched(driver):  # noqa: F811
    """Attempt 1 failed and was refunded; Finance retried (wallet debited again); dispatched."""
    w = WithdrawalRequest.objects.create(driver=driver, amount=D('1000.00'), payout_method='upi',
                                         status='processed')
    uid = driver.user_id
    WalletTransaction.objects.create(user_id=uid, amount_minor=100_000, txn_type='debit',
                                     status='pending', purpose='withdrawal',
                                     reference_id=f'withdrawal_{w.id}',
                                     idempotency_key=f'withdrawal_{w.id}')
    WalletTransaction.objects.create(user_id=uid, amount_minor=100_000, txn_type='credit',
                                     status='completed', purpose='refund_failed_withdrawal',
                                     reference_id=f'refund_withdrawal_{w.id}',
                                     idempotency_key=f'refund_withdrawal_{w.id}')
    WalletTransaction.objects.create(user_id=uid, amount_minor=100_000, txn_type='debit',
                                     status='pending', purpose='withdrawal',
                                     reference_id=f'withdrawal_{w.id}',
                                     idempotency_key=f'withdrawal_retry_{w.id}_1')
    wallet = get_wallet(uid, Wallet.SCOPE_DRIVER)
    wallet.balance_minor -= 100_000          # the retry's debit (the first was refunded)
    wallet.save(update_fields=['balance_minor'])
    w.payout_reference_id = f'withdrawal_{w.id}_driver_{driver.id}'
    w.save(update_fields=['payout_reference_id'])
    return w


def _refunds(driver):  # noqa: F811
    return list(WalletTransaction.objects.filter(
        user_id=driver.user_id, purpose='refund_failed_withdrawal').order_by('id')
        .values_list('reference_id', flat=True))


@pytest.mark.parametrize('kind', ['failed', 'reversed'])
def test_a_retried_payout_failing_at_the_provider_is_refunded(world, driver, api_client, kind):  # noqa: F811
    w = _retried_and_dispatched(driver)
    before = _balance(driver)
    assert _deliver(api_client, world, kind, w.payout_reference_id, f'evt_{kind}').status_code == 200
    w.refresh_from_db()
    assert w.status == 'failed'
    assert _balance(driver) == before + D('1000.00')
    assert _refunds(driver) == [f'refund_withdrawal_{w.id}', f'refund_withdrawal_{w.id}_r1']


def test_a_resent_failure_refunds_the_retry_once(world, driver, api_client):  # noqa: F811
    w = _retried_and_dispatched(driver)
    before = _balance(driver)
    _deliver(api_client, world, 'failed', w.payout_reference_id, 'evt_f1')
    _deliver(api_client, world, 'failed', w.payout_reference_id, 'evt_f2')
    assert _balance(driver) == before + D('1000.00')
    assert len(_refunds(driver)) == 2


def test_a_first_attempt_keeps_its_old_reference(world, driver, api_client):  # noqa: F811
    w = WithdrawalRequest.objects.create(driver=driver, amount=D('1000.00'), payout_method='upi',
                                         status='processed')
    w.payout_reference_id = f'withdrawal_{w.id}_driver_{driver.id}'
    w.save(update_fields=['payout_reference_id'])
    _deliver(api_client, world, 'failed', w.payout_reference_id, 'evt_first')
    assert _refunds(driver) == [f'refund_withdrawal_{w.id}']


def test_both_refund_paths_share_one_reference():
    from servers.payments.payout_actions import failed_refund_reference

    assert failed_refund_reference(7) == 'refund_withdrawal_7'
