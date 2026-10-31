"""One driver's statement: opening balance, movements, closing balance.

WHAT THE BALANCE IS MADE OF
---------------------------
The driver's settlement balance (`Wallet`, scope driver) moves only through
`WalletTransaction` rows carrying one of `DRIVER_LEDGER_PURPOSES`:

  * `trip_earnings`  credit   an online or wallet trip: the platform owes the net fare
  * `trip_commission` debit   a cash trip: the driver owes the commission
  * `platform_funded_trip_end`  credit   an operator ended a started trip as a safety incident
                      or platform fault: the rider paid nothing and the platform pays the
                      driver the earnings (fare minus the commission that would have applied)
  * `promo_discount_platform_funded`  credit   a CASH trip with a rider promo: the driver collected
                      the fare less the discount, and the platform pays them the discount (module 12)
  * `incentive_reward` credit   a driver incentive reward: no commission, paid out with the next
                      weekly payout, its own line (tax: not configured, go-live blocker 15)
  * `referral_reward` credit    a driver referral reward: same treatment
  * `withdrawal`     debit    a payout request (taken at request time)
  * `refund_rejected_withdrawal` / `refund_failed_withdrawal`  credit   money
    returned because the payout was rejected, or failed at the gateway
  * `adjustment`     credit or debit   a manual correction by Finance through a
    maker-checker request (Part B); the entry carries its reason code and note

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
The commission is on the settlement line. The withdrawal transfer fee
(`payout_config.withdrawal_fee`) is 0 in every configured country and is taken
from the PAYOUT, not the wallet (the wallet is debited the full amount), so the
statement has no fee line; a non-zero fee is stamped on the withdrawal row
(`fee_minor`).
"""

import re

from django.db.models import BigIntegerField, Case, F, Q, Sum, Value, When
from django.db.models.functions import Coalesce

from servers.payments.scope import DRIVER_LEDGER_PURPOSES

SIGNED = Case(
    When(txn_type='credit', then=F('amount_minor')),
    When(txn_type='debit', then=-F('amount_minor')),
    default=Value(0), output_field=BigIntegerField(),
)

#: `withdrawal_12`, `refund_withdrawal_12`, `refund_withdrawal_12_r1`,
#: `refund_rejected_withdrawal_12` -> 12.
WITHDRAWAL_REF = re.compile(r'^(?:refund_rejected_withdrawal|refund_withdrawal|withdrawal)_(\d+)')

ENTRY_KINDS = {
    'adjustment': 'adjustment',
    'trip_earnings': 'settlement',
    'trip_commission': 'settlement',
    'platform_funded_trip_end': 'platform_funded_trip_end',
    'promo_discount_platform_funded': 'promo_discount_platform_funded',
    'incentive_reward': 'incentive_reward',
    'referral_reward': 'referral_reward',
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

    adjustments = _adjustment_details([t.reference_id for t in page if t.purpose == 'adjustment'])

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
        if txn.purpose in ('incentive_reward', 'referral_reward'):
            # Paid in the weekly payout, no commission; tax is go-live blocker 15, so nothing is
            # computed or withheld here and the line says so.
            entry['commission'] = 'none'
            entry['tax'] = 'not configured'
        match = WITHDRAWAL_REF.match(ref)
        if match:
            entry['withdrawal_id'] = int(match.group(1))
        elif txn.purpose == 'adjustment':
            entry['adjustment'] = adjustments.get(txn.reference_id)
        out.append(entry)
    return out


def _adjustment_details(references):
    """`{reference: {request_id, reason_code, note, ...}}` for adjustment rows."""
    from servers.approvals.models import ApprovalRequest

    ids = {}
    for ref in references:
        if ref and ref.startswith('adjustment_') and ref[11:].isdigit():
            ids[int(ref[11:])] = ref
    if not ids:
        return {}
    out = {}
    for row in ApprovalRequest.objects.filter(pk__in=list(ids)):
        payload = row.payload or {}
        out[ids[row.pk]] = {
            'approval_request_id': row.pk,
            'reason_code': payload.get('reason_code'),
            'note': payload.get('note'),
            'maker': {'label': row.maker_label, 'role': row.maker_role},
            'checker': {'label': row.checker_label, 'role': row.checker_role} if row.checker_id else None,
        }
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
