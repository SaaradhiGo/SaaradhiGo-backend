"""What a driver earned, read from the settlement ledger that already exists.

A cash-only driver saw zero earnings. Not "slightly wrong" -- zero, for every
ride they had ever completed. The cause is a read, not a write, which is why this
module only reads.

How settlement is actually recorded
-----------------------------------
`driver.utils.credit_driver_wallet` writes one `WalletTransaction` per completed
trip, and the direction depends on who is holding the money:

* **online / wallet trip** -- the platform collected the fare and owes the driver
  the net, so the row is a **credit** of `fare - commission`,
  `purpose='trip_earnings'`.
* **cash trip** -- the driver already has the whole fare in hand and owes the
  platform its cut, so the row is a **debit** of the commission,
  `purpose='trip_commission'`.

Both are settlement. Reading only `txn_type='credit'` therefore drops every cash
trip, which is the ₹0 defect exactly.

Two things follow, and they are why the totals move even for online drivers.
The credit row holds the **net**, not the fare. The old summary summed those
credits into `total_earned` and then subtracted a hardcoded 20% from it, so an
online driver's commission was deducted twice: once by settlement and once by
the report. And the driver app itself computes `netEarned = totalEarned -
totalCommission`, so `total_earned` has to mean the **gross fare** for that
subtraction to be right.

Where each number comes from
----------------------------
* **gross** -- the trip's own fare. This is the one number not in the ledger.
* **commission** -- derived from the ledger row, never recomputed from today's
  rate card. Re-deriving it would mean a rate card edited in March silently
  rewrites what a driver was told they earned in January.
* **net** -- gross minus commission, the same definition for cash and online, so
  the two are comparable in one list.

This module performs no writes. It cannot alter settlement history, a wallet
balance or a commission, and it is deliberately not able to: fixing the report is
separable from fixing the ledger, and the ledger's own gaps are written up in the
driver-earnings report rather than patched here.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)

# The two purposes `credit_driver_wallet` writes. Anything else in the driver's
# wallet -- withdrawals, payouts, refunds of rejected withdrawals -- is movement
# of already-earned money and must not be counted as a trip.
PURPOSE_ONLINE = 'trip_earnings'
PURPOSE_CASH = 'trip_commission'
SETTLEMENT_PURPOSES = (PURPOSE_ONLINE, PURPOSE_CASH)

_REFERENCE_PREFIX = 'TRIP_'


@dataclass
class EarningRow:
    """One trip's settlement, as the driver should see it."""

    ledger_id: int
    trip_id: Optional[int]
    gross: Optional[Decimal]
    commission: Optional[Decimal]
    net: Optional[Decimal]
    method: str
    created_at: Optional[datetime]
    rider_name: str
    # True when the driver physically holds the fare and owes the commission.
    driver_holds_cash: bool

    def to_api(self):
        """The shape the driver app already parses, with nothing removed."""
        return {
            'id': self.ledger_id,
            'trip_id_val': self.trip_id,
            # `amount` is the fare for the trip. The app falls back to
            # `net_amount` when absent, so both are always sent.
            'amount': _s(self.gross if self.gross is not None else self.net),
            'commission': _s(self.commission),
            'net_amount': _s(self.net),
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'method': self.method,
            'user_name': self.rider_name,
            'cash_in_hand': self.driver_holds_cash,
        }


def _s(value):
    return None if value is None else str(value)


def _trip_id_from_reference(reference_id):
    """`TRIP_1234` -> 1234. Returns None for anything else.

    The reference is written by `credit_driver_wallet` and is the only link from
    a wallet row back to a trip -- `WalletTransaction` has no trip FK. That gap
    is a real modelling weakness and is recorded in the report; parsing is what
    is available without changing settlement.
    """
    if not reference_id or not str(reference_id).startswith(_REFERENCE_PREFIX):
        return None
    try:
        return int(str(reference_id)[len(_REFERENCE_PREFIX):])
    except ValueError:
        return None


def settlement_rows(driver, since=None, until=None, limit=None):
    """Every settled trip for this driver, newest first.

    Two queries regardless of how many trips: the ledger rows, then the trips
    they reference.
    """
    from servers.ride.models import Trip
    from servers.rider.models import WalletTransaction

    qs = (
        WalletTransaction.objects
        .filter(user_id=driver.user_id,
                purpose__in=SETTLEMENT_PURPOSES,
                status='completed')
        .order_by('-created_at', '-id')
    )
    if since is not None:
        qs = qs.filter(created_at__gte=since)
    if until is not None:
        qs = qs.filter(created_at__lt=until)
    if limit is not None:
        qs = qs[:limit]

    ledger = list(qs)
    trip_ids = {
        tid for tid in (_trip_id_from_reference(r.reference_id) for r in ledger)
        if tid is not None
    }
    trips = {
        t.id: t for t in Trip.objects
        .filter(id__in=trip_ids)
        .select_related('user_id')
    }

    rows = []
    for entry in ledger:
        trip_id = _trip_id_from_reference(entry.reference_id)
        trip = trips.get(trip_id)
        rows.append(_row_for(entry, trip, trip_id))
    return rows


def _row_for(entry, trip, trip_id):
    amount = Decimal(str(entry.amount or '0.00'))
    is_cash = entry.purpose == PURPOSE_CASH

    # `final_fare` is never written by the current completion path, so the fare
    # in practice is the estimate. That is a known gap in the chain and is why
    # fare finalisation exists as its own piece of work -- it is surfaced here
    # rather than papered over.
    gross = None
    if trip is not None:
        gross = trip.final_fare if trip.final_fare is not None else trip.estimated_fare
        gross = Decimal(str(gross)) if gross is not None else None

    if is_cash:
        commission = amount
        net = (gross - commission) if gross is not None else None
    else:
        net = amount
        commission = (gross - net) if gross is not None else None

    method = (trip.payment_method if trip is not None and trip.payment_method
              else ('cash' if is_cash else 'online'))

    rider_name = ''
    if trip is not None and trip.user_id is not None:
        rider_name = trip.user_id.full_name or trip.user_id.phone_number or ''

    return EarningRow(
        ledger_id=entry.id,
        trip_id=trip_id,
        gross=gross,
        commission=commission,
        net=net,
        method=method,
        created_at=entry.created_at,
        rider_name=rider_name,
        driver_holds_cash=is_cash,
    )


def summarise(rows):
    """Totals over settlement rows.

    `total_gross` is the fare the driver's trips were worth, `total_commission`
    what the platform actually charged, and `total_net` the difference. Reporting
    all three means nobody has to guess which one a single "earnings" number
    meant.
    """
    totals = {
        'total_gross': Decimal('0.00'),
        'total_commission': Decimal('0.00'),
        'total_net': Decimal('0.00'),
        'cash_collected': Decimal('0.00'),
        'total_trips': 0,
        # Trips whose fare could not be resolved. Counted rather than dropped:
        # a driver noticing a missing trip trusts the number less than a driver
        # seeing that one trip is unresolved.
        'unresolved_trips': 0,
    }
    for row in rows:
        totals['total_trips'] += 1
        if row.gross is None:
            totals['unresolved_trips'] += 1
            # A row with no resolvable fare still contributes what it does know.
            if row.driver_holds_cash and row.commission is not None:
                totals['total_commission'] += row.commission
            elif row.net is not None:
                totals['total_net'] += row.net
            continue

        totals['total_gross'] += row.gross
        if row.commission is not None:
            totals['total_commission'] += row.commission
        if row.net is not None:
            totals['total_net'] += row.net
        if row.driver_holds_cash:
            totals['cash_collected'] += row.gross

    if totals['total_gross'] > 0:
        totals['effective_commission_percent'] = (
            totals['total_commission'] / totals['total_gross'] * Decimal('100')
        ).quantize(Decimal('0.01'))
    else:
        # No gross means no rate to report. Zero is the honest answer here
        # precisely because a hardcoded 20 was the old bug.
        totals['effective_commission_percent'] = Decimal('0.00')
    return totals
