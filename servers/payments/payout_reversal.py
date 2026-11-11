"""A payout the provider reported PAID, then reversed (owner ruling, AUDIT 27.71).

RazorpayX sends `payout.reversed` after `payout.processed` when the beneficiary bank returns
the money (a closed account, a wrong IFSC): the transfer did not stay with the driver. Before
this the late reversal was ignored, so the driver's balance said the money was paid out when
the driver never received it.

Now, inside the webhook's locked transaction:

* the withdrawal is marked `reversed` (a status of its own: it was paid, then came back);
* the amount is credited back to the driver's settlement balance as a NEW ledger line,
  `refund_reversed_withdrawal` (append-only: the original withdrawal debit stays, and the
  unique key `refund_reversed_withdrawal_<id>` makes a repeated webhook write nothing);
* one Finance alert is raised (`finance.payout_reversed`, anchored to the driver's home city);
* after the commit, the driver is told through the notifications system
  (`payout.reversed`, in-app and push; with live sending off it is recorded, not sent).

A reversal that arrives BEFORE the payout was paid is unchanged: it is a failed payout and the
money comes back as `refund_failed_withdrawal`.
"""

import logging

from django.db import IntegrityError, transaction

from base.money import format_money

logger = logging.getLogger(__name__)

PURPOSE = 'refund_reversed_withdrawal'
TEMPLATE = 'payout.reversed'
ALERT_SOURCE_TYPE = 'withdrawal_reversed'
FAILURE_REASON = 'The payout was paid, then reversed by the bank'


def reverse_paid_payout(withdrawal, *, provider_status=None):
    """Call inside `transaction.atomic()` with `withdrawal` locked and `status == 'completed'`.
    Returns the credit row, or None if the reversal was already recorded."""
    from servers.command_centre.models import FINANCE_PAYOUT_REVERSED, WARNING
    from servers.command_centre.services import raise_alert
    from servers.rider.models import Wallet, WalletTransaction, get_wallet

    driver_user = withdrawal.driver.user_id
    wallet = get_wallet(driver_user, Wallet.SCOPE_DRIVER, lock=True)
    reference = f'{PURPOSE}_{withdrawal.id}'
    try:
        with transaction.atomic():
            txn = WalletTransaction.objects.create(
                user_id=driver_user, currency=withdrawal.currency,
                amount_minor=withdrawal.amount_minor, txn_type='credit', status='completed',
                purpose=PURPOSE, reference_id=reference, idempotency_key=reference)
    except IntegrityError:
        logger.info('payout_reversal_duplicate_suppressed', extra={
            'event': 'payout_reversal_duplicate_suppressed', 'withdrawal_id': withdrawal.id})
        return None
    wallet.balance_minor = wallet.balance_minor + withdrawal.amount_minor
    wallet.save(update_fields=['balance_minor'])

    withdrawal.status = 'reversed'
    withdrawal.payout_status = provider_status or 'reversed'
    withdrawal.failure_reason = FAILURE_REASON
    withdrawal.save(update_fields=['status', 'payout_status', 'failure_reason'])

    city = getattr(withdrawal.driver, 'home_city', None)
    amount = format_money(withdrawal.amount, withdrawal.currency)
    raise_alert(
        FINANCE_PAYOUT_REVERSED, severity=WARNING,
        title=(f'Payout {withdrawal.id} of {amount} was reversed after it was paid'
               f'{" in " + city.name if city is not None else ""}')[:200],
        payload={'withdrawal_id': withdrawal.id, 'driver_id': withdrawal.driver_id,
                 'amount_minor': withdrawal.amount_minor, 'currency': withdrawal.currency,
                 'payout_reference_id': withdrawal.payout_reference_id or '',
                 'city': city.code if city is not None else None},
        source_type=ALERT_SOURCE_TYPE, source_id=str(withdrawal.id), city=city)

    withdrawal_id, user_pk = withdrawal.id, driver_user.pk
    transaction.on_commit(lambda: notify_driver(withdrawal_id, user_pk, amount))
    logger.warning('payout_reversed_after_paid', extra={
        'event': 'payout_reversed_after_paid', 'withdrawal_id': withdrawal.id})
    return txn


def notify_driver(withdrawal_id, user_pk, amount):
    """Best effort, after the commit: a notice must never undo the credit."""
    from django.contrib.auth import get_user_model

    try:
        from servers.notifications import dispatcher

        user = get_user_model().objects.filter(pk=user_pk).first()
        if user is not None:
            dispatcher.notify(
                TEMPLATE, user, {'amount': amount}, channels=('in_app', 'push'),
                idempotency_key=f'{TEMPLATE}:{withdrawal_id}')
    except Exception:  # noqa: BLE001
        logger.exception('payout reversal: driver notice failed for withdrawal %s', withdrawal_id)
