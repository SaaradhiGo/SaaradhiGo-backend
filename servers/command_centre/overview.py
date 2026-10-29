"""Per-city numbers for the Command Centre, and the same numbers a week ago.

ONE QUERY PER TABLE, NOT ONE PER CITY
-------------------------------------
Every figure is a conditional aggregate -- `Count(..., filter=Q(<this
city's anchor> & <this city's window>))` -- so a table is read once for
every city in the caller's scope, however many there are. A city's anchor
is the same ORM path `admin_access.scoping.SCOPE_PATHS` uses for that
table, spelled here once per table, so the Command Centre cannot count a
trip the Trips list would not show.

WINDOWS ARE PER CITY
--------------------
"Today" is the city's own day. Hyderabad's today starts at midnight IST
and Austin's at midnight Central, so the two rows on one screen are each
about the day their operators are living in; the `24h` and `7d` windows
are rolling and identical everywhere. Every stored timestamp is UTC; the
window is the only place a time zone enters, and the row says which one it
used (`timezone`) so the console can display in it.

"Last week" is the same window shifted back exactly seven days: the same
weekday, the same hours, which is the only comparison that means anything
for a business with a weekly rhythm.

MONEY
-----
Per currency, in integer minor units, never summed across currencies.
`charged_fare()` (`base.money`) is the amount a trip actually charged;
`final_fare_minor` alone is NULL on every trip without a promo and used to
report a GMV of zero.

WHAT IS STATED RATHER THAN ASSUMED
----------------------------------
`metrics.DEFINITIONS` is sent with every response: the acceptance rate
divides by REQUESTS because offers are not persisted; the ETA is
`reached_at − accepted_at`; the online-driver count says whether it came
from Redis presence or the status column.
"""

import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone as dt_timezone
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Min, Q, Sum
from django.utils import timezone

from base.countries import get_country
from base.market_time import zone_timezone
from base.money import charged_fare, normalize_currency

from .metrics import (
    DEFINITIONS, WINDOW_7D, WINDOW_24H, WINDOW_TODAY, OVERVIEW_WINDOWS,
)

logger = logging.getLogger(__name__)

WEEK = timedelta(days=7)
DAY = timedelta(days=1)


# ---------------------------------------------------------------------------
# Cities, their clocks and their currencies
# ---------------------------------------------------------------------------

@dataclass
class CityContext:
    node: object            # admin_access.GeoNode
    country: str            # ISO code, from the path
    tz: ZoneInfo
    currency: str

    @property
    def path(self):
        return self.node.path


def country_code_of(node):
    """`GLOBAL/IN/IN-TG/IN-TG-HYD/` -> `IN`. No parent walk, no query."""
    parts = [p for p in str(node.path).split('/') if p]
    return parts[1] if len(parts) > 1 else None


def city_contexts(cities):
    """Clock and currency for each city, from its active ServiceZone when it
    has one (the zone carries `timezone_name` and `currency`), else the
    country's defaults. One query for the zones, whatever the city count."""
    from servers.pricing.models import ServiceZone

    cities = list(cities)
    zones = {}
    for zone in (ServiceZone.objects.filter(geo_node__in=cities, is_active=True)
                 .order_by('priority', 'id')):
        zones.setdefault(zone.geo_node_id, zone)
    out = OrderedDict()
    for city in cities:
        country = country_code_of(city) or 'IN'
        zone = zones.get(city.id)
        if zone is not None:
            tz = zone_timezone(zone)
            currency = normalize_currency(zone.currency or get_country(country).currency)
        else:
            tz = zone_timezone_for_country(country)
            currency = get_country(country).currency
        out[city.id] = CityContext(node=city, country=country, tz=tz, currency=currency)
    return out


def zone_timezone_for_country(country):
    from base.market_time import _zoneinfo

    return _zoneinfo(get_country(country).default_timezone)


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

def window_bounds(window, now, tz):
    """`(start, end)` for `window`, both aware UTC datetimes."""
    if window not in OVERVIEW_WINDOWS:
        raise ValueError(f'window must be one of {OVERVIEW_WINDOWS}, got {window!r}')
    if window == WINDOW_TODAY:
        local = now.astimezone(tz)
        start = datetime.combine(local.date(), time.min, tzinfo=tz)
        return start.astimezone(dt_timezone.utc), now
    if window == WINDOW_24H:
        return now - DAY, now
    return now - WEEK, now


def last_week(bounds):
    start, end = bounds
    return start - WEEK, end - WEEK


# ---------------------------------------------------------------------------
# Anchors: the same paths the scoped lists use
# ---------------------------------------------------------------------------

def trip_anchor(ctx):
    return Q(zone__geo_node__path__startswith=ctx.path)


def driver_anchor(ctx):
    return Q(home_city__path__startswith=ctx.path)


def withdrawal_anchor(ctx):
    return Q(driver__home_city__path__startswith=ctx.path)


def sos_anchor(ctx):
    return (Q(anchor_node__path__startswith=ctx.path)
            | Q(anchor_node__isnull=True, trip__zone__geo_node__path__startswith=ctx.path))


def ticket_anchor(ctx):
    return (Q(trip_id__zone__geo_node__path__startswith=ctx.path)
            | Q(trip_id__isnull=True, user_id__driver__home_city__path__startswith=ctx.path))


def between(field, bounds):
    start, end = bounds
    return Q(**{f'{field}__gte': start, f'{field}__lt': end})


# ---------------------------------------------------------------------------
# The measurements
# ---------------------------------------------------------------------------

def _aggregate(queryset, contexts, build):
    """One `aggregate()` over `queryset` with every city's figures in it.

    `build(ctx)` returns `{name: Aggregate}` for one city; the names are
    prefixed with the city id so the single query carries them all, and the
    result is handed back as `{city_id: {name: value}}`.
    """
    spec = {}
    for city_id, ctx in contexts.items():
        for name, agg in build(ctx).items():
            spec[f'c{city_id}__{name}'] = agg
    if not spec:
        return {}
    raw = queryset.aggregate(**spec)
    out = {city_id: {} for city_id in contexts}
    for key, value in raw.items():
        prefix, name = key.split('__', 1)
        out[int(prefix[1:])][name] = value
    return out


def _by_currency(queryset, contexts, build):
    """Like `_aggregate`, grouped by currency: `{city_id: {name: {CUR: int}}}`."""
    spec = {}
    for city_id, ctx in contexts.items():
        for name, agg in build(ctx).items():
            spec[f'c{city_id}__{name}'] = agg
    out = {city_id: {} for city_id in contexts}
    if not spec:
        return out
    for row in queryset.values('currency').annotate(**spec).order_by('currency'):
        code = normalize_currency(row.get('currency'))
        for key, value in row.items():
            if key == 'currency' or value in (None, 0):
                continue
            prefix, name = key.split('__', 1)
            out[int(prefix[1:])].setdefault(name, {})[code] = int(value)
    return out


def measure_trips(contexts, bounds_by_city):
    """Requested / accepted / cancelled / unfulfilled by request time,
    completed by completion time, ETA and distinct riders -- per city."""
    from servers.ride.models import Trip

    eta = ExpressionWrapper(F('reached_at') - F('accepted_at'), output_field=DurationField())

    def build(ctx):
        req = trip_anchor(ctx) & between('requested_at', bounds_by_city[ctx.node.id])
        done = trip_anchor(ctx) & between('completed_at', bounds_by_city[ctx.node.id])
        return {
            'requested': Count('id', filter=req),
            'accepted': Count('id', filter=req & Q(accepted_at__isnull=False)),
            'cancelled': Count('id', filter=req & Q(status_id__status_code='cancelled')),
            'unfulfilled': Count('id', filter=req & Q(accepted_at__isnull=True,
                                                     status_id__status_code='cancelled')),
            'completed': Count('id', filter=done & Q(status_id__status_code='completed')),
            'eta_avg': Avg(eta, filter=req & Q(accepted_at__isnull=False, reached_at__isnull=False)),
            'eta_n': Count('id', filter=req & Q(accepted_at__isnull=False, reached_at__isnull=False)),
            'riders': Count('user_id', distinct=True, filter=req),
        }

    return _aggregate(Trip.objects.all(), contexts, build)


def measure_collected(contexts, bounds_by_city):
    """Charged fare of trips completed in the window, per city per currency."""
    from servers.ride.models import Trip

    def build(ctx):
        done = (trip_anchor(ctx) & between('completed_at', bounds_by_city[ctx.node.id])
                & Q(status_id__status_code='completed'))
        return {'collected': Sum(charged_fare(), filter=done)}

    return _by_currency(Trip.objects.all(), contexts, build)


def measure_riders_24h(contexts, now):
    from servers.ride.models import Trip

    def build(ctx):
        return {
            'riders_24h': Count('user_id', distinct=True,
                                filter=trip_anchor(ctx) & between('requested_at', (now - DAY, now))),
            'riders_24h_last_week': Count(
                'user_id', distinct=True,
                filter=trip_anchor(ctx) & between('requested_at', (now - DAY - WEEK, now - WEEK))),
        }

    return _aggregate(Trip.objects.all(), contexts, build)


def online_driver_ids():
    """Driver ids with a live presence heartbeat, or None if presence is
    unreachable (no Redis configured, or it failed). None, not an empty
    set: "nobody is online" and "we cannot tell" must stay different."""
    try:
        from servers.redis_client import get_all_online_drivers, redis_client
    except Exception:  # noqa: BLE001 - presence is optional in local runs
        return None
    if redis_client is None:
        return None
    try:
        return {int(d['driver_id']) for d in get_all_online_drivers()}
    except Exception:  # noqa: BLE001 - a presence failure must not blank the board
        logger.exception('command_centre: presence read failed')
        return None


def measure_drivers(contexts, bounds_by_city, online_ids):
    from servers.driver.models import Driver

    def build(ctx):
        if online_ids is None:
            online = Q(status='online')
        else:
            online = Q(id__in=list(online_ids)) if online_ids else Q(pk__in=[])
        return {
            'online_now': Count('id', filter=driver_anchor(ctx) & online),
            'active': Count('id', distinct=True, filter=driver_anchor(ctx)
                            & between('sessions__started_at', bounds_by_city[ctx.node.id])),
            'kyc_awaiting': Count('id', filter=driver_anchor(ctx) & Q(approved=False)),
        }

    return _aggregate(Driver.objects.all(), contexts, build)


def measure_sos(contexts, bounds_by_city):
    from servers.sos.models import SOSEvent

    def build(ctx):
        return {
            'open': Count('id', filter=sos_anchor(ctx) & Q(status='open')),
            'oldest_open': Min('created_at', filter=sos_anchor(ctx) & Q(status='open')),
            'raised': Count('id', filter=sos_anchor(ctx) & between('created_at', bounds_by_city[ctx.node.id])),
        }

    return _aggregate(SOSEvent.objects.all(), contexts, build)


def measure_tickets(contexts, bounds_by_city):
    from servers.support.models import SupportTicket

    def build(ctx):
        return {
            'open': Count('id', filter=ticket_anchor(ctx) & Q(status='OPEN')),
            'raised': Count('id', filter=ticket_anchor(ctx)
                            & between('created_at', bounds_by_city[ctx.node.id])),
        }

    return _aggregate(SupportTicket.objects.all(), contexts, build)


def measure_withdrawals(contexts, bounds_by_city):
    from servers.driver.models import WithdrawalRequest

    pending = Q(status='pending')

    def build(ctx):
        return {
            'pending': Count('id', filter=withdrawal_anchor(ctx) & pending),
            'oldest_pending': Min('requested_at', filter=withdrawal_anchor(ctx) & pending),
            'requested': Count('id', filter=withdrawal_anchor(ctx)
                               & between('requested_at', bounds_by_city[ctx.node.id])),
        }

    counts = _aggregate(WithdrawalRequest.objects.all(), contexts, build)

    def build_sum(ctx):
        return {'pending_sum': Sum('amount_minor', filter=withdrawal_anchor(ctx) & pending)}

    sums = _by_currency(WithdrawalRequest.objects.all(), contexts, build_sum)
    for city_id in counts:
        counts[city_id]['pending_sum'] = sums.get(city_id, {}).get('pending_sum', {})
    return counts


def measure_alerts(contexts):
    from .models import OperatorAlert

    def build(ctx):
        return {'open': Count('id', filter=Q(city__path__startswith=ctx.path,
                                             acknowledged_at__isnull=True))}

    return _aggregate(OperatorAlert.objects.all(), contexts, build)


# ---------------------------------------------------------------------------
# Rates, with honest zeros
# ---------------------------------------------------------------------------

def rate(numerator, denominator):
    """A percentage with one decimal, or None when there is nothing to divide
    by. None, not 0: "no requests" is not "0 % unfulfilled"."""
    if not denominator:
        return None
    return float((Decimal(numerator) * 100 / Decimal(denominator))
                 .quantize(Decimal('0.1'), rounding=ROUND_HALF_UP))


def minutes(delta):
    if delta is None:
        return None
    if isinstance(delta, timedelta):
        seconds = delta.total_seconds()
    else:
        # SQLite hands back microseconds for a duration aggregate.
        seconds = float(delta) / 1_000_000
    return float(Decimal(seconds / 60).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP))


def age_minutes(stamp, now):
    if stamp is None:
        return 0.0
    return float(Decimal((now - stamp).total_seconds() / 60).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP))


def age_hours(stamp, now):
    if stamp is None:
        return 0.0
    return float(Decimal((now - stamp).total_seconds() / 3600).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP))


def rates_from(trips):
    """The three brief metrics from a `measure_trips` row."""
    requested = trips.get('requested', 0)
    return {
        'unfulfilled_rate': rate(trips.get('unfulfilled', 0), requested),
        'acceptance_rate': rate(trips.get('accepted', 0), requested),
        'avg_eta_minutes': minutes(trips.get('eta_avg')) if trips.get('eta_n') else None,
    }


# ---------------------------------------------------------------------------
# Putting a row together
# ---------------------------------------------------------------------------

def measure(contexts, window, now=None, *, previous=True, online_ids=None,
            presence_checked=False):
    """Every figure for every city in `contexts`, as `{city_id: row}`.

    `previous=True` adds the last-week counterpart to each windowed figure.
    `online_ids` is the presence set (see `online_driver_ids`); callers that
    have not looked it up pass `presence_checked=False` and it is read here.
    """
    now = now or timezone.now()
    if not presence_checked:
        online_ids = online_driver_ids()
    online_source = 'db' if online_ids is None else 'presence'

    bounds = {cid: window_bounds(window, now, ctx.tz) for cid, ctx in contexts.items()}
    trips = measure_trips(contexts, bounds)
    collected = measure_collected(contexts, bounds)
    riders = measure_riders_24h(contexts, now)
    drivers = measure_drivers(contexts, bounds, online_ids)
    sos = measure_sos(contexts, bounds)
    tickets = measure_tickets(contexts, bounds)
    withdrawals = measure_withdrawals(contexts, bounds)
    alerts = measure_alerts(contexts)

    prev = {}
    if previous:
        prev_bounds = {cid: last_week(b) for cid, b in bounds.items()}
        prev = {
            'trips': measure_trips(contexts, prev_bounds),
            'collected': measure_collected(contexts, prev_bounds),
            'drivers': measure_drivers(contexts, prev_bounds, online_ids),
            'sos': measure_sos(contexts, prev_bounds),
            'tickets': measure_tickets(contexts, prev_bounds),
            'withdrawals': measure_withdrawals(contexts, prev_bounds),
        }

    rows = OrderedDict()
    for cid, ctx in contexts.items():
        t, pt = trips.get(cid, {}), prev.get('trips', {}).get(cid, {})
        d, pd = drivers.get(cid, {}), prev.get('drivers', {}).get(cid, {})
        s, ps = sos.get(cid, {}), prev.get('sos', {}).get(cid, {})
        k, pk = tickets.get(cid, {}), prev.get('tickets', {}).get(cid, {})
        w, pw = withdrawals.get(cid, {}), prev.get('withdrawals', {}).get(cid, {})
        r = riders.get(cid, {})
        now_rates, prev_rates = rates_from(t), rates_from(pt)
        start, end = bounds[cid]

        def pair(now_value, then_value, has_previous=previous):
            return {'now': now_value, 'last_week': then_value if has_previous else None}

        rows[cid] = {
            'code': ctx.node.code,
            'name': ctx.node.name,
            'status': ctx.node.status,
            'country': ctx.country,
            'currency': ctx.currency,
            'timezone': str(ctx.tz),
            'window': {
                'key': window,
                'start': start.isoformat(),
                'end': end.isoformat(),
                'last_week_start': (start - WEEK).isoformat(),
                'last_week_end': (end - WEEK).isoformat(),
            },
            'trips': {
                'requested': pair(t.get('requested', 0), pt.get('requested', 0)),
                'accepted': pair(t.get('accepted', 0), pt.get('accepted', 0)),
                'completed': pair(t.get('completed', 0), pt.get('completed', 0)),
                'cancelled': pair(t.get('cancelled', 0), pt.get('cancelled', 0)),
                'unfulfilled': pair(t.get('unfulfilled', 0), pt.get('unfulfilled', 0)),
            },
            'rates': {
                key: pair(now_rates[key], prev_rates[key]) for key in now_rates
            },
            'money': {
                'collected': _money_pairs(collected.get(cid, {}).get('collected', {}),
                                          prev.get('collected', {}).get(cid, {}).get('collected', {}),
                                          previous),
                'withdrawals_pending_sum': _money_pairs(w.get('pending_sum', {}), {}, False),
            },
            'drivers': {
                'online_now': pair(d.get('online_now', 0), None, False),
                'online_source': online_source,
                'active': pair(d.get('active', 0), pd.get('active', 0)),
            },
            'riders': {
                'active_24h': pair(r.get('riders_24h', 0), r.get('riders_24h_last_week', 0)),
            },
            'queues': {
                'sos_open': pair(s.get('open', 0), None, False),
                'sos_oldest_open_minutes': age_minutes(s.get('oldest_open'), now) if s.get('open') else 0.0,
                'sos_raised': pair(s.get('raised', 0), ps.get('raised', 0)),
                'tickets_open': pair(k.get('open', 0), None, False),
                'tickets_raised': pair(k.get('raised', 0), pk.get('raised', 0)),
                'withdrawals_pending': pair(w.get('pending', 0), None, False),
                'withdrawals_oldest_pending_hours': (
                    age_hours(w.get('oldest_pending'), now) if w.get('pending') else 0.0),
                'withdrawals_requested': pair(w.get('requested', 0), pw.get('requested', 0)),
                'kyc_awaiting': pair(d.get('kyc_awaiting', 0), None, False),
                'alerts_open': pair(alerts.get(cid, {}).get('open', 0), None, False),
            },
        }
    return rows


def _money_pairs(now_by_currency, then_by_currency, previous):
    """`{CUR: {now_minor, last_week_minor}}` -- one entry per currency seen
    in either window, never a sum across them."""
    out = OrderedDict()
    for code in sorted(set(now_by_currency) | set(then_by_currency)):
        out[code] = {
            'now_minor': int(now_by_currency.get(code, 0)),
            'last_week_minor': int(then_by_currency.get(code, 0)) if previous else None,
        }
    return out


def strip_money(row):
    """The row without its money, for a caller who holds no `finance.read`.
    Keys are removed, not zeroed: a zero is a number somebody acts on."""
    row = dict(row)
    row.pop('money', None)
    queues = dict(row.get('queues', {}))
    queues.pop('withdrawals_pending', None)
    queues.pop('withdrawals_oldest_pending_hours', None)
    queues.pop('withdrawals_requested', None)
    row['queues'] = queues
    return row


def build_overview(cities, window, *, now=None, include_money=True, scope_code=None):
    """The endpoint's payload for `cities` (already scoped by the caller)."""
    now = now or timezone.now()
    contexts = city_contexts(cities)
    rows = measure(contexts, window, now)
    cities_out = [row if include_money else strip_money(row) for row in rows.values()]
    return {
        'window': window,
        'generated_at': now.isoformat(),
        'scope': scope_code,
        'money_visible': include_money,
        'definitions': DEFINITIONS,
        'cities': cities_out,
    }


def has_data(row):
    """Does a paused city have anything worth a row?"""
    t = row['trips']
    return any(
        (t[k]['now'] or 0) + (t[k]['last_week'] or 0) for k in t
    ) or (row['drivers']['online_now']['now'] or 0) or (row['queues']['sos_open']['now'] or 0)
