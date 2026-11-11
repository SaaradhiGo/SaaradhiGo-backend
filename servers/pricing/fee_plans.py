"""Driver fee plans: resolution, the day's fee, the end-of-day charge, the simulator (owner, 9 Nov).

ONE ARITHMETIC: `day_fee` is used by the end-of-day job, the driver's "today so far" line and the
console simulator, so the three can never disagree (a test holds them equal).

THE DAY'S FEE (for every plan type but percent_commission), in this order:
  1  launch waiver (before `waiver_until`, or in the driver's first `waiver_first_days`)  -> 0
  2  earnings floor: the day's completed fares below `earnings_floor_minor`              -> 0
  3  per trip: `per_trip_fee_minor` (per_trip_fee, fair_share) for each trip up to
     `fee_free_after_trips`; or `daily_pass_minor` once (daily_pass) on a day with a trip
  4  the legal guard: the driver keeps at least the MVAG share on every trip (80% today, from
     `regulation.commission_cap`): a trip's fee never exceeds cap% of its fare, a pass never
     exceeds cap% of the day's fares; a reduction is recorded on the day
  5  the daily cap `daily_cap_minor`

WHEN: settlement records each trip with no commission (the trip's commission snapshot is 0 when its
booking-time plan is not percent_commission); the hourly `pricing.charge_driver_fees` writes ONE
`platform_fee` ledger debit per driver and local day, the day after, idempotent per driver and day.
A cash driver's fee is owed and comes off the next earnings like cash-trip commission today.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
from typing import Optional

from django.db import IntegrityError, transaction
from django.utils import timezone

from .fee_plan_models import (
    ACTIVE, ARCHIVED, DAILY_PASS, FAIR_SHARE, PER_TRIP_FEE, PERCENT_COMMISSION, DriverDailyFee,
    DriverFeePlan,
)

logger = logging.getLogger(__name__)

PURPOSE = 'platform_fee'
#: A fee taken that the plan did not ask for, returned as its own credit (11 Nov).
REFUND_PURPOSE = 'platform_fee_refund'
NON_COMMISSION = (PER_TRIP_FEE, DAILY_PASS, FAIR_SHARE)


# ---------------------------------------------------------------------------
# Resolution: city -> state -> country, on a local day
# ---------------------------------------------------------------------------

def scope_codes_of_zone(zone):
    """`[city_code, state_code]` of a zone's geo node (most specific first)."""
    node = getattr(zone, 'geo_node', None)
    out = []
    while node is not None and node.kind in ('city', 'state'):
        out.append(node.code)
        node = node.parent
    return out


def resolve(country, scope_codes=(), day=None):
    """The plan governing `day` in the most specific scope that has one, or None."""
    day = day or timezone.localdate()
    qs = (DriverFeePlan.objects.filter(country=str(country or '').upper(),
                                       status__in=(ACTIVE, ARCHIVED), effective_from__lte=day)
          .exclude(effective_to__lte=day))
    by_scope = {}
    for plan in qs.order_by('-effective_from', '-version'):
        by_scope.setdefault(plan.scope_code, plan)
    for code in list(scope_codes) + ['']:
        if code in by_scope:
            return by_scope[code]
    return None


def local_day(country, when=None):
    from zoneinfo import ZoneInfo

    from base.countries import get_country

    when = when or timezone.now()
    return when.astimezone(ZoneInfo(get_country(country or None).default_timezone)).date()


def plan_for_zone(zone, when=None):
    from servers.pricing.commission import country_code_of_zone

    if zone is None:
        return None
    country = country_code_of_zone(zone)
    return resolve(country, scope_codes_of_zone(zone), local_day(country, when))


def snapshot_for_booking(zone, vehicle_type=None, when=None):
    """Trip fields for the plan in force at booking. A non-commission plan zeroes the trip's
    commission snapshot, so settlement keeps the whole fare for the driver."""
    try:
        plan = plan_for_zone(zone, when)
    except Exception:  # noqa: BLE001 - a snapshot never breaks a booking
        logger.exception('fee plan snapshot failed')
        return {}
    if plan is None:
        return {}
    vt = getattr(vehicle_type, 'type', vehicle_type)
    if plan.plan_type in NON_COMMISSION and not plan.amounts_for(vt):
        # The plan sets nothing for this class (autos and bikes still to be decided): the class
        # keeps today's percentage commission.
        return {}
    out = {'fee_plan_id': plan.id, 'fee_plan_version': plan.version, 'fee_plan_type': plan.plan_type}
    if plan.plan_type in NON_COMMISSION:
        out.update({'commission_percent_snapshot': Decimal('0.00'), 'commission_rule_id': None,
                    'commission_rule_version': None})
    return out


# ---------------------------------------------------------------------------
# The day's fee
# ---------------------------------------------------------------------------

@dataclass
class DayFee:
    fee_minor: int
    reason: str                     # charged | waiver | under_floor | no_trips
    explanation: str
    trips: int
    fares_minor: int
    reduced_from_minor: Optional[int] = None
    per_trip_minor: list = field(default_factory=list)


def legal_cap_percent(country, state_code=''):
    from servers.pricing.regulation import commission_cap

    cap = commission_cap(country, state_code or None)
    return Decimal(cap) if cap is not None else Decimal('20.00')


def _money(minor, currency):
    from base.money import format_money, from_minor

    return format_money(from_minor(int(minor), currency), currency)


def day_fee(plan, vehicle_type, fares_minor, *, day=None, first_trip_day=None, state_code=''):
    """The fee for one driver-day under `plan`. `fares_minor` is the day's completed trip fares."""
    cur = plan.currency or 'INR'
    n, total = len(fares_minor), sum(int(f) for f in fares_minor)
    if plan.plan_type == PERCENT_COMMISSION:
        return DayFee(0, 'charged', 'Commission is taken on each trip.', n, total)
    if n == 0:
        return DayFee(0, 'no_trips', 'No trips, no fee.', 0, 0)
    if plan.waiver_until and day and day < plan.waiver_until:
        return DayFee(0, 'waiver', f'No fee: launch waiver until {plan.waiver_until:%d %b}.', n, total)
    if plan.waiver_first_days and day and first_trip_day and \
            day < first_trip_day + timedelta(days=plan.waiver_first_days):
        until = first_trip_day + timedelta(days=plan.waiver_first_days)
        return DayFee(0, 'waiver', f'No fee: your first {plan.waiver_first_days} days are free '
                                   f'(until {until:%d %b}).', n, total)
    if plan.earnings_floor_minor and total < plan.earnings_floor_minor:
        return DayFee(0, 'under_floor', f'No fee today: earnings under {_money(plan.earnings_floor_minor, cur)}.',
                      n, total)
    a = plan.amounts_for(vehicle_type)
    cap_pct = legal_cap_percent(plan.country, state_code)
    legal = [int((Decimal(int(f)) * cap_pct / 100).to_integral_value(ROUND_FLOOR)) for f in fares_minor]
    if plan.plan_type == DAILY_PASS:
        raw = int(a.get('daily_pass_minor') or 0)
        allowed = sum(legal)
        fee, per_trip = min(raw, allowed), []
        reduced = raw if fee < raw else None
        text = f'Today: daily pass {_money(fee, cur)}'
    else:
        unit = int(a.get('per_trip_fee_minor') or 0)
        free_after = a.get('fee_free_after_trips')
        per_trip, reduced_any, raw_sum = [], False, 0
        for i, cap in enumerate(legal):
            want = 0 if (free_after and i >= int(free_after)) else unit
            raw_sum += want
            got = min(want, cap)
            reduced_any = reduced_any or got < want
            per_trip.append(got)
        fee = sum(per_trip)
        reduced = raw_sum if reduced_any else None
        charged_trips = sum(1 for x in per_trip if x)
        text = f'Today: {charged_trips} trips × {_money(unit, cur)} = {_money(fee, cur)}'
    if plan.daily_cap_minor is not None and fee > plan.daily_cap_minor:
        fee = plan.daily_cap_minor
        text = f'{text} (cap {_money(plan.daily_cap_minor, cur)})'
    elif plan.daily_cap_minor is not None:
        text = f'{text} (cap {_money(plan.daily_cap_minor, cur)})'
    return DayFee(fee, 'charged', text, n, total, reduced, per_trip)


# ---------------------------------------------------------------------------
# The end-of-day charge
# ---------------------------------------------------------------------------

def _day_bounds(country, day):
    from zoneinfo import ZoneInfo

    from base.countries import get_country

    tz = ZoneInfo(get_country(country or None).default_timezone)
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    return start, start + timedelta(days=1)


def _state_code(zone):
    node = getattr(zone, 'geo_node', None)
    while node is not None and node.kind != 'state':
        node = node.parent
    return node.code.split('-')[-1] if node is not None else ''


def day_trips(driver, country, day):
    from servers.ride.models import Trip

    start, end = _day_bounds(country, day)
    return list(Trip.objects.filter(driver_id=driver, status_id__status_code='completed',
                                    completed_at__gte=start, completed_at__lt=end,
                                    fee_plan_type__in=NON_COMMISSION)
                .select_related('zone', 'requested_vehicle_type').order_by('completed_at', 'id'))


def first_trip_day(driver, country):
    from servers.ride.models import Trip

    first = (Trip.objects.filter(driver_id=driver, status_id__status_code='completed')
             .order_by('completed_at').values_list('completed_at', flat=True).first())
    return local_day(country, first) if first else None


def charge_day(driver, country, day):
    """Write the day's `platform_fee` row (once). Returns the DriverDailyFee or None (no plan trips)."""
    from servers.rider.models import Wallet, WalletTransaction, get_wallet

    existing = DriverDailyFee.objects.filter(driver=driver, day=day).first()
    if existing is not None:
        return existing
    trips = day_trips(driver, country, day)
    if not trips:
        return None
    plan = DriverFeePlan.objects.get(pk=trips[0].fee_plan_id)
    vt = getattr(trips[0].requested_vehicle_type, 'type', '') if trips[0].requested_vehicle_type_id else ''
    fares = [t.final_fare_minor if t.final_fare_minor is not None else (t.estimated_fare_minor or 0)
             for t in trips]
    result = day_fee(plan, vt, fares, day=day, first_trip_day=first_trip_day(driver, country),
                     state_code=_state_code(trips[0].zone))
    if result.reduced_from_minor is not None:
        logger.warning('platform_fee_legal_guard', extra={
            'event': 'platform_fee_legal_guard', 'driver_id': driver.id, 'day': str(day),
            'wanted_minor': result.reduced_from_minor, 'charged_minor': result.fee_minor})
    try:
        with transaction.atomic():
            row = DriverDailyFee.objects.create(
                driver=driver, day=day, plan=plan, plan_version=plan.version, currency=plan.currency,
                trips=result.trips, fares_minor=result.fares_minor, fee_minor=result.fee_minor,
                guard_reduced_from_minor=result.reduced_from_minor, reason=result.reason,
                explanation=result.explanation[:255], trip_ids=[t.id for t in trips])
            if result.fee_minor:
                wallet = get_wallet(driver.user_id, Wallet.SCOPE_DRIVER, lock=True)
                txn = WalletTransaction.objects.create(
                    user_id=driver.user_id, currency=plan.currency, amount_minor=result.fee_minor,
                    txn_type='debit', status='completed', purpose=PURPOSE,
                    reference_id=f'DAY_{day:%Y%m%d}', idempotency_key=f'PLATFORM_FEE_{driver.id}_{day:%Y%m%d}')
                wallet.balance_minor -= result.fee_minor
                wallet.save(update_fields=['balance_minor'])
                row.wallet_transaction_id = txn.id
                row.save(update_fields=['wallet_transaction_id'])
    except IntegrityError:
        return DriverDailyFee.objects.filter(driver=driver, day=day).first()
    return row


def charge_yesterday(now=None):
    """Hourly: every country's drivers, for the local day that has just ended. Idempotent."""
    from base.countries import COUNTRIES
    from servers.driver.models import Driver
    from servers.ride.models import Trip

    out = {}
    for code in COUNTRIES:
        day = local_day(code, now) - timedelta(days=1)
        start, end = _day_bounds(code, day)
        drivers = (Trip.objects.filter(status_id__status_code='completed', completed_at__gte=start,
                                       completed_at__lt=end, fee_plan_type__in=NON_COMMISSION,
                                       zone__country=code)
                   .values_list('driver_id', flat=True).distinct())
        n = 0
        for driver in Driver.objects.filter(pk__in=list(drivers)).select_related('user_id'):
            try:
                if charge_day(driver, code, day) is not None:
                    n += 1
            except Exception:  # noqa: BLE001 - one driver never stops the others
                logger.exception('platform fee failed driver=%s day=%s', driver.id, day)
        out[code] = {'day': str(day), 'drivers': n}
    return out


def today_so_far(driver, country, now=None):
    """The driver's fee for today's completed trips, as the end-of-day job will work it out."""
    day = local_day(country, now)
    trips = day_trips(driver, country, day)
    if not trips:
        return None
    plan = DriverFeePlan.objects.filter(pk=trips[0].fee_plan_id).first()
    if plan is None:
        return None
    vt = getattr(trips[0].requested_vehicle_type, 'type', '') if trips[0].requested_vehicle_type_id else ''
    fares = [t.final_fare_minor if t.final_fare_minor is not None else (t.estimated_fare_minor or 0)
             for t in trips]
    result = day_fee(plan, vt, fares, day=day, first_trip_day=first_trip_day(driver, country),
                     state_code=_state_code(trips[0].zone))
    return {'fee_minor': result.fee_minor, 'cap_minor': plan.daily_cap_minor, 'currency': plan.currency,
            'explanation': result.explanation, 'plan_type': plan.plan_type}


# ---------------------------------------------------------------------------
# Commission the plan did not ask for (11 Nov)
# ---------------------------------------------------------------------------

def _commission_minor(driver):
    """`{trip_id: commission_minor}` as the driver's ledger settled it (the same rows Trips and
    Earnings show), so a trip without a settlement record is not missed."""
    from base.money import to_minor
    from servers.driver.earnings import settlement_rows
    from servers.driver.services import driver_currency

    cur = driver_currency(driver)
    out = {}
    for row in settlement_rows(driver):
        if row.trip_id is not None and row.commission is not None:
            out[row.trip_id] = out.get(row.trip_id, 0) + to_minor(row.commission, cur)
    return out


def overcharged(driver, country):
    """Days on which a non-commission plan was in force for the trip's city and class but the trip
    settled with a percentage commission (booked before the plan existed: QA, 10 Nov). Each day:
    `(day, plan, [(trip, commission_minor, refund_minor)])`. The refund is the commission less any
    plan fee the day would have owed with those trips counted, so the driver ends where the plan
    puts them."""
    from servers.ride.models import Trip

    trips = list(Trip.objects.filter(driver_id=driver, status_id__status_code='completed', fee_plan_type='',
                                     completed_at__isnull=False)
                 .select_related('zone', 'requested_vehicle_type').order_by('completed_at', 'id'))
    commission = _commission_minor(driver)
    by_day = {}
    for t in trips:
        if commission.get(t.id, 0) <= 0 or t.zone is None:
            continue
        day = local_day(country, t.completed_at)
        plan = resolve(country, scope_codes_of_zone(t.zone), day)
        vt = getattr(t.requested_vehicle_type, 'type', '') if t.requested_vehicle_type_id else ''
        if plan is None or plan.plan_type not in NON_COMMISSION or not plan.amounts_for(vt):
            continue
        by_day.setdefault(day, (plan, vt, []))[2].append(t)
    first = first_trip_day(driver, country)
    out = []
    for day, (plan, vt, extra) in sorted(by_day.items()):
        counted_trips = day_trips(driver, country, day) + extra
        fares = [t.final_fare_minor if t.final_fare_minor is not None else (t.estimated_fare_minor or 0)
                 for t in counted_trips]
        owed = day_fee(plan, vt, fares, day=day, first_trip_day=first,
                       state_code=_state_code(extra[0].zone)).fee_minor
        charged = DriverDailyFee.objects.filter(driver=driver, day=day).values_list('fee_minor', flat=True).first() or 0
        still_owed = max(owed - charged, 0)
        rows = []
        for t in extra:
            c = commission[t.id]
            keep = min(c, still_owed)
            still_owed -= keep
            rows.append((t, c, c - keep))
        out.append((day, plan, rows))
    return out


def refund_overcharged(driver, country, *, apply=False):
    """One `platform_fee_refund` credit per overcharged trip (append-only, idempotent per trip).
    Returns `[(trip_id, day, commission_minor, refund_minor, written)]`."""
    from servers.rider.models import Wallet, WalletTransaction, get_wallet

    report = []
    for day, plan, rows in overcharged(driver, country):
        for trip, commission, refund in rows:
            key = f'PLATFORM_FEE_REFUND_TRIP_{trip.id}'
            done = WalletTransaction.objects.filter(idempotency_key=key).exists()
            written = False
            if apply and refund > 0 and not done:
                with transaction.atomic():
                    wallet = get_wallet(driver.user_id, Wallet.SCOPE_DRIVER, lock=True)
                    WalletTransaction.objects.create(
                        user_id=driver.user_id, currency=plan.currency, amount_minor=refund, txn_type='credit',
                        status='completed', purpose=REFUND_PURPOSE, reference_id=f'TRIP_{trip.id}',
                        idempotency_key=key)
                    wallet.balance_minor += refund
                    wallet.save(update_fields=['balance_minor'])
                written = True
            report.append((trip.id, day, commission, refund, written or done))
    return report


# ---------------------------------------------------------------------------
# The words on the driver's screens (11 Nov): always from the plan in force
# ---------------------------------------------------------------------------

def fee_words(driver, today=None):
    """How the driver's platform fee works, in a few words, from the plan in force for their city
    and class: "No fee until 1 Jan, then ₹10.00 a trip, at most ₹149.00 a day". Never fixed text."""
    from servers.payments.payout_config import driver_payout_country
    from servers.payments.payout_scope import scope_codes_of_node

    country = driver_payout_country(driver) or 'IN'
    today = today or local_day(country)
    home = getattr(driver, 'home_city', None)
    plan = resolve(country, scope_codes_of_node(home) if home else [], today)
    vehicle = getattr(driver, 'active_vehicle', None)
    vt = getattr(getattr(vehicle, 'vehicle_type_id', None), 'type', '') if vehicle is not None else ''
    if plan is None or plan.plan_type == PERCENT_COMMISSION or not plan.amounts_for(vt):
        return 'A commission on each trip, shown on every trip'
    cur, a = plan.currency, plan.amounts_for(vt)
    if plan.plan_type == DAILY_PASS:
        text = f"{_money(a.get('daily_pass_minor') or 0, cur)} a day you drive"
    else:
        text = f"{_money(a.get('per_trip_fee_minor') or 0, cur)} a trip"
    if plan.daily_cap_minor is not None and plan.plan_type != DAILY_PASS:
        text += f', at most {_money(plan.daily_cap_minor, cur)} a day'
    until = None
    if plan.waiver_until and today < plan.waiver_until:
        until = plan.waiver_until
    first = first_trip_day(driver, country) or today
    if plan.waiver_first_days and today < first + timedelta(days=plan.waiver_first_days):
        until = max(until or today, first + timedelta(days=plan.waiver_first_days))
    if until:
        return f'No fee until {until.day} {until:%b}, then {text}'
    return text[0].upper() + text[1:]


# ---------------------------------------------------------------------------
# The simulator (B3)
# ---------------------------------------------------------------------------

PRESETS = {'basic': 5, 'medium': 12, 'heavy': 20}


def simulate(plan, vehicle_type, trips_per_day, average_fare_minor, *, cost_per_trip_minor=0,
             commission_percent=None, state_code=''):
    """One plan at one preset: what the driver pays per day and month, their share, our cost and
    what SaaradhiGo keeps. A percent_commission plan uses `commission_percent`."""
    fares = [int(average_fare_minor)] * int(trips_per_day)
    total = sum(fares)
    if plan.plan_type == PERCENT_COMMISSION:
        pct = Decimal(str(commission_percent if commission_percent is not None
                          else plan.amounts_for(vehicle_type).get('commission_percent') or 0))
        fee = sum(int((Decimal(f) * pct / 100).quantize(Decimal('1'))) for f in fares)
        legal_ok = pct <= legal_cap_percent(plan.country, state_code)
    else:
        result = day_fee(plan, vehicle_type, fares, state_code=state_code)
        fee = result.fee_minor
        legal_ok = result.reduced_from_minor is None
    cost = int(cost_per_trip_minor) * len(fares)
    return {'plan_type': plan.plan_type, 'trips_per_day': int(trips_per_day),
            'fares_per_day_minor': total, 'driver_pays_day_minor': fee,
            'driver_pays_month_minor': fee * 30,
            'driver_share_percent': round(100 * (total - fee) / total, 1) if total else None,
            'our_cost_day_minor': cost, 'we_keep_day_minor': fee - cost,
            'loses_money': fee - cost < 0, 'breaks_legal_share': not legal_ok}


def normal_trip_fare_minor(country, vehicle_type):
    """A normal trip for the activation check: 5 km, 15 min on the market's active rate card."""
    from servers.pricing.models import RateCard

    fares = [max(c.base_fare + c.per_km_fare * 5 + c.per_min_fare * 15, c.min_fare)
             for c in RateCard.objects.filter(is_active=True, vehicle_type__type__iexact=vehicle_type,
                                              zone__country=country)]
    # The cheapest market's normal trip: the most protective check.
    return int(min(fares) * 100) if fares else None


def breaks_legal_share_on_normal_trip(plan):
    """Classes for which the plan takes more than the legal share of a normal trip (activation
    is refused). Classes with no rate card are skipped."""
    cap = legal_cap_percent(plan.country)
    bad = []
    for vt in [k for k in (plan.amounts or {}) if k != '*']:
        fare = normal_trip_fare_minor(plan.country, vt)
        if not fare:
            continue
        a = plan.amounts_for(vt)
        if plan.plan_type in (PER_TRIP_FEE, FAIR_SHARE) and int(a.get('per_trip_fee_minor') or 0) > fare * cap / 100:
            bad.append(vt)
        if plan.plan_type == DAILY_PASS and int(a.get('daily_pass_minor') or 0) > fare * PRESETS['basic'] * cap / 100:
            bad.append(vt)
        if plan.plan_type == PERCENT_COMMISSION and Decimal(str(a.get('commission_percent') or 0)) > cap:
            bad.append(vt)
    return bad

