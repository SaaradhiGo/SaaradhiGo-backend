"""Aggregate money metrics per city: the ONE service function behind the Command
Centre finance tiles (and, later, the Analyst's Reports module).

WHO MAY SEE THEM
----------------
`finance.aggregate` ("See aggregate money metrics, no individual earnings"),
held by City Ops, Analyst, Finance and Super Admin. It is NOT finance.read: the
holder gets city totals, never a driver's earnings, a payout row, a ledger or
any other /payments/admin/ endpoint (those stay finance.read, so City Ops and
Analyst keep 403 on them). `cities_with_aggregate` narrows a list of cities to
the places the capability is held, so Finance in Hyderabad plus Support in
Austin sees Hyderabad's tiles only.

WHAT THEY ARE (per city, per currency; never summed across currencies)
-----------------------------------------------------------------------
  gmv            gross fare of trips settled in the window (TripSettlement,
                 current version of each trip)
  commission     the platform's commission on those settlements
  take_rate      commission / gross, percent to one decimal, None with no gross
  payouts        pending withdrawals right now: count and amount
  reconciliation open reconciliation findings anchored in the city (a count)

All of it is the same anchor the scoped lists use (the trip's zone city for
settlements, the driver's home city for withdrawals, the item's anchor node
for findings), computed in a handful of conditional aggregates for every city
at once.
"""

from collections import OrderedDict
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Count, Exists, OuterRef, Q, Sum
from django.utils import timezone

from base.money import normalize_currency
from servers.command_centre.overview import (
    _aggregate, _by_currency, between, city_contexts, window_bounds, withdrawal_anchor,
)


def cities_with_aggregate(user, cities):
    """The subset of `cities` (GeoNodes) where the caller holds finance.aggregate."""
    from .scope import paths_for

    paths = paths_for(user, 'finance.aggregate')
    if paths is None:
        return list(cities)
    return [c for c in cities if any(c.path.startswith(p) for p in paths)]


def _percent(numerator, denominator):
    if not denominator:
        return None
    return float((Decimal(numerator) * 100 / Decimal(denominator))
                 .quantize(Decimal('0.1'), rounding=ROUND_HALF_UP))


def _current_settlements():
    """Settlements that are the latest version of their trip's settlement."""
    from .models import TripSettlement

    newer = TripSettlement.objects.filter(trip=OuterRef('trip'), version__gt=OuterRef('version'))
    return TripSettlement.objects.annotate(_superseded=Exists(newer)).filter(_superseded=False)


def finance_aggregates(cities, window='today', now=None):
    """`OrderedDict {city_code: tile dict}` for `cities` (already permitted).

    The caller has decided who may see which city (`cities_with_aggregate`);
    this only measures.
    """
    from .models import ReconciliationItem
    from servers.driver.models import WithdrawalRequest

    now = now or timezone.now()
    contexts = city_contexts(cities)
    bounds = {cid: window_bounds(window, now, ctx.tz) for cid, ctx in contexts.items()}

    def settled(ctx):
        return (Q(trip__zone__geo_node__path__startswith=ctx.path)
                & between('settled_at', bounds[ctx.node.id]))

    money = _by_currency(_current_settlements(), contexts, lambda ctx: {
        'gross': Sum('gross_fare_minor', filter=settled(ctx)),
        'commission': Sum('commission_amount_minor', filter=settled(ctx)),
    })

    pending = Q(status='pending')
    payout_counts = _aggregate(WithdrawalRequest.objects.all(), contexts, lambda ctx: {
        'pending': Count('id', filter=withdrawal_anchor(ctx) & pending),
    })
    payout_sums = _by_currency(WithdrawalRequest.objects.all(), contexts, lambda ctx: {
        'pending_sum': Sum('amount_minor', filter=withdrawal_anchor(ctx) & pending),
    })

    recon = _aggregate(ReconciliationItem.objects.all(), contexts, lambda ctx: {
        'open': Count('id', filter=Q(anchor_node__path__startswith=ctx.path,
                                     resolved_at__isnull=True)),
    })

    out = OrderedDict()
    for cid, ctx in contexts.items():
        m = money.get(cid, {})
        gross, commission = m.get('gross', {}), m.get('commission', {})
        currencies = sorted(set(gross) | set(commission) | {normalize_currency(ctx.currency)})
        out[ctx.node.code] = {
            'code': ctx.node.code,
            'name': ctx.node.name,
            'country': ctx.country,
            'currency': ctx.currency,
            'window': window,
            'timezone': str(ctx.tz),
            'gmv': {c: gross.get(c, 0) for c in currencies},
            'commission': {c: commission.get(c, 0) for c in currencies},
            'take_rate_percent': {c: _percent(commission.get(c, 0), gross.get(c, 0))
                                  for c in currencies},
            'payouts_pending': {
                'count': payout_counts.get(cid, {}).get('pending', 0),
                'amount': payout_sums.get(cid, {}).get('pending_sum', {}),
            },
            'reconciliation': {'open_findings': recon.get(cid, {}).get('open', 0)},
        }
    return out
