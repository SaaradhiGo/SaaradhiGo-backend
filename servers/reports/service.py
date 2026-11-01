"""Reading the rollups: scope, parameters and the series each report returns.

Everything here READS `models.py`; nothing writes. Rows are folded in Python from the daily
rollup rows (a few thousand at most for a 400-day range), which keeps the rule for a period, a
group and a currency in one place that is easy to test.

CURRENCY
--------
A money report keeps `currency` in every row and every total. Nothing adds across currencies.
A count report (trips, riders) adds counts across cities freely: a trip is a trip.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Max, Q

from servers.admin_access.bridge import granted
from servers.admin_access.models import CITY, GeoNode
from servers.payments.scope import paths_for

from . import catalog
from .catalog import GRANULARITIES, MAX_RANGE_DAYS, REPORT_DOOR
from .models import (
    DailyCityMetrics, DailyDriverSupply, DailyFraud, DailyPayouts, DailyRiderMetrics, DailySafety,
    DailySupport, RebuildLog, WeeklyRiderCohort,
)

UNATTRIBUTED = 'unattributed'


class ParamError(Exception):
    """A query parameter that cannot be used (the view answers 400)."""

    def __init__(self, field, message):
        super().__init__(message)
        self.field = field
        self.message = message


class NotInScope(Exception):
    """An explicit city the caller does not hold, or that does not exist (the view answers 404)."""


# ---------------------------------------------------------------------------
# Who may open what, and where
# ---------------------------------------------------------------------------

def holds_any(user, permissions):
    return any(granted(user, p)[0] for p in permissions)


def may_open(user, report):
    """The door (`report.read`) plus the report's own gate, each held somewhere."""
    return granted(user, REPORT_DOOR)[0] and holds_any(user, report.gate)


def _intersect(a, b):
    if a is None:
        return b
    if b is None:
        return a
    out = set()
    for pa in a:
        for pb in b:
            if pa.startswith(pb):
                out.add(pa)
            elif pb.startswith(pa):
                out.add(pb)
    return sorted(out)


def _union(lists):
    if any(item is None for item in lists):
        return None
    return sorted({p for item in lists for p in item})


def allowed_paths(user, report):
    """Path prefixes the caller may see this report in, or None for everywhere.

    `report.read` must be held at the place AND one of the report's gate capabilities, so an
    operator with Finance in one city and Support in another gets the money of the first only.
    """
    door = paths_for(user, REPORT_DOOR)
    held = [p for p in report.gate if granted(user, p)[0]]
    gate = _union([paths_for(user, p) for p in held]) if held else []
    return _intersect(door, gate)


def cities_in(paths):
    qs = GeoNode.objects.filter(kind=CITY)
    if paths is not None:
        covered = Q(pk__in=[])
        for path in paths:
            covered |= Q(path__startswith=path)
        qs = qs.filter(covered)
    return list(qs.order_by('path'))


def resolve_cities(user, report, city_code):
    """`(cities, node)`: the cities the report covers for this caller, narrowed by `?city=`.

    An explicit code the caller does not hold (or that does not exist) raises NotInScope, which
    is a 404, never a quietly wider answer.
    """
    paths = allowed_paths(user, report)
    cities = cities_in(paths)
    if not city_code:
        return cities, None
    node = GeoNode.objects.filter(code=city_code).first()
    if node is None or (paths is not None and not any(node.path.startswith(p) for p in paths)):
        raise NotInScope(city_code)
    return [c for c in cities if c.path.startswith(node.path)], node


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

@dataclass
class Params:
    d_from: date
    d_to: date
    granularity: str
    city: str
    vehicle: str
    group_by: str

    def as_dict(self):
        return {'from': self.d_from.isoformat(), 'to': self.d_to.isoformat(),
                'granularity': self.granularity, 'city': self.city, 'vehicle': self.vehicle,
                'group_by': self.group_by}


def validate_range(d_from, d_to):
    if d_to < d_from:
        raise ParamError('to', 'to must not be before from')
    if (d_to - d_from).days + 1 > MAX_RANGE_DAYS:
        raise ParamError('to', f'The range is limited to {MAX_RANGE_DAYS} days')


def validate_granularity(value):
    value = (value or 'day').strip().lower()
    if value not in GRANULARITIES:
        raise ParamError('granularity', f'granularity must be one of: {", ".join(GRANULARITIES)}')
    return value


def period_start(day, granularity):
    """The first day of the day / ISO week (Monday) / month that holds `day`."""
    if granularity == 'week':
        return day - timedelta(days=day.weekday())
    if granularity == 'month':
        return day.replace(day=1)
    return day


def _date(raw, name):
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ParamError(name, f'{name} must be a date like 2026-03-04') from None


def parse_params(query, report, default_to):
    """`Params` from the query string, validated. `default_to` is the default end date."""
    raw_from, raw_to = (query.get('from') or '').strip(), (query.get('to') or '').strip()
    d_to = _date(raw_to, 'to') if raw_to else default_to
    d_from = _date(raw_from, 'from') if raw_from else d_to - timedelta(days=catalog.DEFAULT_RANGE_DAYS - 1)
    validate_range(d_from, d_to)
    group = (query.get('group_by') or report.default_group).strip().lower()
    if group not in report.groups:
        raise ParamError('group_by', f'group_by must be one of: {", ".join(report.groups)}')
    return Params(d_from, d_to, validate_granularity(query.get('granularity')),
                  (query.get('city') or '').strip(), (query.get('vehicle') or '').strip(), group)


def default_to_date(cities):
    """The default end of a range: today, in the latest-running city's clock."""
    from django.utils import timezone

    from servers.command_centre.overview import city_contexts

    now = timezone.now()
    if not cities:
        return now.date()
    return max(now.astimezone(ctx.tz).date() for ctx in city_contexts(cities).values())


# ---------------------------------------------------------------------------
# Folding rollup rows into a series
# ---------------------------------------------------------------------------

def _pct(numerator, denominator):
    if not denominator:
        return None
    return float((Decimal(numerator) * 100 / Decimal(denominator)).quantize(Decimal('0.1')))


def _avg(total, count):
    return int(round(total / count)) if count else None


def _fold(records, sums, params, *, currency, city_codes, group_value):
    """`{(period, group, currency): {col: sum, '_n': rows folded}}` over `records`."""
    acc = {}
    for rec in records:
        key = (period_start(rec['date'], params.granularity).isoformat(),
               group_value(rec), rec['currency'] if currency else None)
        slot = acc.get(key)
        if slot is None:
            slot = acc[key] = {c: 0 for c in sums}
            slot['_n'] = 0
        for c in sums:
            slot[c] += rec[c] or 0
        slot['_n'] += 1
    return acc


def _group_getter(params, report):
    if params.group_by == 'city':
        return lambda rec: rec['city_code']
    if params.group_by == 'vehicle' and 'vehicle' in report.groups:
        return lambda rec: rec.get('vehicle_type') or UNATTRIBUTED
    return lambda rec: None


def _finish(acc, params, report, derive, *, currency):
    """Rows (sorted) and totals from the folded accumulator."""
    rows = []
    for (period, group, cur), slot in sorted(
            acc.items(), key=lambda kv: (kv[0][0], kv[0][1] or '', kv[0][2] or '')):
        row = {'period': period, **{k: v for k, v in slot.items() if k != '_n'}}
        if params.group_by == 'city':
            row['city'] = group
        elif group is not None:
            row['vehicle'] = group
        if currency:
            row['currency'] = cur
        derive(row, slot['_n'], True)
        rows.append(row)

    totals = {}
    sums = [k for k in (next(iter(acc.values())) if acc else {}) if k != '_n']
    for (_period, _group, cur), slot in acc.items():
        t = totals.setdefault(cur, {c: 0 for c in sums} | {'_n': 0})
        for c in sums:
            t[c] += slot[c]
        t['_n'] += slot['_n']
    out = []
    for cur, t in sorted(totals.items(), key=lambda kv: kv[0] or ''):
        row = {k: v for k, v in t.items() if k != '_n'}
        if currency:
            row['currency'] = cur
        derive(row, t['_n'], False)
        out.append(row)
    if not out and not currency:
        zero = {c: 0 for c in (report_sums(report))}
        derive(zero, 0, False)
        out.append(zero)
    return rows, out


def report_sums(report):
    return SPECS[report.key]['sums']


# ---------------------------------------------------------------------------
# Derived columns (one function per report: `derive(row, rows_folded, is_series_row)`)
# ---------------------------------------------------------------------------

def _derive_trips(row, n, series):
    row['completion_rate'] = _pct(row['trips_completed'], row['trips_requested'])
    row['acceptance_rate'] = _pct(row['trips_accepted'], row['trips_requested'])
    row['avg_eta_seconds'] = _avg(row['eta_seconds_sum'], row['eta_count'])
    if n != 1 or not row['eta_count']:   # a percentile of one rollup row only
        row['eta_p50_seconds'] = row['eta_p90_seconds'] = None
    row['distance_km'] = float(row['distance_km'])
    row['duration_min'] = float(row['duration_min'])


def _derive_revenue(row, n, series):
    row['take_rate'] = _pct(row['commission_minor'], row['gmv_minor'])
    row['platform_cost_minor'] = (
        row['promo_cost_minor'] + row['platform_funded_trip_end_minor']
        + row['regulatory_fee_expense_minor'] + row['incentive_rewards_minor']
        + row['referral_rewards_minor'] - row['reward_recovery_minor'])
    row['net_take_minor'] = row['commission_minor'] - row['platform_cost_minor']
    row['avg_fare_minor'] = (int(round(row['rider_paid_minor'] / row['trips_completed']))
                             if row['trips_completed'] else None)
    row.setdefault('gmv_growth_percent', None)


def _derive_promotions(row, n, series):
    row['total_cost_minor'] = (row['promo_cost_minor'] + row['incentive_rewards_minor']
                               + row['referral_rewards_minor'] - row['reward_recovery_minor'])
    row['cost_per_trip_minor'] = (int(round(row['total_cost_minor'] / row['trips_completed']))
                                  if row['trips_completed'] else None)


def _derive_supply(row, n, series):
    row['online_hours'] = round(row['online_seconds'] / 3600, 1)
    row['trips_per_active_driver'] = (round(row['trips_completed'] / row['active_drivers'], 1)
                                      if row['active_drivers'] else None)
    row['utilisation'] = (_pct(row['duration_min'] * 60, row['online_seconds'])
                          if row['online_seconds'] else None)
    row['duration_min'] = float(row['duration_min'])


def _derive_support(row, n, series):
    row['breach_rate'] = _pct(row['first_response_breached'] + row['resolution_breached'],
                              row['opened'] * 2) if row['opened'] else None
    row['avg_first_response_seconds'] = _avg(row['first_response_seconds_sum'],
                                             row['first_response_count'])


def _derive_safety(row, n, series):
    row['sos_avg_response_seconds'] = _avg(row['sos_response_seconds_sum'], row['sos_responded'])
    if n != 1 or not row['sos_responded']:   # a median of one rollup row only
        row['sos_median_response_seconds'] = None


def _derive_fraud(row, n, series):
    decided = row['confirmed'] + row['dismissed']
    row['confirm_rate'] = _pct(row['confirmed'], decided)


def _noop(row, n, series):
    return None


# ---------------------------------------------------------------------------
# Record sources
# ---------------------------------------------------------------------------

def _base(model, cities, params, **extra):
    return model.objects.filter(city__in=cities, date__gte=params.d_from, date__lte=params.d_to, **extra)


def _recs(qs, fields):
    for r in qs.values('date', 'city__code', *fields):
        r['city_code'] = r.pop('city__code')
        yield r


TRIP_SUMS = ('trips_requested', 'trips_accepted', 'trips_completed', 'cancelled_by_rider',
             'cancelled_by_driver', 'cancelled_by_ops', 'no_driver_found', 'distance_km',
             'duration_min', 'eta_count', 'eta_seconds_sum', 'eta_p50_seconds', 'eta_p90_seconds')
REVENUE_SUMS = ('trips_completed', 'settled_trips', 'gmv_minor', 'rider_paid_minor',
                'commission_minor', 'driver_net_minor', 'promo_cost_minor',
                'platform_funded_trip_end_minor', 'regulatory_fee_expense_minor',
                'incentive_rewards_minor', 'referral_rewards_minor', 'reward_recovery_minor',
                'refunds_minor')
PROMO_SUMS = ('trips_completed', 'promo_cost_minor', 'incentive_rewards_minor',
              'referral_rewards_minor', 'reward_recovery_minor')
PAYOUT_SUMS = ('requested_count', 'requested_minor', 'paid_count', 'paid_minor', 'failed_count',
               'failed_minor')
SUPPLY_SUMS = ('active_drivers', 'online_drivers', 'online_seconds', 'new_approved_drivers',
               'trips_requested', 'trips_accepted', 'trips_completed', 'duration_min')
RIDER_SUMS = ('new_riders', 'active_riders', 'first_trip_riders')
SUPPORT_SUMS = ('opened', 'resolved', 'first_response_breached', 'resolution_breached',
                'first_response_count', 'first_response_seconds_sum')
SAFETY_SUMS = ('incidents', 'incidents_critical', 'incidents_high', 'incidents_medium',
               'incidents_low', 'sos_events', 'sos_responded', 'sos_response_seconds_sum')
FRAUD_SUMS = ('raised', 'confirmed', 'dismissed')


def _vehicle_filter(params):
    return {'vehicle_type__iexact': params.vehicle} if params.vehicle else {}


def _trip_records(cities, params):
    qs = _base(DailyCityMetrics, cities, params, **_vehicle_filter(params))
    yield from _recs(qs, ['vehicle_type', 'currency', *TRIP_SUMS])


def _money_records(sums):
    def records(cities, params):
        qs = _base(DailyCityMetrics, cities, params, **_vehicle_filter(params))
        yield from _recs(qs, ['vehicle_type', 'currency', *sums])
    return records


def _payout_records(cities, params):
    yield from _recs(_base(DailyPayouts, cities, params), ['currency', *PAYOUT_SUMS])


def _supply_records(cities, params):
    """Driver supply joined with that city-day's trip counts."""
    trips = defaultdict(lambda: defaultdict(int))
    for r in _recs(_base(DailyCityMetrics, cities, params),
                   ['trips_requested', 'trips_accepted', 'trips_completed', 'duration_min']):
        slot = trips[(r['city_code'], r['date'])]
        for c in ('trips_requested', 'trips_accepted', 'trips_completed'):
            slot[c] += r[c]
        slot['duration_min'] += r['duration_min'] or 0
    seen = set()
    for r in _recs(_base(DailyDriverSupply, cities, params),
                   ['active_drivers', 'online_drivers', 'online_seconds', 'new_approved_drivers']):
        key = (r['city_code'], r['date'])
        seen.add(key)
        r.update({c: trips[key][c] for c in ('trips_requested', 'trips_accepted',
                                             'trips_completed', 'duration_min')})
        yield r
    for key, slot in trips.items():          # a city-day with trips and no driver rollup row
        if key not in seen:
            yield {'date': key[1], 'city_code': key[0], 'active_drivers': 0, 'online_drivers': 0,
                   'online_seconds': 0, 'new_approved_drivers': 0, **slot}


def _rider_records(cities, params):
    yield from _recs(_base(DailyRiderMetrics, cities, params), RIDER_SUMS)


def _support_records(cities, params):
    yield from _recs(_base(DailySupport, cities, params), SUPPORT_SUMS)


def _safety_records(cities, params):
    acc = defaultdict(lambda: {c: 0 for c in SAFETY_SUMS} | {'sos_median_response_seconds': None})
    for r in _recs(_base(DailySafety, cities, params),
                   ['source', 'kind', 'severity', 'count', 'responded_count',
                    'response_seconds_sum', 'response_median_seconds']):
        slot = acc[(r['date'], r['city_code'])]
        if r['source'] == DailySafety.SOURCE_INCIDENT:
            slot['incidents'] += r['count']
            if r['severity'] in ('critical', 'high', 'medium', 'low'):
                slot[f'incidents_{r["severity"]}'] += r['count']
        elif r['kind'] == '*':
            slot['sos_events'] += r['count']
            slot['sos_responded'] += r['responded_count']
            slot['sos_response_seconds_sum'] += r['response_seconds_sum']
            slot['sos_median_response_seconds'] = r['response_median_seconds']
    for (day, city_code), slot in acc.items():
        yield {'date': day, 'city_code': city_code, **slot}


def _fraud_records(cities, params):
    yield from _recs(_base(DailyFraud, cities, params), ['kind', *FRAUD_SUMS])


SPECS = {
    'trips': {'sums': TRIP_SUMS, 'records': _trip_records, 'derive': _derive_trips, 'currency': False},
    'revenue': {'sums': REVENUE_SUMS, 'records': _money_records(REVENUE_SUMS),
                'derive': _derive_revenue, 'currency': True},
    'promotions_cost': {'sums': PROMO_SUMS, 'records': _money_records(PROMO_SUMS),
                        'derive': _derive_promotions, 'currency': True},
    'payouts': {'sums': PAYOUT_SUMS, 'records': _payout_records, 'derive': _noop, 'currency': True},
    'supply': {'sums': SUPPLY_SUMS, 'records': _supply_records, 'derive': _derive_supply,
               'currency': False},
    'riders': {'sums': RIDER_SUMS, 'records': _rider_records, 'derive': _noop, 'currency': False},
    'support': {'sums': SUPPORT_SUMS, 'records': _support_records, 'derive': _derive_support,
                'currency': False},
    'safety': {'sums': SAFETY_SUMS + ('sos_median_response_seconds',), 'records': _safety_records,
               'derive': _derive_safety, 'currency': False},
    'fraud': {'sums': FRAUD_SUMS, 'records': _fraud_records, 'derive': _derive_fraud,
              'currency': False},
}


# ---------------------------------------------------------------------------
# One report
# ---------------------------------------------------------------------------

def _growth(rows):
    """GMV change against the previous period, per currency, for an ungrouped series."""
    last = {}
    for row in rows:
        cur = row.get('currency')
        before = last.get(cur)
        row['gmv_growth_percent'] = (_pct((row['gmv_minor'] - before) , before)
                                     if before else None)
        last[cur] = row['gmv_minor']


def _funnel(total):
    return [
        {'stage': 'requested', 'count': total.get('trips_requested', 0)},
        {'stage': 'accepted', 'count': total.get('trips_accepted', 0)},
        {'stage': 'completed', 'count': total.get('trips_completed', 0)},
    ]


def _breakdowns(report, cities, params):
    """Category splits shown beside the series (counts only)."""
    out = {}
    if report.key == 'safety':
        cats, sos = defaultdict(int), defaultdict(int)
        for r in _base(DailySafety, cities, params).values('source', 'kind', 'count'):
            if r['source'] == DailySafety.SOURCE_INCIDENT:
                cats[r['kind']] += r['count']
            elif r['kind'] != '*':
                sos[r['kind']] += r['count']
        out['incident_categories'] = [{'kind': k, 'count': v} for k, v in sorted(cats.items())]
        out['sos_types'] = [{'kind': k, 'count': v} for k, v in sorted(sos.items())]
    elif report.key == 'fraud':
        kinds = defaultdict(lambda: {'raised': 0, 'confirmed': 0, 'dismissed': 0})
        for r in _base(DailyFraud, cities, params).values('kind', 'raised', 'confirmed', 'dismissed'):
            for c in ('raised', 'confirmed', 'dismissed'):
                kinds[r['kind']][c] += r[c]
        out['kinds'] = [{'kind': k, **v} for k, v in sorted(kinds.items())]
    return out


def cohort_table(cities, params):
    """`[{week, size, retained: [n...], percent: [p...]}]` summed over the cities."""
    sizes, kept = {}, defaultdict(dict)
    for r in (WeeklyRiderCohort.objects.filter(
            city__in=cities, cohort_week__gte=period_start(params.d_from, 'week'),
            cohort_week__lte=params.d_to)
            .values('cohort_week', 'weeks_since', 'cohort_size', 'retained')):
        week = r['cohort_week']
        if r['weeks_since'] == 0:
            sizes[week] = sizes.get(week, 0) + r['cohort_size']
        kept[week][r['weeks_since']] = kept[week].get(r['weeks_since'], 0) + r['retained']
    table = []
    for week in sorted(sizes):
        weeks = max(kept[week]) + 1 if kept[week] else 0
        retained = [kept[week].get(k, 0) for k in range(weeks)]
        table.append({'week': week.isoformat(), 'size': sizes[week], 'retained': retained,
                      'percent': [_pct(n, sizes[week]) for n in retained]})
    return table


def build(report, cities, params):
    """The report body for `cities` and `params`: columns, rows, totals, extras."""
    if report.key == 'rider_retention':
        table = cohort_table(cities, params)
        return {'columns': [], 'rows': [], 'totals': [], 'cohorts': table, 'extra': {}}
    spec = SPECS[report.key]
    sums = spec['sums']
    acc = _fold(spec['records'](cities, params), sums, params, currency=spec['currency'],
                city_codes=None, group_value=_group_getter(params, report))
    rows, totals = _finish(acc, params, report, spec['derive'], currency=spec['currency'])
    if report.key == 'revenue' and params.group_by == 'none':
        _growth(rows)
    extra = _breakdowns(report, cities, params)
    if report.key in ('supply', 'trips'):
        total = totals[0] if totals else {}
        extra['funnel'] = _funnel(total)
    return {'columns': list(report.columns), 'rows': rows, 'totals': totals, 'extra': extra}


def as_of(cities, params):
    """When the rollups this answer reads were last rebuilt (None if never)."""
    row = RebuildLog.objects.filter(
        city__in=cities, date__gte=params.d_from, date__lte=params.d_to).aggregate(m=Max('built_at'))
    return row['m']


def vehicle_types(cities):
    return sorted({v for v in DailyCityMetrics.objects.filter(city__in=cities)
                   .exclude(vehicle_type='').values_list('vehicle_type', flat=True).distinct()})
