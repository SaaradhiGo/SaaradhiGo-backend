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
    """`[zone_code, city_code, state_code]`, most specific first (22 Nov: plans may be set per zone).
    The zone's own code leads only when it differs from its city's."""
    node = getattr(zone, 'geo_node', None)
    out = []
    code = getattr(zone, 'code', '') or ''
    if code and code != getattr(node, 'code', None):
        out.append(code)
    while node is not None and node.kind in ('city', 'state'):
        out.append(node.code)
        node = node.parent
    return out


def class_offered(zone, vehicle_type):
    """May riders be offered `vehicle_type` in `zone`? (owner, 22 Nov: "don't offer a class to riders
    until its driver fee is approved"). False when an active driver fee plan covers the zone and sets
    no driver fee for the class ("to be decided"); True where no fee plan applies (a commission
    market) or the plan sets one. Never raises: an unreadable plan offers the class as before."""
    try:
        from servers.pricing.commission import country_code_of_zone

        country = country_code_of_zone(zone) or getattr(zone, 'country', '')
        scopes, day = scope_codes_of_zone(zone), local_day(country, zone=zone)
        plan = resolve(country, scopes, day, vehicle_type)
        if plan is None:
            # No plan covers this class. In a fee-plan place (some plan applies here) the class's
            # fee is undecided; with no fee plan at all, the commission rules charge it as before.
            return resolve(country, scopes, day) is None
    except Exception:  # noqa: BLE001
        logger.exception('class_offered: fee plan lookup failed')
        return True
    if plan.plan_type == PERCENT_COMMISSION:
        return True
    return any(v is not None for v in plan.amounts_for(vehicle_type).values())


def covers(plan, vehicle_type):
    """Does `plan` say anything for this class (its own row, or "*" for every class)? A row left
    blank ("to be decided") still covers it: the class is decided as undecided, not inherited."""
    table = plan.amounts or {}
    return str(vehicle_type or '').lower() in table or '*' in table


def resolve(country, scope_codes=(), day=None, vehicle_type=None):
    """The plan governing `day`. Without a class: the most specific scope that has one. With a class
    (22 Nov, "anything unset inherits from the level above"): the most specific scope whose plan
    covers that class -- a city plan for cars leaves the city's autos on the state's plan."""
    return resolve_with_source(country, scope_codes, day, vehicle_type)[0]


def resolve_with_source(country, scope_codes=(), day=None, vehicle_type=None):
    """`(plan, scope_code)`: the plan and the place it comes from ('' = the country)."""
    day = day or timezone.localdate()
    qs = (DriverFeePlan.objects.filter(country=str(country or '').upper(),
                                       status__in=(ACTIVE, ARCHIVED), effective_from__lte=day)
          .exclude(effective_to__lte=day))
    by_scope = {}
    for plan in qs.order_by('-effective_from', '-version'):
        by_scope.setdefault(plan.scope_code, plan)
    for code in list(scope_codes) + ['']:
        plan = by_scope.get(code)
        if plan is not None and (vehicle_type is None or covers(plan, vehicle_type)):
            return plan, code
    return None, None


def in_force(plan, day):
    """The plan governed `day`: active or archived, started on or before it, not ended by it."""
    return (plan.status in (ACTIVE, ARCHIVED) and plan.effective_from is not None
            and plan.effective_from <= day and not (plan.effective_to and plan.effective_to <= day))


def local_day(country, when=None, zone=None):
    """The local date; in the zone's own time zone when a zone is given (14 Nov: Boise)."""
    from zoneinfo import ZoneInfo

    from base.countries import get_country

    when = when or timezone.now()
    tz = (getattr(zone, 'timezone_name', '') or get_country(country or None).default_timezone)
    return when.astimezone(ZoneInfo(tz)).date()


def plan_for_zone(zone, when=None, vehicle_type=None):
    from servers.pricing.commission import country_code_of_zone

    if zone is None:
        return None
    country = country_code_of_zone(zone)
    return resolve(country, scope_codes_of_zone(zone), local_day(country, when, zone), vehicle_type)


def snapshot_for_booking(zone, vehicle_type=None, when=None):
    """Trip fields for the plan in force at booking. A non-commission plan zeroes the trip's
    commission snapshot, so settlement keeps the whole fare for the driver."""
    vt = getattr(vehicle_type, 'type', vehicle_type)
    try:
        plan = plan_for_zone(zone, when, vt) if vt else plan_for_zone(zone, when)
    except Exception:  # noqa: BLE001 - a snapshot never breaks a booking
        logger.exception('fee plan snapshot failed')
        return {}
    if plan is None:
        return {}
    if plan.plan_type in NON_COMMISSION and not plan.amounts_for(vt):
        # The plan sets nothing for this class (autos and bikes still to be decided): the class
        # keeps today's percentage commission.
        return {}
    out = {'fee_plan_id': plan.id, 'fee_plan_version': plan.version, 'fee_plan_type': plan.plan_type}
    if plan.plan_type == PERCENT_COMMISSION:
        # 22 Nov: a commission plan's own percentage, never above the legal cap (India 20%).
        pct = plan.amounts_for(vt).get('commission_percent')
        if pct is not None:
            from servers.pricing.regulation import state_code_of_zone

            cap = legal_cap_percent(plan.country, state_code_of_zone(zone) or '')
            out.update({'commission_percent_snapshot': min(Decimal(str(pct)), cap).quantize(Decimal('0.01')),
                        'commission_rule_id': None, 'commission_rule_version': None})
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


def raises_fee(plan, day=None):
    """Would `plan` make any driver pay more than the plan in force for its place and class? Judged
    at 5, 12 and 20 trips of a normal fare per class (the simulator's presets). 22 Nov: an increase
    starts only after the notice period (`commission.notice_days`, 7), a cut the next day."""
    from servers.admin_access.models import GeoNode
    from servers.payments.payout_scope import scope_codes_of_node

    day = day or local_day(plan.country)
    node = GeoNode.objects.filter(code=plan.scope_code).first() if plan.scope_code else None
    scopes = scope_codes_of_node(node) if node is not None else []
    for vt in (plan.amounts or {}):
        if vt == '*':
            continue
        current = resolve(plan.country, scopes, day, vt)
        fare = normal_trip_fare_minor(plan.country, vt) or 15000
        for n in PRESETS.values():
            new = simulate(plan, vt, n, fare)['driver_pays_day_minor']
            old = simulate(current, vt, n, fare)['driver_pays_day_minor'] if current is not None else 0
            if new > old:
                return True
    return False


def legal_cap_percent(country, state_code=''):
    from servers.pricing.regulation import commission_cap

    cap = commission_cap(country, state_code or None)
    return Decimal(cap) if cap is not None else Decimal('20.00')


def _money(minor, currency):
    """Fee text (19 Nov, owner): India in whole rupees ("₹10 a trip, at most ₹149 a day"), paise
    only when there are any; other currencies keep their cents."""
    from base.money import format_money, from_minor, normalize_currency

    text = format_money(from_minor(int(minor), currency), currency)
    if normalize_currency(currency) == 'INR' and int(minor) % 100 == 0 and text.endswith('.00'):
        text = text[:-3]
    return text


def day_fee(plan, vehicle_type, fares_minor, *, day=None, first_trip_day=None, state_code='', label='Today'):
    """The fee for one driver-day under `plan`. `fares_minor` is the day's completed trip fares;
    `label` starts the explanation ("Today" live, the date once the day is charged)."""
    from base.market_dates import day_month

    cur = plan.currency or 'INR'
    n, total = len(fares_minor), sum(int(f) for f in fares_minor)
    if plan.plan_type == PERCENT_COMMISSION:
        return DayFee(0, 'charged', 'Commission is taken on each trip.', n, total)
    if n == 0:
        return DayFee(0, 'no_trips', 'No trips, no fee.', 0, 0)
    if plan.waiver_until and day and day < plan.waiver_until:
        return DayFee(0, 'waiver', f'No fee: launch waiver until {day_month(plan.waiver_until, plan.country)}.', n, total)
    if plan.waiver_first_days and day and first_trip_day and \
            day < first_trip_day + timedelta(days=plan.waiver_first_days):
        until = first_trip_day + timedelta(days=plan.waiver_first_days)
        return DayFee(0, 'waiver', f'No fee: your first {plan.waiver_first_days} days are free '
                                   f'(until {day_month(until, plan.country)}).', n, total)
    if plan.earnings_floor_minor and total < plan.earnings_floor_minor:
        when = 'today' if label == 'Today' else f'on {label}'
        return DayFee(0, 'under_floor', f'No fee {when}: earnings under {_money(plan.earnings_floor_minor, cur)}.',
                      n, total)
    a = plan.amounts_for(vehicle_type)
    cap_pct = legal_cap_percent(plan.country, state_code)
    legal = [int((Decimal(int(f)) * cap_pct / 100).to_integral_value(ROUND_FLOOR)) for f in fares_minor]
    if plan.plan_type == DAILY_PASS:
        raw = int(a.get('daily_pass_minor') or 0)
        allowed = sum(legal)
        fee, per_trip = min(raw, allowed), []
        reduced = raw if fee < raw else None
        text = f'{label}: daily pass {_money(fee, cur)}'
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
        text = f'{label}: {charged_trips} trips × {_money(unit, cur)} = {_money(fee, cur)}'
    if plan.daily_cap_minor is not None and fee > plan.daily_cap_minor:
        fee = plan.daily_cap_minor
        text = f'{text} (cap {_money(plan.daily_cap_minor, cur)})'
    elif plan.daily_cap_minor is not None:
        text = f'{text} (cap {_money(plan.daily_cap_minor, cur)})'
    return DayFee(fee, 'charged', text, n, total, reduced, per_trip)


# ---------------------------------------------------------------------------
# The end-of-day charge
# ---------------------------------------------------------------------------

def driver_timezone(driver, country):
    """The IANA zone of the driver's city (its service zone), else the country's: a driver's day
    closes at midnight where they drive (15 Nov: Treasure Valley on Mountain time)."""
    from base.countries import get_country
    from servers.pricing.models import ServiceZone

    home = getattr(driver, 'home_city_id', None)
    name = ''
    if home:
        name = (ServiceZone.objects.filter(geo_node_id=home).exclude(timezone_name='')
                .order_by('id').values_list('timezone_name', flat=True).first() or '')
    return name or get_country(country or None).default_timezone


def _day_bounds(country, day, tz_name=None):
    """[start, end) of a local day. Built from the date at midnight, so a 23- or 25-hour day
    (daylight saving) is exactly that long."""
    from zoneinfo import ZoneInfo

    from base.countries import get_country

    tz = ZoneInfo(tz_name or get_country(country or None).default_timezone)
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    nxt = day + timedelta(days=1)
    return start, datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)


def _state_code(zone):
    node = getattr(zone, 'geo_node', None)
    while node is not None and node.kind != 'state':
        node = node.parent
    return node.code.split('-')[-1] if node is not None else ''


def trip_class(trip):
    """The ride class a trip's fee is worked out for (11 Nov): the class booked; with none (a trip
    written without one), the class of the vehicle that drove it, else the driver's active vehicle."""
    if trip.requested_vehicle_type_id:
        return getattr(trip.requested_vehicle_type, 'type', '') or ''
    for vehicle in (getattr(trip, 'vehicle_id', None), getattr(getattr(trip, 'driver_id', None), 'active_vehicle', None)):
        vt = getattr(getattr(vehicle, 'vehicle_type_id', None), 'type', '') if vehicle is not None else ''
        if vt:
            return vt
    return ''


def day_trips(driver, country, day):
    from servers.ride.models import Trip

    start, end = _day_bounds(country, day, driver_timezone(driver, country))
    return list(Trip.objects.filter(driver_id=driver, status_id__status_code='completed',
                                    completed_at__gte=start, completed_at__lt=end,
                                    fee_plan_type__in=NON_COMMISSION)
                .select_related('zone', 'requested_vehicle_type', 'vehicle_id__vehicle_type_id', 'driver_id__active_vehicle__vehicle_type_id').order_by('completed_at', 'id'))


def first_trip_day(driver, country):
    from servers.ride.models import Trip

    first = (Trip.objects.filter(driver_id=driver, status_id__status_code='completed')
             .order_by('completed_at').values_list('completed_at', flat=True).first())
    if not first:
        return None
    from zoneinfo import ZoneInfo
    return first.astimezone(ZoneInfo(driver_timezone(driver, country))).date()


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
    if not in_force(plan, day):
        # 15 Nov: a trip keeps the plan it was booked under, but a plan retired (or never in force)
        # for this day charges nothing. Recorded as a zero row so the day is not looked at again.
        try:
            with transaction.atomic():
                return DriverDailyFee.objects.create(
                    driver=driver, day=day, plan=plan, plan_version=plan.version, currency=plan.currency,
                    trips=len(trips), fares_minor=0, fee_minor=0, reason='not_in_force',
                    explanation='No fee: the plan was not in force on this day.', trip_ids=[t.id for t in trips])
        except IntegrityError:
            return DriverDailyFee.objects.filter(driver=driver, day=day).first()
    vt = trip_class(trips[0])
    fares = [t.final_fare_minor if t.final_fare_minor is not None else (t.estimated_fare_minor or 0)
             for t in trips]
    from base.market_dates import day_month
    result = day_fee(plan, vt, fares, day=day, first_trip_day=first_trip_day(driver, country),
                     state_code=_state_code(trips[0].zone), label=day_month(day, country))
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
    """Hourly: each driver's local day that has just ended, in their city's own time zone (a day
    closes at midnight there, 23 or 25 hours long across a clock change). Idempotent."""
    from zoneinfo import ZoneInfo

    from base.countries import COUNTRIES
    from servers.driver.models import Driver
    from servers.ride.models import Trip

    now = now or timezone.now()
    out = {}
    for code in COUNTRIES:
        # Every driver with a plan trip in the last two days; each is charged for their own yesterday.
        drivers = (Trip.objects.filter(status_id__status_code='completed', completed_at__gte=now - timedelta(hours=50),
                                       completed_at__lt=now, fee_plan_type__in=NON_COMMISSION,
                                       zone__country=code)
                   .values_list('driver_id', flat=True).distinct())
        n, days = 0, {}
        for driver in Driver.objects.filter(pk__in=list(drivers)).select_related('user_id'):
            tz = driver_timezone(driver, code)
            day = now.astimezone(ZoneInfo(tz)).date() - timedelta(days=1)
            days[tz] = str(day)
            try:
                if charge_day(driver, code, day) is not None:
                    n += 1
            except Exception:  # noqa: BLE001 - one driver never stops the others
                logger.exception('platform fee failed driver=%s day=%s', driver.id, day)
        out[code] = {'drivers': n, 'days': days}
    return out


def today_so_far(driver, country, now=None):
    """The driver's fee for today's completed trips, as the end-of-day job will work it out."""
    from zoneinfo import ZoneInfo
    day = (now or timezone.now()).astimezone(ZoneInfo(driver_timezone(driver, country))).date()
    trips = day_trips(driver, country, day)
    if not trips:
        return None
    plan = DriverFeePlan.objects.filter(pk=trips[0].fee_plan_id).first()
    if plan is None:
        return None
    vt = trip_class(trips[0])
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
    for row in settlement_rows(driver, refunds=False):
        if row.trip_id is not None and row.commission is not None:
            out[row.trip_id] = out.get(row.trip_id, 0) + to_minor(row.commission, cur)
    return out


def overcharged(driver, country):
    """Days on which a non-commission plan was in force for the trip's city and class (or, before any
    plan existed, the plan in force now) but the trip settled with a percentage commission. Each day:
    `(day, plan, [(trip, commission_minor, refund_minor)])`. The refund is the commission less any
    plan fee the day would have owed with those trips counted, so the driver ends where the plan
    puts them."""
    from servers.ride.models import Trip

    trips = list(Trip.objects.filter(driver_id=driver, status_id__status_code='completed', fee_plan_type='',
                                     completed_at__isnull=False)
                 .select_related('zone', 'requested_vehicle_type', 'vehicle_id__vehicle_type_id', 'driver_id__active_vehicle__vehicle_type_id').order_by('completed_at', 'id'))
    commission = _commission_minor(driver)
    by_day = {}
    for t in trips:
        if commission.get(t.id, 0) <= 0 or t.zone is None:
            continue
        day = local_day(country, t.completed_at, t.zone)
        # A driver never pays for configuration written late (owner ruling, 11 Nov): a day before any
        # plan existed is judged by the plan in force now, waiver included.
        plan = resolve(country, scope_codes_of_zone(t.zone), day)
        if plan is None:
            plan = resolve(country, scope_codes_of_zone(t.zone))
            if plan is not None:
                _late_plan_alert(driver, country, day, plan)
        vt = trip_class(t)
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


def _late_plan_alert(driver, country, day, plan):
    """Every time a day with no plan is judged by today's (11 Nov): logged and one console alert per
    driver and day. In production every driver has a plan from day one, so this should never fire."""
    logger.warning('fee_plan_missing_for_day', extra={
        'event': 'fee_plan_missing_for_day', 'driver_id': driver.id, 'country': country, 'day': str(day),
        'judged_by_plan': plan.id})
    try:
        from servers.command_centre.models import DATA_INTEGRITY
        from servers.command_centre.services import raise_alert

        raise_alert(DATA_INTEGRITY, title=f'No fee plan on {day} for driver {driver.id}: judged by plan {plan.id} '
                                          f'({plan.plan_type} v{plan.version})',
                    payload={'driver_id': driver.id, 'country': country, 'day': str(day), 'plan_id': plan.id},
                    source_type='fee_plan_missing_for_day', source_id=f'{driver.id}:{day}')
    except Exception:  # noqa: BLE001 - the alert never stops a refund
        logger.exception('fee plan late alert failed')


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


def refund_day(driver, day, *, apply=False):
    """Return a day's plan fee (15 Nov: Nov 14's $3.00 under a plan never approved). The same
    append-only `platform_fee_refund` credit as a trip refund, against `DAY_<yyyymmdd>`, idempotent
    per driver and day. Returns `(charged_minor, refund_minor, written)`; nothing for a day with no fee."""
    from servers.rider.models import Wallet, WalletTransaction, get_wallet

    fee = DriverDailyFee.objects.filter(driver=driver, day=day).first()
    if fee is None or not fee.fee_minor:
        return 0, 0, False
    key = f'PLATFORM_FEE_REFUND_DAY_{driver.id}_{day:%Y%m%d}'
    if WalletTransaction.objects.filter(idempotency_key=key).exists():
        return fee.fee_minor, fee.fee_minor, True
    if not apply:
        return fee.fee_minor, fee.fee_minor, False
    with transaction.atomic():
        wallet = get_wallet(driver.user_id, Wallet.SCOPE_DRIVER, lock=True)
        WalletTransaction.objects.create(
            user_id=driver.user_id, currency=fee.currency, amount_minor=fee.fee_minor, txn_type='credit',
            status='completed', purpose=REFUND_PURPOSE, reference_id=f'DAY_{day:%Y%m%d}', idempotency_key=key)
        wallet.balance_minor += fee.fee_minor
        wallet.save(update_fields=['balance_minor'])
    return fee.fee_minor, fee.fee_minor, True


# ---------------------------------------------------------------------------
# The words on the driver's screens (11 Nov): always from the plan in force
# ---------------------------------------------------------------------------

def plan_words(plan, vehicle_type, country=None):
    """A plan for one class in plain words (22 Nov): "₹20 a day", "₹10 a trip, at most ₹149 a day",
    "10% of each fare", "To be decided", with "first 60 days free" when it waives new drivers."""
    if plan is None:
        return None
    a = plan.amounts_for(vehicle_type)
    cur = plan.currency
    if not any(v is not None for v in a.values()):
        return 'To be decided'
    if _charges_nothing(plan, a):
        return 'No platform fee'
    if plan.plan_type == DAILY_PASS:
        text = f"{_money(a.get('daily_pass_minor') or 0, cur)} a day, only on a day with a trip"
    elif plan.plan_type == PERCENT_COMMISSION:
        text = f"{a.get('commission_percent')}% of each fare"
    else:
        text = f"{_money(a.get('per_trip_fee_minor') or 0, cur)} a trip"
        if plan.daily_cap_minor is not None:
            text += f', at most {_money(plan.daily_cap_minor, cur)} a day'
    if plan.waiver_first_days:
        text += f'; first {plan.waiver_first_days} days free'
    return text


def _driver_place(driver):
    from servers.payments.payout_config import driver_payout_country
    from servers.payments.payout_scope import scope_codes_of_node

    country = driver_payout_country(driver) or 'IN'
    home = getattr(driver, 'home_city', None)
    vehicle = getattr(driver, 'active_vehicle', None)
    vt = getattr(getattr(vehicle, 'vehicle_type_id', None), 'type', '') if vehicle is not None else ''
    return country, (scope_codes_of_node(home) if home else []), vt


def fee_notice(driver, today=None):
    """An approved change that has not started yet, for the driver (22 Nov: "notices of upcoming
    changes"): "From 30 Nov: ₹49 daily fee, charged only if you complete a trip", or None."""
    country, scopes, vt = _driver_place(driver)
    today = today or local_day(country)
    upcoming = (DriverFeePlan.objects.filter(country=country, status=ACTIVE, effective_from__gt=today,
                                             scope_code__in=list(scopes) + [''])
                .order_by('effective_from'))
    order = {code: i for i, code in enumerate(list(scopes) + [''])}
    for plan in sorted(upcoming, key=lambda p: (p.effective_from, order.get(p.scope_code, 99))):
        if covers(plan, vt):
            from base.market_dates import day_month

            words = _line_words(plan, vt)
            return f'From {day_month(plan.effective_from, country)}: {words}'
    return None


def _charges_nothing(plan, a):
    """A plan whose fee for this class is zero (Treasure Valley's launch plan): no platform fee."""
    keys = {DAILY_PASS: 'daily_pass_minor', PERCENT_COMMISSION: 'commission_percent'}.get(
        plan.plan_type, 'per_trip_fee_minor')
    return a.get(keys) is not None and float(a.get(keys) or 0) == 0


def _line_words(plan, vt):
    cur, a = plan.currency, plan.amounts_for(vt)
    if _charges_nothing(plan, a):
        return 'No platform fee'
    if plan.plan_type == DAILY_PASS:
        return f"{_money(a.get('daily_pass_minor') or 0, cur)} daily fee, charged only if you complete a trip"
    if plan.plan_type == PERCENT_COMMISSION:
        return f"{a.get('commission_percent')}% of each fare"
    text = f"{_money(a.get('per_trip_fee_minor') or 0, cur)} a trip"
    if plan.daily_cap_minor is not None:
        text += f', at most {_money(plan.daily_cap_minor, cur)} a day'
    return text


def fee_words(driver, today=None):
    """How the driver's platform fee works, in a few words, from the plan in force for their city
    and class: "No fee until 1 Jan, then ₹10.00 a trip, at most ₹149.00 a day". Never fixed text."""
    from servers.payments.payout_config import driver_payout_country
    from servers.payments.payout_scope import scope_codes_of_node

    country, scopes, vt = _driver_place(driver)
    today = today or local_day(country)
    plan = resolve(country, scopes, today, vt or None)
    if plan is None and _fee_plan_market(driver):
        # 15 Nov: a market that charges by fee plan (USA) with no plan in force charges nothing,
        # and says so; it never shows a rate the owner has not approved.
        return 'No platform fee'
    if plan is None or not plan.amounts_for(vt):
        return 'A commission on each trip, shown on every trip'
    if plan.plan_type == PERCENT_COMMISSION and plan.amounts_for(vt).get('commission_percent') is None:
        return 'A commission on each trip, shown on every trip'
    text = _line_words(plan, vt)
    until = None
    if plan.waiver_until and today < plan.waiver_until:
        until = plan.waiver_until
    first = first_trip_day(driver, country) or today
    if plan.waiver_first_days and today < first + timedelta(days=plan.waiver_first_days):
        until = max(until or today, first + timedelta(days=plan.waiver_first_days))
    if until:
        from base.market_dates import day_month

        return f'No fee until {day_month(until, country)}, then {text}'
    return text[0].upper() + text[1:]


def _fee_plan_market(driver):
    """Whether drivers here pay a fee plan instead of a commission (`fee_plan_required`, USA)."""
    from servers.driver.records import market_of
    from servers.pricing.regulation import market_rules

    try:
        country, state = market_of(driver)
        return bool(market_rules(country or 'IN', state)['fee_plan_required'])
    except Exception:  # noqa: BLE001 - the words fall back to the commission wording
        return False


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
            'driver_takes_home_day_minor': total - fee,
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



def dated_explanation(fee, country):
    """A charged day's explanation with its date, also for rows written as "Today: ..." before 15 Nov."""
    from base.market_dates import day_month

    text = fee.explanation or ''
    return f'{day_month(fee.day, country)}:{text[6:]}' if text.startswith('Today:') else text
