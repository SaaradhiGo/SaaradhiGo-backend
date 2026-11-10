"""What Maps costs, and the monthly budget guard (9 Nov).

Cost is metered calls x the list price per SKU (`MapsSkuPrice`, USD per 1,000, editable in the
console), after the SKU's free monthly calls. The month's spend per country is checked hourly
(`check_budgets`): at `amber_percent` a warning alert, at `red_percent` a critical one, and the
degrade steps switched on in `MapsBudget` apply, in order:

    1  longer route cache (route_cache.ttl_seconds doubles)
    2  straight-line ETA only (eta.compute never reroutes)
    3  autocomplete only after 5 characters (app-config)

Never block booking or SOS: every step only spends fewer calls on the same screens.
"""

from datetime import date
from decimal import Decimal

from django.core.cache import cache
from django.db.models import Sum

LEVEL_KEY = 'maps:budget:level:{country}'
ALERT_KIND = 'platform.maps_budget'


def prices():
    from servers.ride.maps_models import MapsSkuPrice

    return {p.sku: p for p in MapsSkuPrice.objects.all()}


def cost_usd(counts, month_counts=None):
    """USD for `{sku: calls}`. With `month_counts` (the month so far, before these calls), the
    free monthly calls are used up first."""
    table, total = prices(), Decimal('0')
    for sku, n in counts.items():
        p = table.get(sku)
        if p is None:
            continue
        billable = n
        if month_counts is not None:
            already = month_counts.get(sku, 0)
            billable = max(0, n - max(0, p.free_monthly_calls - already))
        total += Decimal(billable) * p.usd_per_1000 / Decimal(1000)
    return total.quantize(Decimal('0.0001'))


def list_cost_usd(counts):
    """At list price, ignoring the free calls (what a trip costs at scale)."""
    return cost_usd(counts)


def counts(start, end=None, country=None, trip_id=None):
    from servers.ride.maps_models import MapsUsage

    qs = MapsUsage.objects.filter(day__gte=start)
    if end:
        qs = qs.filter(day__lte=end)
    if country:
        qs = qs.filter(country__in=[country, ''])
    if trip_id is not None:
        qs = qs.filter(trip_id=trip_id)
    return {r['sku']: r['n'] for r in qs.values('sku').annotate(n=Sum('count'))}


def month_summary(country=None, today=None):
    today = today or date.today()
    start = today.replace(day=1)
    month = counts(start, country=country)
    day = counts(today, today, country=country)
    hits, misses = month.get('route_cache_hit', 0), month.get('route_cache_miss', 0)
    table = prices()
    return {
        'month_start': start.isoformat(),
        'today': day, 'month': month,
        'today_cost_usd': str(cost_usd(day)),
        'month_cost_usd': str(cost_usd(month, month_counts={})),
        'month_list_cost_usd': str(list_cost_usd(month)),
        'route_cache_hit_rate': (round(hits / (hits + misses), 3) if hits + misses else None),
        'free_allowance_used': {sku: {'used': month.get(sku, 0), 'free': p.free_monthly_calls}
                                for sku, p in table.items() if p.free_monthly_calls},
    }


def cost_per_completed_trip(country=None, today=None):
    """The month's metered Maps cost at list price / the month's completed trips."""
    from servers.ride.models import Trip

    today = today or date.today()
    start = today.replace(day=1)
    qs = Trip.objects.filter(status_id__status_code='completed', completed_at__date__gte=start)
    if country:
        qs = qs.filter(zone__country=country)
    n = qs.count()
    if not n:
        return None
    return (list_cost_usd(counts(start, country=country)) / n).quantize(Decimal('0.0001'))


def degrade_level(country):
    """0 within budget; else the number of degrade steps in force (see the module docstring)."""
    try:
        return int(cache.get(LEVEL_KEY.format(country=(country or '').upper())) or 0)
    except Exception:  # noqa: BLE001
        return 0


def check_budgets(today=None):
    """Hourly: alerts and the degrade level per country. Returns {country: {spent, percent, level}}."""
    from servers.command_centre.models import CRITICAL, WARNING
    from servers.command_centre.services import raise_alert
    from servers.ride.maps_models import MapsBudget

    out = {}
    for budget in MapsBudget.objects.exclude(monthly_usd__isnull=True):
        summary = month_summary(budget.country, today)
        spent = Decimal(summary['month_cost_usd'])
        percent = int(spent * 100 / budget.monthly_usd) if budget.monthly_usd else 0
        level = 0
        if percent >= budget.red_percent:
            steps = [budget.degrade_longer_cache, budget.degrade_straight_line_eta,
                     budget.degrade_autocomplete_5_chars]
            # In order: a step applies only when every step before it is on.
            for on in steps:
                if not on:
                    break
                level += 1
        cache.set(LEVEL_KEY.format(country=budget.country), level, 2 * 3600)
        month = summary['month_start']
        if percent >= budget.red_percent:
            raise_alert(ALERT_KIND, severity=CRITICAL,
                        title=f'Maps budget {budget.country}: {percent}% spent; degrade level {level}',
                        payload={'country': budget.country, 'spent_usd': str(spent), 'level': level},
                        source_type='maps_budget', source_id=f'{budget.country}:{month}:red')
        elif percent >= budget.amber_percent:
            raise_alert(ALERT_KIND, severity=WARNING,
                        title=f'Maps budget {budget.country}: {percent}% spent',
                        payload={'country': budget.country, 'spent_usd': str(spent)},
                        source_type='maps_budget', source_id=f'{budget.country}:{month}:amber')
        out[budget.country] = {'spent_usd': str(spent), 'percent': percent, 'level': level}
    return out

