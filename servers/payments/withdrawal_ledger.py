"""A withdrawal's ledger debit reaches a final state with its payout (AUDIT §35, decision 18 reversed).

The debit row (`purpose='withdrawal'`, `reference_id='withdrawal_<id>'`) is written `pending`
when the driver asks, and the wallet is debited then. When the withdrawal itself reaches a
final status, its debit rows follow in the same database transaction:

  * `completed` (Paid)                      -> the pending debit rows become `completed`
  * `failed`, `rejected`, `reversed`        -> the debit rows become `failed`; the money comes
                                               back through the reversal row written today
                                               (`refund_failed_withdrawal`,
                                               `refund_rejected_withdrawal`,
                                               `refund_reversed_withdrawal`)

`unresolved` and `conflict` are not final: the rows wait. Safe to run twice and in any
order: it only moves rows towards the withdrawal's current status, so a duplicate webhook
changes nothing and a late success after a reversal leaves them failed.

The BALANCE never moves here. `ledger.counted` counts a withdrawal debit whether pending,
completed or failed: a failed debit is the half of a pair whose other half is its reversal
row, exactly as the pending debit was before this change.
"""
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

PAID = 'completed'
FAILED = ('failed', 'rejected', 'reversed')
FINAL = (PAID, *FAILED)


def reference_of(withdrawal_id):
    return f'withdrawal_{withdrawal_id}'


def finalise(withdrawal):
    """Move the withdrawal's debit rows to its final status. Returns the number of rows moved."""
    from servers.rider.models import WalletTransaction

    rows = WalletTransaction.objects.filter(purpose='withdrawal', txn_type='debit',
                                            reference_id=reference_of(withdrawal.pk))
    with transaction.atomic():
        if withdrawal.status == PAID:
            return rows.filter(status='pending').update(status='completed')
        if withdrawal.status in FAILED:
            return rows.filter(status__in=('pending', 'completed')).update(status='failed')
    return 0


@receiver(post_save, sender='driver.WithdrawalRequest', dispatch_uid='withdrawal_ledger_finalise')
def _on_withdrawal_saved(sender, instance, update_fields=None, **kwargs):
    if update_fields is not None and 'status' not in update_fields:
        return
    if instance.status in FINAL:
        finalise(instance)
