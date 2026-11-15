"""When a trip's earnings reached the driver's bank (15 Nov, US round 4): "Paid out <date>".

Payouts pay the wallet balance, not trips, so the trip is matched first-in-first-out: its money is
paid out by the first completed payout that brings the payouts' running total up to the balance the
wallet held once the trip settled (every wallet line up to and including the trip's own, leaving out
payouts and their reversals). A cash trip never passes through the wallet, and a trip whose money
went to a debt (the balance stayed at or under zero) has nothing paid out: both are None.
"""

from servers.payments.ledger import driver_rows, signed_total

PAYOUT_PURPOSES = ('withdrawal', 'refund_failed_withdrawal', 'refund_rejected_withdrawal',
                   'refund_reversed_withdrawal')
PAID = ('completed', 'processed')


def paid_out_at(driver, entry):
    """The datetime of the payout that carried this settlement line, or None."""
    from servers.driver.models import WithdrawalRequest

    if entry is None or entry.txn_type != 'credit':
        return None
    owed = signed_total(driver_rows(driver, entry.currency)
                        .exclude(purpose__in=PAYOUT_PURPOSES).filter(id__lte=entry.id))
    if owed <= 0:
        return None
    total = 0
    for amount, at in (WithdrawalRequest.objects
                       .filter(driver=driver, currency=entry.currency, status__in=PAID,
                               processed_at__isnull=False)
                       .order_by('processed_at', 'id').values_list('amount_minor', 'processed_at')):
        total += amount
        if total >= owed:
            return at
    return None
