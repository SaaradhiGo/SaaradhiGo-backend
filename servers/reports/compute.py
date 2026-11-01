"""Rebuilding the rollups: `rebuild_day(city, date)`.

READ-ONLY OVER BUSINESS DATA
----------------------------
This module only SELECTs from trips, settlements, ledgers, sessions, tickets, incidents and
flags. Its only writes are to the derived tables in `models.py`. It never re-derives an amount:

  GMV, commission, driver net   `TripSettlement` (the CURRENT version of each trip, by
                                `settled_at`), the same rows `finance.aggregate` reads
                                (`payments.aggregates._current_settlements`). Commission is what
                                was applied at settlement, never today's rate card.
                                US pass-through (AUDIT 27.62 item 1): a US settlement's gross is the fare BEFORE
                                tax and fees, so GMV, commission and driver net exclude them (and agree with
                                each other); the tax and fees collected are on the settlement rows
                                (`tax_collected_minor`, `regulatory_fee_collected_minor`) and in the Finance
                                overview. Not rolled up here (no new column).
  rider paid                    `charged_fare()` of the trips completed that day (the Command
                                Centre's "collected": the discounted fare when a promo applied).
  promo cost                    `promotions.LedgerEntry`, kind `promo_discount_platform_funded`.
  platform-funded trip end,     the wallet ledger (`WalletTransaction.purpose`): a credit of kind
  incentive / referral reward,  `platform_funded_trip_end`, `incentive_reward`, `referral_reward`,
  reward recovery               a debit of kind `reward_recovery`.
  regulatory fee expense        `PlatformExpense` kind `platform_regulatory_fee_expense`.
  refunds                       `TripMoneyAction` kind `refund`.

WHERE A ROW LANDS
-----------------
Trip-linked money lands in the city of the trip's pickup zone (the Finance rule). A wallet row
with no trip follows its owner: a driver's home city, a rider's home city or, when the rider has
none, the city of their first trip (`rider_city_of`). A row that cannot be placed is in no city.
Amounts keep their own currency; nothing here converts or adds currencies.

THE DAY
-------
Local days in the city's time zone (`command_centre.overview.city_contexts`).

IDEMPOTENT
----------
`rebuild_day` deletes the (city, date) rows and inserts them again in one transaction. Running it
twice, or while a trip settles, ends in the same rows for the same source data.
"""

import logging
import re
from collections import defaultdict
from datetime import date as date_cls, datetime, time, timedelta, timezone as dt_timezone
from decimal import Decimal

from django.db import transaction
from django.db.models import (
    Avg, CharField, Count, DurationField, ExpressionWrapper, F, Q, Sum, Value,
)
from django.db.models.functions import Coalesce
from django.utils import timezone

from base.money import charged_fare, normalize_currency

from .models import (
    DailyCityMetrics, DailyDriverSupply, DailyFraud, DailyPayouts, DailyRiderMetrics, DailySafety,
    DailySupport, RebuildLog, WeeklyRiderCohort,
)

logger = logging.getLogger(__name__)

UNKNOWN_VEHICLE = 'unknown'
#: Rollup rows that cannot be attributed to a vehicle class.
NO_VEHICLE = ''
REWARD_PURPOSES = {
    'incentive_reward': 'incentive_rewards_minor',
    'referral_reward': 'referral_rewards_minor',
}
PLATFORM_FUNDED_PURPOSE = 'platform_funded_trip_end'
RECOVERY_PURPOSE = 'reward_recovery'
DISCOUNT_KIND = 'promo_discount_platform_funded'
COHORT_WEEKS = 12

_TRIP_REF = re.compile(r'^TRIP_(\d+)$')

MONEY_COLUMNS = (
    'gmv_minor', 'rider_paid_minor', 'commission_minor', 'driver_net_minor', 'promo_cost_minor',
    'platform_funded_trip_end_minor', 'refunds_minor', 'incentive_rewards_minor',
    'referral_rewards_minor', 'reward_recovery_minor', 'regulatory_fee_expense_minor',
)
COUNT_COLUMNS = (
    'trips_requested', 'trips_accepted', 'trips_completed', 'cancelled_by_rider',
    'cancelled_by_driver', 'cancelled_by_ops', 'no_driver_found', 'settled_trips',
)


# ---------------------------------------------------------------------------
# Cities and days
# ---------------------------------------------------------------------------

def city_context(city):
    from servers.command_centre.overview import city_contexts

    return city_contexts([city])[city.id]


def day_bounds(day, tz):
    """UTC-aware `[start, end)` of the local date `day` in `tz`."""
    start = datetime.combine(day, time.min, tzinfo=tz).astimezone(dt_timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz).astimezone(dt_timezone.utc)
    return start, end


def local_today(city, now=None):
    return (now or timezone.now()).astimezone(city_context(city).tz).date()


def active_cities():
    """Every city node: a rollup is built for all of them (a paused city can still have data)."""
    from servers.admin_access.models import CITY, GeoNode

    return list(GeoNode.objects.filter(kind=CITY).order_by('path'))


def _vehicle(prefix=''):
    """The vehicle class of a trip: the requested class, else the class of the vehicle that drove
    it (the frozen executive_revenue rule), else 'unknown'."""
    return Coalesce(
        F(f'{prefix}requested_vehicle_type__type'),
        F(f'{prefix}vehicle_id__vehicle_type_id__type'),
        Value(UNKNOWN_VEHICLE), output_field=CharField())


def _cur(value, fallback):
    return normalize_currency(value) if value else fallback


# ---------------------------------------------------------------------------
# Percentiles (small lists, in Python: the median of a day is not an aggregate)
# ---------------------------------------------------------------------------

def percentile(values, pct):
    """Nearest-rank percentile of `values` (0 < pct <= 100); None on an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * pct // 100))   # ceil
    return int(ordered[int(rank) - 1])


def median(values):
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return int(ordered[mid])
    return int((ordered[mid - 1] + ordered[mid]) // 2)


# ---------------------------------------------------------------------------
# Riders: a rider's city
# ---------------------------------------------------------------------------

def rider_city_of(user_ids):
    """`{user_id: city GeoNode id}`: the rider's home city, else the city of their first trip.
    A rider with neither is absent (placed nowhere)."""
    from servers.admin_access.models import CITY, GeoNode
    from servers.ride.models import Trip
    from servers.rider.models import Rider

    user_ids = list(set(user_ids))
    out = {}
    if not user_ids:
        return out
    for uid, home in Rider.objects.filter(user_id__in=user_ids, home_city__isnull=False).values_list(
            'user_id_id', 'home_city_id'):
        out[uid] = home
    rest = [u for u in user_ids if u not in out]
    if rest:
        city_ids = {n.path: n.id for n in GeoNode.objects.filter(kind=CITY)}
        firsts = {}
        for uid, path in (Trip.objects.filter(user_id__in=rest, zone__isnull=False)
                          .order_by('requested_at', 'id')
                          .values_list('user_id', 'zone__geo_node__path')):
            firsts.setdefault(uid, path)
        for uid, path in firsts.items():
            for city_path, node_id in city_ids.items():
                if path and path.startswith(city_path):
                    out[uid] = node_id
                    break
    return out


# ---------------------------------------------------------------------------
# The trip and money rollup
# ---------------------------------------------------------------------------

class _Row(dict):
    """One (vehicle_type, currency) accumulator with every column at zero."""

    def __init__(self):
        super().__init__({c: 0 for c in COUNT_COLUMNS + MONEY_COLUMNS})
        self['distance_km'] = Decimal('0')
        self['duration_min'] = Decimal('0')
        self['eta_count'] = 0
        self['eta_seconds_sum'] = 0
        self['eta_p50_seconds'] = None
        self['eta_p90_seconds'] = None


def _trip_anchor(city):
    return Q(zone__geo_node__path__startswith=city.path)


def _requests(city, start, end, rows, default_currency):
    """Counts of the trips REQUESTED in the window, and their pickup ETA."""
    from servers.ride.models import Trip

    window = Q(requested_at__gte=start, requested_at__lt=end)
    qs = (Trip.objects.filter(_trip_anchor(city) & window)
          .annotate(_vt=_vehicle())
          .values('_vt', 'currency').order_by()
          .annotate(
              requested=Count('id'),
              accepted=Count('id', filter=Q(accepted_at__isnull=False)),
              by_rider=Count('id', filter=Q(status_id__status_code='cancelled', cancelled_by='rider')),
              by_driver=Count('id', filter=Q(status_id__status_code='cancelled', cancelled_by='driver')),
              no_driver=Count('id', filter=Q(status_id__status_code='cancelled', accepted_at__isnull=True)
                              & ~Q(cancelled_by__in=('rider', 'driver', 'admin'))),
              by_ops=Count('id', filter=Q(status_id__status_code='cancelled')
                           & (Q(cancelled_by='admin')
                              | (Q(accepted_at__isnull=False) & ~Q(cancelled_by__in=('rider', 'driver'))))),
          ))
    for r in qs:
        row = rows[(r['_vt'], _cur(r['currency'], default_currency))]
        row['trips_requested'] += r['requested']
        row['trips_accepted'] += r['accepted']
        row['cancelled_by_rider'] += r['by_rider']
        row['cancelled_by_driver'] += r['by_driver']
        row['cancelled_by_ops'] += r['by_ops']
        row['no_driver_found'] += r['no_driver']

    # Pickup ETA: reached - accepted, per trip, so the day's median and p90 are real.
    etas = defaultdict(list)
    eta = ExpressionWrapper(F('reached_at') - F('accepted_at'), output_field=DurationField())
    for vt, cur, delta in (Trip.objects.filter(
            _trip_anchor(city) & window & Q(accepted_at__isnull=False, reached_at__isnull=False))
            .annotate(_vt=_vehicle(), _eta=eta).values_list('_vt', 'currency', '_eta')):
        seconds = max(0, int(delta.total_seconds()))
        etas[(vt, _cur(cur, default_currency))].append(seconds)
    for key, values in etas.items():
        row = rows[key]
        row['eta_count'] = len(values)
        row['eta_seconds_sum'] = sum(values)
        row['eta_p50_seconds'] = percentile(values, 50)
        row['eta_p90_seconds'] = percentile(values, 90)


def _completions(city, start, end, rows, default_currency):
    """Trips COMPLETED in the window: count, distance, time and what the rider paid."""
    from servers.ride.models import Trip

    qs = (Trip.objects.filter(_trip_anchor(city), status_id__status_code='completed',
                              completed_at__gte=start, completed_at__lt=end)
          .annotate(_vt=_vehicle()).values('_vt', 'currency').order_by()
          .annotate(n=Count('id'), km=Sum('actual_distance_km'), mins=Sum('actual_duration_min'),
                    paid=Sum(charged_fare())))
    for r in qs:
        row = rows[(r['_vt'], _cur(r['currency'], default_currency))]
        row['trips_completed'] += r['n']
        row['distance_km'] += Decimal(r['km'] or 0)
        row['duration_min'] += Decimal(r['mins'] or 0)
        row['rider_paid_minor'] += int(r['paid'] or 0)


def _settlements(city, start, end, rows, default_currency):
    from servers.payments.aggregates import _current_settlements

    qs = (_current_settlements()
          .filter(trip__zone__geo_node__path__startswith=city.path,
                  settled_at__gte=start, settled_at__lt=end)
          .annotate(_vt=_vehicle('trip__')).values('_vt', 'currency').order_by()
          .annotate(n=Count('id'), gross=Sum('gross_fare_minor'),
                    commission=Sum('commission_amount_minor'), net=Sum('driver_net_minor')))
    for r in qs:
        row = rows[(r['_vt'], _cur(r['currency'], default_currency))]
        row['settled_trips'] += r['n']
        row['gmv_minor'] += int(r['gross'] or 0)
        row['commission_minor'] += int(r['commission'] or 0)
        row['driver_net_minor'] += int(r['net'] or 0)


def _promo_and_refunds(city, start, end, rows, default_currency):
    from servers.promotions.models import LedgerEntry
    from servers.ride.models import TripMoneyAction

    anchor = {'trip__zone__geo_node__path__startswith': city.path}
    for r in (LedgerEntry.objects.filter(kind=DISCOUNT_KIND, created_at__gte=start,
                                         created_at__lt=end, **anchor)
              .annotate(_vt=_vehicle('trip__')).values('_vt', 'currency').order_by()
              .annotate(total=Sum('amount_minor'))):
        rows[(r['_vt'], _cur(r['currency'], default_currency))]['promo_cost_minor'] += int(r['total'] or 0)
    for r in (TripMoneyAction.objects.filter(kind=TripMoneyAction.REFUND, created_at__gte=start,
                                             created_at__lt=end, **anchor)
              .annotate(_vt=_vehicle('trip__')).values('_vt', 'currency').order_by()
              .annotate(total=Sum('amount_minor'))):
        rows[(r['_vt'], _cur(r['currency'], default_currency))]['refunds_minor'] += int(r['total'] or 0)


def _trip_ids_in_city(city, trip_ids):
    """`{trip_id: vehicle class}` for the given trips that sit in `city`."""
    from servers.ride.models import Trip

    if not trip_ids:
        return {}
    return {tid: vt for tid, vt in (
        Trip.objects.filter(id__in=trip_ids, zone__geo_node__path__startswith=city.path)
        .annotate(_vt=_vehicle()).values_list('id', '_vt'))}


def _wallet_and_expenses(city, start, end, rows, default_currency):
    """Wallet-ledger kinds and the regulatory-fee expense."""
    from servers.payments.models import PlatformExpense
    from servers.rider.models import WalletTransaction

    window = Q(created_at__gte=start, created_at__lt=end, status='completed')

    # Platform-funded trip end: a driver credit tied to the trip by reference_id (TRIP_<id>).
    funded = list(WalletTransaction.objects.filter(
        window, purpose=PLATFORM_FUNDED_PURPOSE, txn_type='credit')
        .values_list('reference_id', 'currency', 'amount_minor'))
    refs = {}
    for ref, _cur_code, _amt in funded:
        m = _TRIP_REF.match(ref or '')
        if m:
            refs[int(m.group(1))] = None
    in_city = _trip_ids_in_city(city, list(refs))
    for ref, cur, amount in funded:
        m = _TRIP_REF.match(ref or '')
        if m and int(m.group(1)) in in_city:
            rows[(in_city[int(m.group(1))], _cur(cur, default_currency))][
                'platform_funded_trip_end_minor'] += int(amount)

    # Regulatory fee expense: one row per trip.
    fees = list(PlatformExpense.objects.filter(
        kind=PlatformExpense.REGULATORY_FEE, created_at__gte=start, created_at__lt=end,
        trip_id__isnull=False).values_list('trip_id', 'currency', 'amount_minor'))
    in_city = _trip_ids_in_city(city, [t for t, _c, _a in fees])
    for trip_id, cur, amount in fees:
        if trip_id in in_city:
            rows[(in_city[trip_id], _cur(cur, default_currency))][
                'regulatory_fee_expense_minor'] += int(amount)

    # Rewards and their recovery: not tied to a vehicle class. A driver's home city, else the
    # rider's city.
    driver_q = Q(user_id__driver__home_city__path__startswith=city.path)
    reward_q = window & Q(purpose__in=list(REWARD_PURPOSES) + [RECOVERY_PURPOSE])
    for r in (WalletTransaction.objects.filter(reward_q & driver_q).values('purpose', 'currency')
              .order_by().annotate(total=Sum('amount_minor'))):
        _add_reward(rows, r['purpose'], r['currency'], r['total'], default_currency)
    rider_rows = list(WalletTransaction.objects.filter(
        reward_q, user_id__driver__isnull=True).values_list('user_id', 'purpose', 'currency', 'amount_minor'))
    if rider_rows:
        where = rider_city_of([u for u, _p, _c, _a in rider_rows])
        for uid, purpose, cur, amount in rider_rows:
            if where.get(uid) == city.id:
                _add_reward(rows, purpose, cur, amount, default_currency)
    # A driver with no home city, or a user with neither profile, is placed nowhere.


def _add_reward(rows, purpose, currency, amount, default_currency):
    column = REWARD_PURPOSES.get(purpose) or 'reward_recovery_minor'
    rows[(NO_VEHICLE, _cur(currency, default_currency))][column] += int(amount or 0)


def _payouts(city, start, end, tz):
    from servers.driver.models import WithdrawalRequest
    from servers.payments.models import TransactionHistory

    anchor_w = Q(driver__home_city__path__startswith=city.path)
    acc = defaultdict(lambda: defaultdict(int))
    for r in (WithdrawalRequest.objects.filter(anchor_w, requested_at__gte=start, requested_at__lt=end)
              .values('currency').order_by()
              .annotate(n=Count('id'), total=Sum('amount_minor'),
                        failed_n=Count('id', filter=Q(status='failed')),
                        failed=Sum('amount_minor', filter=Q(status='failed')))):
        c = normalize_currency(r['currency'])
        acc[c]['requested_count'] += r['n']
        acc[c]['requested_minor'] += int(r['total'] or 0)
        acc[c]['failed_count'] += r['failed_n']
        acc[c]['failed_minor'] += int(r['failed'] or 0)
    for r in (TransactionHistory.objects.filter(
            driver_id__home_city__path__startswith=city.path, withdrawal_request__isnull=False,
            status='completed', created_at__gte=start, created_at__lt=end)
            .values('currency').order_by().annotate(n=Count('id'), total=Sum('amount_minor'))):
        c = normalize_currency(r['currency'])
        acc[c]['paid_count'] += r['n']
        acc[c]['paid_minor'] += int(r['total'] or 0)
    return acc


# ---------------------------------------------------------------------------
# Supply, riders, safety, support, fraud
# ---------------------------------------------------------------------------

def _supply(city, day, start, end):
    from servers.driver.models import Driver, DriverSession
    from servers.ride.models import Trip

    home = Q(home_city__path__startswith=city.path)
    active = (Trip.objects.filter(status_id__status_code='completed', completed_at__gte=start,
                                  completed_at__lt=end, driver_id__home_city__path__startswith=city.path)
              .values('driver_id').distinct().count())
    sessions = (DriverSession.objects.filter(driver__home_city__path__startswith=city.path,
                                             started_at__lt=end)
                .filter(Q(ended_at__isnull=True) | Q(ended_at__gt=start))
                .values_list('driver_id', 'started_at', 'ended_at'))
    now = timezone.now()
    seconds, online = 0, set()
    for driver_id, s, e in sessions:
        # An open session counts up to now (never into the future), clipped to this day.
        lo, hi = max(s, start), min(e or now, end)
        if hi > lo:
            seconds += int((hi - lo).total_seconds())
            online.add(driver_id)
    approved = Driver.objects.filter(home, approved=True, doc_status_updated_at__gte=start,
                                     doc_status_updated_at__lt=end).count()
    return {'active_drivers': active, 'online_drivers': len(online), 'online_seconds': seconds,
            'new_approved_drivers': approved}


def _riders(city, day, start, end):
    from servers.ride.models import Trip
    from servers.rider.models import Rider

    anchor = _trip_anchor(city)
    active = (Trip.objects.filter(anchor, requested_at__gte=start, requested_at__lt=end)
              .values('user_id').distinct().count())
    completed_today = list(Trip.objects.filter(
        anchor, status_id__status_code='completed', completed_at__gte=start, completed_at__lt=end)
        .values_list('user_id', flat=True).distinct())
    earlier = set(Trip.objects.filter(
        user_id__in=completed_today, status_id__status_code='completed', completed_at__lt=start)
        .values_list('user_id', flat=True).distinct())
    first_trip = len([u for u in completed_today if u not in earlier])
    signed_up = list(Rider.objects.filter(created_at__gte=start, created_at__lt=end)
                     .values_list('user_id_id', flat=True))
    where = rider_city_of(signed_up)
    new = len([u for u in signed_up if where.get(u) == city.id])
    return {'new_riders': new, 'active_riders': active, 'first_trip_riders': first_trip}


def _safety(city, start, end):
    from servers.safety.models import SafetyIncident
    from servers.sos.models import SOSEvent, SOSEventUpdate

    out = []
    for r in (SafetyIncident.objects.filter(anchor_node__path__startswith=city.path,
                                            opened_at__gte=start, opened_at__lt=end)
              .values('category', 'severity').order_by().annotate(n=Count('id'))):
        out.append(dict(source=DailySafety.SOURCE_INCIDENT, kind=r['category'],
                        severity=r['severity'], count=r['n']))

    anchor = (Q(anchor_node__path__startswith=city.path)
              | Q(anchor_node__isnull=True, trip__zone__geo_node__path__startswith=city.path))
    events = list(SOSEvent.objects.filter(anchor, created_at__gte=start, created_at__lt=end)
                  .values_list('id', 'event_type', 'created_at'))
    if events:
        first_ack = {}
        for event_id, at in (SOSEventUpdate.objects.filter(
                event_id__in=[e[0] for e in events], new_status='acknowledged')
                .order_by('created_at').values_list('event_id', 'created_at')):
            first_ack.setdefault(event_id, at)
        by_type, durations = defaultdict(int), []
        for event_id, kind, created in events:
            by_type[kind] += 1
            if event_id in first_ack:
                durations.append(max(0, int((first_ack[event_id] - created).total_seconds())))
        for kind, n in sorted(by_type.items()):
            out.append(dict(source=DailySafety.SOURCE_SOS, kind=kind, severity='', count=n))
        out.append(dict(source=DailySafety.SOURCE_SOS, kind='*', severity='', count=len(events),
                        responded_count=len(durations), response_seconds_sum=sum(durations),
                        response_median_seconds=median(durations)))
    return out


def _support(city, start, end, now):
    from servers.support.models import SupportTicket

    anchor = (Q(trip_id__zone__geo_node__path__startswith=city.path)
              | Q(trip_id__isnull=True, user_id__driver__home_city__path__startswith=city.path))
    base = SupportTicket.objects.filter(anchor)
    opened = base.filter(created_at__gte=start, created_at__lt=end)
    first_late = (Q(first_response_due_at__isnull=False, first_responded_at__gt=F('first_response_due_at'))
                  | Q(first_responded_at__isnull=True, first_response_due_at__lt=now))
    res_late = (Q(resolution_due_at__isnull=False, resolved_at__gt=F('resolution_due_at'))
                | Q(resolved_at__isnull=True, resolution_due_at__lt=now, waiting_since__isnull=True))
    agg = opened.aggregate(n=Count('id'), fr=Count('id', filter=first_late),
                           rs=Count('id', filter=res_late))
    fr_seconds = [int((r - c).total_seconds()) for c, r in opened.filter(
        first_responded_at__isnull=False).values_list('created_at', 'first_responded_at')]
    resolved = base.filter(resolved_at__gte=start, resolved_at__lt=end).count()
    return {'opened': agg['n'], 'resolved': resolved, 'first_response_breached': agg['fr'],
            'resolution_breached': agg['rs'], 'first_response_count': len(fr_seconds),
            'first_response_seconds_sum': sum(max(0, s) for s in fr_seconds)}


def _fraud(city, start, end):
    from servers.fraud.models import CONFIRMED, DISMISSED, RiskFlag

    acc = defaultdict(lambda: {'raised': 0, 'confirmed': 0, 'dismissed': 0})
    base = RiskFlag.objects.filter(anchor_node__path__startswith=city.path)
    for r in (base.filter(created_at__gte=start, created_at__lt=end).values('kind').order_by()
              .annotate(n=Count('id'))):
        acc[r['kind']]['raised'] += r['n']
    for r in (base.filter(decided_at__gte=start, decided_at__lt=end, status__in=(CONFIRMED, DISMISSED))
              .values('kind', 'status').order_by().annotate(n=Count('id'))):
        acc[r['kind']]['confirmed' if r['status'] == CONFIRMED else 'dismissed'] += r['n']
    return acc


# ---------------------------------------------------------------------------
# rebuild_day
# ---------------------------------------------------------------------------

def rebuild_day(city, day, *, trigger='', now=None):
    """Recompute every rollup of `city` for the local date `day`. Idempotent; returns a summary.

    The reads happen first and the delete + insert in one transaction, so a reader never sees
    half a day.
    """
    now = now or timezone.now()
    ctx = city_context(city)
    start, end = day_bounds(day, ctx.tz)
    currency = normalize_currency(ctx.currency)

    rows = defaultdict(_Row)
    _requests(city, start, end, rows, currency)
    _completions(city, start, end, rows, currency)
    _settlements(city, start, end, rows, currency)
    _promo_and_refunds(city, start, end, rows, currency)
    _wallet_and_expenses(city, start, end, rows, currency)
    payouts = _payouts(city, start, end, ctx.tz)
    supply = _supply(city, day, start, end)
    riders = _riders(city, day, start, end)
    safety = _safety(city, start, end)
    support = _support(city, start, end, now)
    fraud = _fraud(city, start, end)

    with transaction.atomic():
        for model in (DailyCityMetrics, DailyPayouts, DailyDriverSupply, DailyRiderMetrics,
                      DailySafety, DailySupport, DailyFraud):
            model.objects.filter(city=city, date=day).delete()
        DailyCityMetrics.objects.bulk_create([
            DailyCityMetrics(city=city, date=day, vehicle_type=vt, currency=cur, built_at=now, **values)
            for (vt, cur), values in sorted(rows.items()) if any(
                v for k, v in values.items() if k not in ('eta_p50_seconds', 'eta_p90_seconds'))
        ])
        DailyPayouts.objects.bulk_create([
            DailyPayouts(city=city, date=day, currency=cur, built_at=now, **values)
            for cur, values in sorted(payouts.items())])
        if any(supply.values()):
            DailyDriverSupply.objects.create(city=city, date=day, built_at=now, **supply)
        if any(riders.values()):
            DailyRiderMetrics.objects.create(city=city, date=day, built_at=now, **riders)
        DailySafety.objects.bulk_create([DailySafety(city=city, date=day, built_at=now, **r)
                                         for r in safety])
        if any(support.values()):
            DailySupport.objects.create(city=city, date=day, built_at=now, **support)
        DailyFraud.objects.bulk_create([DailyFraud(city=city, date=day, kind=k, built_at=now, **v)
                                        for k, v in sorted(fraud.items())])
        RebuildLog.objects.update_or_create(
            city=city, date=day, defaults={'built_at': now, 'trigger': trigger[:24]})
    return {'city': city.code, 'date': day.isoformat(), 'metric_rows': len(rows),
            'timezone': str(ctx.tz)}


def monday_of(day):
    return day - timedelta(days=day.weekday())


def rebuild_cohorts(city, *, weeks=COHORT_WEEKS, today=None, now=None):
    """Weekly first-trip cohorts of `city` for the last `weeks` weeks, with their retention.

    A rider's cohort is the week (Monday, city-local) of their first ever completed trip, counted
    when that trip was in this city. A rider is retained in week N when they completed a trip
    anywhere N weeks after the cohort week (week 0 is the cohort week itself, so it is the whole
    cohort). Derived from trips each time; the table is replaced for the weeks it covers.
    """
    from servers.ride.models import Trip

    now = now or timezone.now()
    ctx = city_context(city)
    today = today or now.astimezone(ctx.tz).date()
    first_monday = monday_of(today) - timedelta(weeks=weeks - 1)
    first_utc = day_bounds(first_monday, ctx.tz)[0]

    firsts = {}
    for uid, at, path in (Trip.objects.filter(status_id__status_code='completed',
                                              completed_at__isnull=False)
                          .order_by('completed_at', 'id')
                          .values_list('user_id', 'completed_at', 'zone__geo_node__path')):
        firsts.setdefault(uid, (at, path))
    cohorts = defaultdict(set)
    for uid, (at, path) in firsts.items():
        if at >= first_utc and path and path.startswith(city.path):
            cohorts[monday_of(at.astimezone(ctx.tz).date())].add(uid)

    activity = defaultdict(set)   # user -> {monday of each week with a completed trip}
    members = {u for users in cohorts.values() for u in users}
    if members:
        for uid, at in (Trip.objects.filter(user_id__in=members, status_id__status_code='completed',
                                            completed_at__gte=first_utc)
                        .values_list('user_id', 'completed_at')):
            activity[uid].add(monday_of(at.astimezone(ctx.tz).date()))

    out = []
    this_monday = monday_of(today)
    for week in (first_monday + timedelta(weeks=i) for i in range(weeks)):
        users = cohorts.get(week, set())
        for k in range(0, int((this_monday - week).days // 7) + 1):
            target = week + timedelta(weeks=k)
            retained = len(users) if k == 0 else sum(1 for u in users if target in activity[u])
            out.append(WeeklyRiderCohort(city=city, cohort_week=week, weeks_since=k,
                                         cohort_size=len(users), retained=retained, built_at=now))
    with transaction.atomic():
        WeeklyRiderCohort.objects.filter(city=city, cohort_week__gte=first_monday).delete()
        WeeklyRiderCohort.objects.bulk_create([r for r in out if r.cohort_size])
    return len(out)


def rebuild_range(cities, start_day, end_day, *, trigger='', cohorts=True):
    """Rebuild every day in `start_day..end_day` for each city (local dates). Returns day count."""
    days = 0
    for city in cities:
        day = start_day
        while day <= end_day:
            rebuild_day(city, day, trigger=trigger)
            days += 1
            day += timedelta(days=1)
        if cohorts:
            rebuild_cohorts(city)
    return days
