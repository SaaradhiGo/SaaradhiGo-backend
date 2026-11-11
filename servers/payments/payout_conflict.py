"""A payout the provider reports PAID after we failed it and refunded the wallet (AUDIT 27.72).

The sequence: the payout fails (or is rejected), the amount goes back to the driver's balance
(`refund_failed_withdrawal` / `refund_rejected_withdrawal`), and later the provider sends a
success for the same payout. Marking it paid would leave the driver with the transfer AND the
refund; crediting nothing and ignoring it would leave a paid payout recorded as failed. Neither is
decided automatically.

* The payout is frozen as `conflict`. No money moves, and no "paid" history row is written, so
  reports do not count it as paid while it is undecided.
* One CRITICAL Finance alert is raised (`finance.payout_conflict`).
* Finance resolves it through the ordinary driver-balance adjustment (maker-checker): a DEBIT of
  the payout's amount that names the payout (`withdrawal_id`). When the checker approves, the
  debit takes the refund back, the payout becomes `completed` (it was paid), the paid history row
  is written and the alert is cleared, all in one transaction.
* If the provider instead reverses the payout (the money came back), it returns to `failed` and
  the original refund stands; nothing else moves.

While a payout is in conflict nothing more is paid to that driver (independent review HIGH-1,
AUDIT 27.75): a driver withdrawal is refused, a pending one cannot be approved or released, and the
weekly run skips them (`conflict` is an open status). The resolution debit may take the balance
below zero, because the refund may already have been withdrawn before the late "paid" arrived; the
negative balance is then recovered from later earnings, as a cash driver's commission owed is.
"""

import logging

from django.utils import timezone

from base.money import format_money

logger = logging.getLogger(__name__)

CONFLICT = 'conflict'
ALERT_SOURCE_TYPE = 'withdrawal_conflict'
REASON = 'The provider reported this payout paid after it had failed and been refunded'
REFUNDED_STATUSES = ('failed', 'rejected')


def has_open_conflict(driver_id):
    """True while any payout of this driver is waiting for a Finance decision."""
    from servers.driver.models import WithdrawalRequest

    return WithdrawalRequest.objects.filter(driver_id=driver_id, status=CONFLICT).exists()


def freeze(withdrawal, *, provider_status=None, provider='', payout_id=''):
    """Call inside the webhook's transaction with `withdrawal` locked and in a refunded status."""
    from servers.command_centre.models import CRITICAL, FINANCE_PAYOUT_CONFLICT
    from servers.command_centre.services import raise_alert

    before = withdrawal.status
    withdrawal.status = CONFLICT
    withdrawal.payout_status = provider_status or 'success'
    withdrawal.failure_reason = REASON
    withdrawal.save(update_fields=['status', 'payout_status', 'failure_reason'])
    city = getattr(withdrawal.driver, 'home_city', None)
    raise_alert(
        FINANCE_PAYOUT_CONFLICT, severity=CRITICAL,
        title=(f'Payout {withdrawal.id} of {format_money(withdrawal.amount, withdrawal.currency)} '
               f'reported paid after it was refunded'
               f'{" in " + city.name if city is not None else ""}')[:200],
        payload={'withdrawal_id': withdrawal.id, 'driver_id': withdrawal.driver_id,
                 'amount_minor': withdrawal.amount_minor, 'currency': withdrawal.currency,
                 'status_before': before, 'provider': provider or '',
                 'payout_id': str(payout_id or ''),
                 'payout_reference_id': withdrawal.payout_reference_id or '',
                 'city': city.code if city is not None else None},
        source_type=ALERT_SOURCE_TYPE, source_id=str(withdrawal.id), city=city)
    logger.error('payout_paid_after_refund', extra={
        'event': 'payout_paid_after_refund', 'withdrawal_id': withdrawal.id,
        'status_before': before})


def reverse_conflict(withdrawal, *, provider_status=None):
    """The provider reversed a payout in conflict: the money came back, the refund stands."""
    withdrawal.status = 'failed'
    withdrawal.payout_status = provider_status or 'reversed'
    withdrawal.failure_reason = 'Reported paid after the refund, then reversed: the refund stands'
    withdrawal.save(update_fields=['status', 'payout_status', 'failure_reason'])
    clear_alert(withdrawal.id, 'the provider reversed the payout; the refund stands')


def check_resolution(driver, payload):
    """For the adjustment request: the payout named must be this driver's, in conflict, and the
    request a debit of exactly its amount in its currency. Returns the withdrawal, or raises
    ValueError(code, message)."""
    from servers.driver.models import WithdrawalRequest

    withdrawal = WithdrawalRequest.objects.filter(
        pk=payload.get('withdrawal_id'), driver=driver).first()
    if withdrawal is None:
        raise ValueError('PAYOUT_NOT_FOUND', "That payout is not this driver's.")
    if withdrawal.status != CONFLICT:
        raise ValueError('PAYOUT_NOT_IN_CONFLICT', 'That payout is not waiting for a decision.')
    if payload.get('direction') != 'debit' or int(payload.get('amount_minor') or 0) != (
            withdrawal.amount_minor) or payload.get('currency') != withdrawal.currency:
        raise ValueError('RESOLUTION_MISMATCH',
                         'Resolving a payout conflict is a debit of exactly the payout amount.')
    return withdrawal


def resolve_paid(withdrawal_id, *, adjustment_txn, request_id):
    """Inside the adjustment's transaction, after the debit: the payout was paid."""
    from servers.driver.models import WithdrawalRequest
    from servers.payments.models import TransactionHistory

    withdrawal = (WithdrawalRequest.objects.select_for_update()
                  .select_related('driver__user_id').get(pk=withdrawal_id))
    if withdrawal.status != CONFLICT:
        from servers.approvals.registry import ExecutionRefused

        raise ExecutionRefused('PAYOUT_NOT_IN_CONFLICT',
                               'That payout is no longer waiting for a decision.')
    withdrawal.status = 'completed'
    withdrawal.failure_reason = (f'Paid after a refund; the refund was taken back by adjustment '
                                 f'request {request_id}')
    withdrawal.processed_at = withdrawal.processed_at or timezone.now()
    withdrawal.save(update_fields=['status', 'failure_reason', 'processed_at'])
    from servers.command_centre.models import OperatorAlert

    alert = (OperatorAlert.objects.filter(source_type=ALERT_SOURCE_TYPE, source_id=str(withdrawal.id))
             .order_by('-created_at').first())
    seen = (alert.payload if alert is not None else {}) or {}
    user = withdrawal.driver.user_id
    TransactionHistory.objects.create(
        withdrawal_request=withdrawal, user_id=user, driver_id=withdrawal.driver,
        currency=withdrawal.currency, amount_minor=withdrawal.amount_minor,
        method=withdrawal.payout_method, payment_gateway=seen.get('provider') or '',
        gateway_payment_id=seen.get('payout_id') or withdrawal.payout_reference_id or '',
        user_name=user.full_name or user.phone_number, status='completed', txn_type='credit')
    clear_alert(withdrawal.id, f'resolved as paid by adjustment request {request_id} '
                               f'(ledger row {adjustment_txn.id})')
    return withdrawal


def clear_alert(withdrawal_id, why):
    from servers.command_centre.models import OperatorAlert

    return OperatorAlert.objects.filter(
        source_type=ALERT_SOURCE_TYPE, source_id=str(withdrawal_id),
        acknowledged_at__isnull=True,
    ).update(acknowledged_at=timezone.now(), acknowledged_by=None,
             acknowledged_note=f'cleared: {why}')
