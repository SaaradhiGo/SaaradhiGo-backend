"""The console's Edit pattern for settings (owner brief 25 Nov), driver fee plans here.

Edit on a plan in force (or on a draft that came back) proposes a NEW version with the changed
values. Nothing changes until a second person approves; the version in force stays until then.
Before sending, the editor sees the impact (driver take-home and SaaradhiGo revenue at 5, 12 and
20 trips a day, per class, today against the change) and the first day it would apply. A fee
increase never starts before the market's driver notice (7 days). The approval request carries
the same before/after and impact (`payload.display`), so the checker sees what the maker saw.
"""

from datetime import date, timedelta

from servers.approvals.service import InvalidRequest

from . import fee_plans
from .fee_plan_models import ACTIVE, DRAFT, DriverFeePlan

AMOUNT_WORDS = {'per_trip_fee_minor': 'fee a trip', 'daily_pass_minor': 'daily fee',
                'fee_free_after_trips': 'free after (trips a day)', 'commission_percent': 'commission %',
                'rider_platform_fee_minor': 'rider booking fee'}
MONEY_KEYS = {'per_trip_fee_minor', 'daily_pass_minor', 'rider_platform_fee_minor'}
TYPE_WORDS = {'daily_pass': 'Daily fee', 'percent_commission': 'Commission', 'fair_share': 'Per-trip fee (Fair Share)',
              'per_trip_fee': 'Per-trip fee'}


def class_word(vt):
    from servers.approvals.describe import CLASS_WORDS

    return CLASS_WORDS.get(vt, str(vt).title())


def offered_classes(country, scope_code=''):
    """The classes this market sells (market rules), never another market's."""
    from servers.driver.models import VehicleType

    from .regulation import market_rules

    parts = (scope_code or '').split('-')
    state = parts[1] if len(parts) > 1 else ''
    listed = market_rules(country, state).get('vehicle_classes')
    return list(listed) if listed else sorted(VehicleType.objects.values_list('type', flat=True))


def _money(minor, currency):
    return '—' if minor is None else fee_plans._money(minor, currency)


def _amount_text(key, value, currency):
    if value is None:
        return 'not set'
    if key in MONEY_KEYS:
        return _money(value, currency)
    return f'{value}%' if key == 'commission_percent' else str(value)


def changes(old, new):
    """[{label, from, to}] in words, for every field the edit changes."""
    cur = new.currency
    out = []
    if old is None:
        return [{'label': 'Plan', 'from': 'none', 'to': TYPE_WORDS.get(new.plan_type, new.plan_type)}]
    if old.plan_type != new.plan_type:
        out.append({'label': 'Plan type', 'from': TYPE_WORDS.get(old.plan_type, old.plan_type),
                    'to': TYPE_WORDS.get(new.plan_type, new.plan_type)})
    for vt in sorted(set(old.amounts or {}) | set(new.amounts or {}), key=lambda k: (k == '*', k)):
        a, b = (old.amounts or {}).get(vt) or {}, (new.amounts or {}).get(vt) or {}
        for key in AMOUNT_WORDS:
            if a.get(key) != b.get(key):
                who = 'Every other class' if vt == '*' else class_word(vt)
                out.append({'label': f'{who}: {AMOUNT_WORDS[key]}', 'from': _amount_text(key, a.get(key), cur),
                            'to': _amount_text(key, b.get(key), cur)})
    simple = (('earnings_floor_minor', 'Earnings floor a day', True), ('daily_cap_minor', 'Daily cap', True),
              ('waiver_first_days', 'New drivers free for (days)', False), ('note', 'Note', False))
    for field, label, money in simple:
        x, y = getattr(old, field), getattr(new, field)
        if x != y:
            def fmt(v, money=money):
                if v in (None, ''):
                    return 'none'
                return _money(v, cur) if money else str(v)
            out.append({'label': label, 'from': fmt(x), 'to': fmt(y)})
    return out


def _fare_for(country, vt):
    from .fee_plan_views import average_fare_minor

    return (average_fare_minor(country, vt) or fee_plans.normal_trip_fare_minor(country, vt)
            or (1500 if country == 'US' else 15000))


def impact(old, new, classes):
    """Per class at 5, 12 and 20 trips a day: driver take-home and SaaradhiGo revenue, today against
    the change, on the class's average fare (last 30 days, else a normal 5 km trip)."""
    rows = []
    cur = new.currency
    for vt in classes:
        fare = _fare_for(new.country, vt)
        for n in fee_plans.PRESETS.values():
            after = fee_plans.simulate(new, vt, n, fare)
            before = fee_plans.simulate(old, vt, n, fare) if old is not None else None
            rows.append({'class': class_word(vt), 'vehicle_type': vt, 'trips_per_day': n,
                         'fare_minor': fare,
                         'home_before_minor': before['driver_takes_home_day_minor'] if before else None,
                         'home_after_minor': after['driver_takes_home_day_minor'],
                         'revenue_before_minor': before['driver_pays_day_minor'] if before else None,
                         'revenue_after_minor': after['driver_pays_day_minor']})
    table = [[r['class'], f"{r['trips_per_day']} trips", _money(r['home_before_minor'], cur),
              _money(r['home_after_minor'], cur), _money(r['revenue_before_minor'], cur),
              _money(r['revenue_after_minor'], cur)] for r in rows]
    return {'rows': rows, 'currency': cur,
            'table': {'title': 'A driver’s day, today against the change',
                      'columns': ['Class', 'Day', 'Driver takes home today', 'After the change',
                                  'SaaradhiGo gets today', 'After the change'],
                      'rows': table,
                      'note': 'On each class’s average fare (last 30 days, else a normal 5 km trip).'}}


def is_increase(old, new, classes):
    for vt in classes:
        fare = _fare_for(new.country, vt)
        for n in fee_plans.PRESETS.values():
            before = fee_plans.simulate(old, vt, n, fare)['driver_pays_day_minor'] if old is not None else 0
            if fee_plans.simulate(new, vt, n, fare)['driver_pays_day_minor'] > before:
                return True
    return False


def start(new, old, classes, requested=None):
    """The first local day it applies: the next local day by default, never before the driver
    notice for an increase. A chosen date earlier than that is refused (shown and enforced)."""
    from .commission import notice_days

    today = fee_plans.local_day(new.country)
    earliest = today + timedelta(days=1)
    increase = is_increase(old, new, classes)
    days = notice_days(new.country) if increase else 0
    if increase:
        earliest = max(earliest, today + timedelta(days=days))
    if requested:
        try:
            chosen = date.fromisoformat(str(requested)[:10])
        except ValueError:
            raise InvalidRequest('The start date is YYYY-MM-DD.', code='INVALID_DATE', field='effective_from')
        if chosen < earliest:
            why = (f'This raises what drivers pay, so drivers get {days} days’ notice: the earliest start is '
                   f'{earliest.isoformat()}.' if increase else f'The earliest start is {earliest.isoformat()}.')
            raise InvalidRequest(why, code='NOTICE_PERIOD', field='effective_from')
        earliest = chosen
    return {'effective_from': earliest.isoformat(), 'increase': increase, 'notice_days': days,
            'today': today.isoformat()}


def plan_classes(plan, base=None):
    offered = offered_classes(plan.country, plan.scope_code)
    named = set((plan.amounts or {}).keys()) | set(((base.amounts if base else None) or {}).keys())
    if '*' in named:
        return offered
    return [c for c in offered if c in named] or offered


def proposed(base, data):
    """An unsaved plan: `base` with the edit's values on top (validated like a draft)."""
    from .fee_plan_workflow import _clean, plan_dict

    values = _clean({**plan_dict(base), **{k: v for k, v in (data or {}).items() if k != 'reason'}})
    plan = DriverFeePlan(country=base.country, scope_code=base.scope_code, version=base.version + 1,
                         status=DRAFT, **values)
    from base.countries import get_country

    plan.currency = get_country(plan.country).currency
    return plan


def in_force(base):
    """The plan in force for the base's place (the comparison for the impact), or None."""
    if base.status == ACTIVE:
        return base
    return (DriverFeePlan.objects.filter(country=base.country, scope_code=base.scope_code, status=ACTIVE)
            .order_by('-version').first())


def preview(base, data):
    new = proposed(base, data)
    old = in_force(base)
    classes = plan_classes(new, old)
    when = start(new, old, classes, (data or {}).get('effective_from'))
    return new, old, {'changes': changes(old, new), 'impact': impact(old, new, classes), 'start': when}


def display(preview_body):
    """The approval request's copy of what the editor saw."""
    s = preview_body['start']
    starts = f"Starts {s['effective_from']} (local day)."
    if s['increase']:
        starts += f" Drivers pay more, so they are told {s['notice_days']} days ahead."
    return {'changes': preview_body['changes'], 'impact': preview_body['impact']['table'], 'effective': starts}
