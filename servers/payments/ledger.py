"""One driver's statement: opening balance, movements, closing balance.

WHAT THE BALANCE IS MADE OF
---------------------------
The driver's settlement balance (`Wallet`, scope driver) moves only through
`WalletTransaction` rows carrying one of `DRIVER_LEDGER_PURPOSES`:

  * `trip_earnings`  credit   an online or wallet trip: the platform owes the net fare
  * `trip_commission` debit   a cash trip: the driver owes the commission
  * `withdrawal`     debit    a payout request (taken at request time)
  * `refund_rejected_withdrawal` / `refund_failed_withdrawal`  credit   money
    returned because the payout was rejected, or failed at the gateway

A row counts when it is `completed`, or when it is the pending debit of a
withdrawal (taken from the balance at request time). So

    balance == sum(credits) - sum(debits)   over those rows, for ever,

and the statement for a date range is `opening + movements = closing`, where
`opening` is that same sum over everything BEFORE the range. A test pins
`closing == calculate_available_balance(driver)` when the range ends now.

WHAT THE LEDGER DOES NOT HIDE
-----------------------------
`unexplained_minor` is `wallet balance - sum over all rows to date`. It is zero
when the two agree. A wallet that was seeded or edited without a ledger row
shows a non-zero number HERE rather than a statement that quietly closes on a
different figure. The balance function is never changed to fit the ledger.

Memo lines (`affects_balance` false) are shown in time order but are not in any
sum: a payout being PAID moves no money in the wallet (it left at request
time), and a manual fare adjustment is recorded against the trip, not the
wallet.

FEES
----
Today the commission is the only fee, and it is on the settlement line. The
withdrawal fee percentage in `PayoutCountryConfig` is configuration the
withdrawal path does not charge, so the statement has no fee line to show.
"""

from django.db.models import BigIntegerField, Case, F, Q, Sum, Value, When
from django.db.models.functions import Coalesce

from servers.payments.scope import DRIVER_LEDGER_PURPOSES

SIGNED = Case(
    When(txn_type='credit', then=F('amount_minor')),
    When(txn_type='debit', then=-F('amount_minor')),
    default=Value(0), output_field=BigIntegerField(),
)

ENTRY_KINDS = {
    'trip_earnings': 'settlement',
    'trip_commission': 'settlement',
    'withdrawal': 'withdrawal_requested',
    'refund_rejected_withdrawal': 'withdrawal_rejected',
    'refund_failed_withdrawal': 'withdrawal_returned',
}


def counted(queryset):
    """Only the rows that are part of the balance."""
    return queryset.filter(Q(status='completed') | Q(purpose='withdrawal', status='pending'))


def driver_rows(driver, currency):
    from servers.rider.models import WalletTransaction

    return counted(WalletTransaction.objects.filter(
        user_id=driver.user_id_id, purpose__in=DRIVER_LEDGER_PURPOSES, currency=currency))


def wallet_of(driver):
    from servers.rider.models import Wallet

    return Wallet.objects.filter(user_id=driver.user_id_id, scope=Wallet.SCOPE_DRIVER).first()


def signed_total(queryset):
    return queryset.aggregate(t=Coalesce(Sum(SIGNED), Value(0), output_field=BigIntegerField()))['t']


def build_statement(driver, start, end):
    """The ledger totals for `[start, end)` (aware datetimes), without entries.

    Returns a dict, or None when the driver has no driver wallet.
    """
    wallet = wallet_of(driver)
    if wallet is None:
        return None
    currency = wallet.currency
    rows = driver_rows(driver, currency)
    opening = signed_total(rows.filter(created_at__lt=start))
    in_range = rows.filter(created_at__gte=start, created_at__lt=end)
    credits = in_range.filter(txn_type='credit').aggregate(
        t=Coalesce(Sum('amount_minor'), Value(0), output_field=BigIntegerField()))['t']
    debits = in_range.filter(txn_type='debit').aggregate(
        t=Coalesce(Sum('amount_minor'), Value(0), output_field=BigIntegerField()))['t']
    all_to_date = signed_total(rows)
    from servers.rider.models import WalletTransaction

    excluded = WalletTransaction.objects.filter(
        user_id=driver.user_id_id, purpose__in=DRIVER_LEDGER_PURPOSES,
    ).exclude(currency=currency).count()
    return {
        'currency': currency,
        'opening_minor': opening,
        'credits_minor': credits,
        'debits_minor': debits,
        'closing_minor': opening + credits - debits,
        'balance_minor': wallet.balance_minor,
        'unexplained_minor': wallet.balance_minor - all_to_date,
        'other_currency_rows': excluded,
        'rows': in_range,
    }


def entry_dicts(driver, in_range_rows, opening_minor, offset, limit):
    """One page of statement entries with a running balance.

    The running balance is the opening plus every earlier row in the range, so
    a page deep into the range still starts from the right figure.
    """
    from servers.payments.models import TripSettlement

    ordered = in_range_rows.order_by('created_at', 'id')
    before_page = signed_total(ordered[:offset]) if offset else 0
    page = list(ordered[offset:offset + limit])
    running = opening_minor + before_page

    trip_ids = []
    for txn in page:
        ref = txn.reference_id or ''
        if ref.startswith('TRIP_') and ref[5:].isdigit():
            trip_ids.append(int(ref[5:]))
    settlements = {}
    if trip_ids:
        for s in (TripSettlement.objects.filter(trip_id__in=trip_ids, wallet_transaction__isnull=False)
                  .order_by('version')):
            settlements[s.trip_id] = s

    out = []
    for txn in page:
        signed = txn.amount_minor if txn.txn_type == 'credit' else -txn.amount_minor
        running += signed
        entry = {
            'id': txn.id,
            'at': txn.created_at,
            'kind': ENTRY_KINDS.get(txn.purpose, txn.purpose),
            'purpose': txn.purpose,
            'direction': txn.txn_type,
            'amount_minor': signed,
            'currency': txn.currency,
            'running_balance_minor': running,
            'status': txn.status,
            'affects_balance': True,
            'reference': txn.reference_id,
            'trip_id': None,
            'withdrawal_id': None,
        }
        ref = txn.reference_id or ''
        if ref.startswith('TRIP_') and ref[5:].isdigit():
            entry['trip_id'] = int(ref[5:])
            s = settlements.get(entry['trip_id'])
            if s is not None:
                entry['settlement'] = {
                    'gross_fare_minor': s.gross_fare_minor,
                    'commission_percent': str(s.commission_percent),
                    'commission_amount_minor': s.commission_amount_minor,
                    'driver_net_minor': s.driver_net_minor,
                    'payment_method': s.payment_method,
                    'version': s.version,
                }
        for prefix in ('refund_withdrawal_', 'refund_rejected_withdrawal_', 'withdrawal_'):
            if ref.startswith(prefix) and ref[len(prefix):].isdigit():
                entry['withdrawal_id'] = int(ref[len(prefix):])
                break
        out.append(entry)
    return out


def memo_lines(driver, currency, start, end):
    """Events worth showing that move no money in the wallet."""
    from servers.driver.models import WithdrawalRequest
    from servers.payments.models import TransactionHistory
    from servers.ride.models import TripMoneyAction

    lines = []
    paid = (TransactionHistory.objects
            .filter(driver_id=driver, withdrawal_request__isnull=False, status='completed',
                    currency=currency, created_at__gte=start, created_at__lt=end)
            .order_by('created_at', 'id'))
    for t in paid:
        lines.append({
            'id': f'paid-{t.id}', 'at': t.created_at, 'kind': 'withdrawal_paid',
            'amount_minor': t.amount_minor, 'currency': t.currency,
            'affects_balance': False, 'withdrawal_id': t.withdrawal_request_id,
            'note': 'The payout reached the gateway as paid; the money left the wallet when it was requested.',
        })
    for w in (WithdrawalRequest.objects.filter(driver=driver, status='rejected', currency=currency,
                                               processed_at__gte=start, processed_at__lt=end)):
        lines.append({
            'id': f'rejected-{w.id}', 'at': w.processed_at, 'kind': 'withdrawal_rejected_decision',
            'amount_minor': w.amount_minor, 'currency': w.currency,
            'affects_balance': False, 'withdrawal_id': w.id,
            'note': 'The request was rejected; the refund line carries the money back.',
        })
    adjustments = (TripMoneyAction.objects
                   .filter(trip__driver_id=driver, kind=TripMoneyAction.ADJUSTMENT,
                           currency=currency, created_at__gte=start, created_at__lt=end)
                   .order_by('created_at', 'id'))
    for a in adjustments:
        lines.append({
            'id': f'adjustment-{a.id}', 'at': a.created_at, 'kind': 'adjustment',
            'amount_minor': a.amount_minor, 'currency': a.currency,
            'affects_balance': False, 'trip_id': a.trip_id, 'status': a.status,
            'note': 'A fare adjustment recorded against the trip; it does not move the wallet.',
        })
    lines.sort(key=lambda line: (line['at'], str(line['id'])))
    return lines
