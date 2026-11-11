"""Resolving the commission: the rule for a trip, the snapshot, the matrix, the preview.

This module only READS. Writing a rule (draft, submit, approve, activate) is
`commission_workflow`. The model and the layering are described in
`commission_models`.

THE FALLBACK CHAIN IS GONE
--------------------------
`commission_percent_for_trip` used to try a rate card, then a PlatformSettings
key, then an environment setting, then a literal 18. That chain is why the code
said 18 while the owner ruled 15. Now:

    1. the trip's own snapshot (written at booking),
    2. else the most specific rule effective at the trip's `requested_at`
       (a trip that predates the module, or was booked with no rule),
    3. else `CommissionNotConfigured`. There is no default.

A city's readiness checklist has a `commission` item, so a city cannot go live
without a rule, and this error is what stops a settlement from inventing a rate
in the one place the checklist could not reach.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from base.money import from_minor, to_minor

from .commission_models import (
    APPLICABLE_STATUSES, ACTIVE, PERCENTAGE, CommissionCountryConfig, CommissionRule,
)

logger = logging.getLogger(__name__)

PREVIEW_DAYS = 30


class CommissionNotConfigured(Exception):
    """No commission rule governs this trip. Never defaulted."""

    code = 'COMMISSION_NOT_CONFIGURED'

    def __init__(self, country, detail=''):
        self.country = country
        super().__init__(
            f'No active commission rule for country {country or "unknown"}'
            f'{(" (" + detail + ")") if detail else ""}. Create and approve one under '
            f'Pricing before trips can be settled there.')


@dataclass(frozen=True)
class Decision:
    """What the commission for a trip is, and where it came from."""

    percent: Decimal
    rule_id: int | None
    rule_version: int | None
    source: str  # 'snapshot' | 'rule_at_requested_at'


# ---------------------------------------------------------------------------
# Where a trip sits
# ---------------------------------------------------------------------------

def city_node_of(node):
    """The city GeoNode at or above `node`, or None (a state/country node)."""
    while node is not None:
        if node.kind == 'city':
            return node
        node = node.parent
    return None


def country_code_of_zone(zone):
    """ISO country of a zone: its geo node's country, else the zone's own column."""
    node = getattr(zone, 'geo_node', None)
    if node is not None:
        country = node.country
        if country is not None:
            return country.code
    return str(getattr(zone, 'country', '') or '').upper() or None


def trip_context(trip):
    """`(country, city_node_id, vehicle_type_id)` for a trip."""
    from .services import find_zone_for_point

    zone = getattr(trip, 'zone', None)
    if zone is None:
        zone = find_zone_for_point(trip.pickup_lat, trip.pickup_long)
    country = country_code_of_zone(zone) if zone is not None else None
    city = city_node_of(getattr(zone, 'geo_node', None)) if zone is not None else None
    vt = getattr(trip, 'requested_vehicle_type_id', None)
    if vt is None:
        vehicle = getattr(trip, 'vehicle_id', None)
        vt = getattr(vehicle, 'vehicle_type_id', None)
    return country, (city.id if city else None), vt


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def _effective(qs, at):
    return qs.filter(
        status__in=APPLICABLE_STATUSES, effective_from__lte=at,
    ).filter(Q(effective_to__isnull=True) | Q(effective_to__gt=at))


def _same_key(a, b):
    return (a.country == b.country and a.geo_node_id == b.geo_node_id
            and a.vehicle_type_id == b.vehicle_type_id)


def resolve(country, city_node_id, vehicle_type_id, at=None, *, override=None, rules=None):
    """The most specific rule in force, or None. `override` is an unsaved/draft rule
    treated as in force and replacing its own scope key (the earnings preview).
    `rules` is an optional preloaded list for the country (the matrix), filtered
    here by window, so a big matrix does not issue a query per cell."""
    at = at or timezone.now()
    if not country:
        return None
    if rules is None:
        rules = list(
            _effective(CommissionRule.objects.filter(country=country, kind=PERCENTAGE), at)
            .select_related('geo_node'))
    else:
        rules = [r for r in rules if r.kind == PERCENTAGE and r.applies_at(at)]
    if override is not None:
        rules = [r for r in rules if not _same_key(r, override)] + [override]
    best, best_key = None, None
    for rule in rules:
        node = rule.geo_node
        is_city = node is not None and node.kind == 'city'
        if is_city and node.id != city_node_id:
            continue
        if rule.vehicle_type_id is not None and rule.vehicle_type_id != vehicle_type_id:
            continue
        key = (rule.specificity, rule.effective_from, rule.version)
        if best is None or key > best_key:
            best, best_key = rule, key
    return best


def commission_for_trip(trip) -> Decision:
    """Snapshot first, then the rule effective at `requested_at`, else raise."""
    snap = getattr(trip, 'commission_percent_snapshot', None)
    if snap is not None:
        return Decision(Decimal(str(snap)), getattr(trip, 'commission_rule_id', None),
                        getattr(trip, 'commission_rule_version', None), 'snapshot')
    country, city_id, vt_id = trip_context(trip)
    at = getattr(trip, 'requested_at', None) or timezone.now()
    rule = resolve(country, city_id, vt_id, at)
    if rule is None:
        raise CommissionNotConfigured(country, f'trip {getattr(trip, "id", "?")}')
    return Decision(rule.percent, rule.id, rule.version, 'rule_at_requested_at')


def snapshot_for_booking(zone, vehicle_type_id, at=None):
    """The fields to put on a new Trip: `{commission_percent_snapshot, ...}`.

    Best effort: a booking is never blocked here. With no rule in force the trip is
    created WITHOUT a snapshot and the error is logged loudly; settlement then
    raises `CommissionNotConfigured` (never a silent default) and the city
    readiness checklist is what keeps that from happening in a live city.
    """
    try:
        country = country_code_of_zone(zone) if zone is not None else None
        city = city_node_of(getattr(zone, 'geo_node', None)) if zone is not None else None
        rule = resolve(country, city.id if city else None, vehicle_type_id, at)
    except Exception:  # noqa: BLE001 - a snapshot must never break a booking
        logger.exception('commission snapshot lookup failed')
        return {}
    if rule is None:
        logger.error('commission_not_configured_at_booking', extra={
            'event': 'commission_not_configured_at_booking',
            'zone': getattr(zone, 'code', None)})
        return {}
    return {'commission_percent_snapshot': rule.percent,
            'commission_rule_id': rule.id,
            'commission_rule_version': rule.version}


def country_config(country):
    return CommissionCountryConfig.objects.filter(country=country).first()


def notice_days(country):
    cfg = country_config(country)
    return cfg.notice_days if cfg is not None else 7


def max_percent(country):
    cfg = country_config(country)
    return cfg.max_percent if cfg is not None else None


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------

def next_change(country, city_node_id, vehicle_type_id, now, *, rules):
    """The next scheduled rule (starts after now) that would govern this cell.
    `rules` is the country's preloaded rule list."""
    future = sorted((r for r in rules if r.status == ACTIVE and r.kind == PERCENTAGE
                     and r.effective_from > now), key=lambda r: r.effective_from)
    for rule in future:
        node = rule.geo_node
        if node is not None and node.kind == 'city' and node.id != city_node_id:
            continue
        if rule.vehicle_type_id is not None and rule.vehicle_type_id != vehicle_type_id:
            continue
        # Only a change if nothing more specific already covers the cell then.
        current = resolve(country, city_node_id, vehicle_type_id, rule.effective_from,
                          rules=rules)
        if current is not None and current.id == rule.id:
            return rule
    return None


def matrix(countries, cities_by_country, vehicle_types, now=None):
    """Rows for every country x city x vehicle class.

    `countries` is a list of codes; `cities_by_country` maps code -> [GeoNode city];
    `vehicle_types` a list of VehicleType. A cell with no rule says so
    (`percent: None`) - the console shows it as "not configured".
    """
    now = now or timezone.now()
    rows = []
    for country in countries:
        rules = list(CommissionRule.objects.filter(
            country=country, status__in=APPLICABLE_STATUSES).select_related('geo_node'))
        for city in cities_by_country.get(country, []):
            for vt in vehicle_types:
                rule = resolve(country, city.id, vt.id, now, rules=rules)
                nxt = next_change(country, city.id, vt.id, now, rules=rules)
                rows.append({
                    'country': country, 'city': city.code, 'city_name': city.name,
                    'vehicle_type': vt.id, 'vehicle_type_name': vt.type,
                    'percent': str(rule.percent) if rule else None,
                    'source': describe_source(rule) if rule else None,
                    'rule_id': rule.id if rule else None,
                    'version': rule.version if rule else None,
                    'effective_from': rule.effective_from.isoformat() if rule else None,
                    'next_change': ({
                        'rule_id': nxt.id, 'percent': str(nxt.percent),
                        'effective_from': nxt.effective_from.isoformat()} if nxt else None),
                })
    return rows


def describe_source(rule):
    return {'country': 'country default', 'city': 'city override',
            'vehicle': 'vehicle-class override',
            'city_vehicle': 'city + vehicle-class override'}[rule.level]


# ---------------------------------------------------------------------------
# Driver-earnings preview
# ---------------------------------------------------------------------------

def earnings_preview(draft, now=None):
    """What `draft` would have changed for drivers over the last 30 days.

    Replays each completed, settled trip in the draft's scope under the draft's
    percent and compares the commission with the one that was settled. A trip is
    AFFECTED only if the draft would be the rule governing it (a more specific
    rule above it, or an existing rule that stays more specific, excludes it).

    Result per currency: trips_affected, drivers_affected, total_change_minor and
    average_change_minor. A positive change means drivers would have kept MORE.
    Percentage drafts only. The 30 days are of `completed_at`.
    """
    from servers.payments.models import TripSettlement
    from servers.pricing.models import ServiceZone  # noqa: F401  (documented relation)

    now = now or timezone.now()
    since = now - timedelta(days=PREVIEW_DAYS)
    qs = (TripSettlement.objects
          .filter(version=1, trip__completed_at__gte=since, trip__completed_at__lte=now,
                  trip__zone__isnull=False)
          .select_related('trip', 'trip__zone', 'trip__zone__geo_node'))
    if draft.vehicle_type_id:
        qs = qs.filter(trip__requested_vehicle_type_id=draft.vehicle_type_id)
    node = draft.geo_node
    if node is not None:
        qs = qs.filter(trip__zone__geo_node__path__startswith=node.path)
    else:
        qs = qs.filter(trip__zone__country=draft.country)

    out = {}
    cell_cache = {}
    for s in qs.iterator():
        trip = s.trip
        country, city_id, vt_id = trip_context(trip)
        if country != draft.country:
            continue
        cell = (city_id, vt_id)
        if cell not in cell_cache:
            winner = resolve(country, city_id, vt_id, now, override=draft)
            cell_cache[cell] = winner is not None and _same_key(winner, draft)
        if not cell_cache[cell]:
            continue
        new_commission = to_minor(
            from_minor(s.gross_fare_minor, s.currency) * draft.percent / Decimal('100'), s.currency)
        change = s.commission_amount_minor - new_commission  # driver net change
        bucket = out.setdefault(s.currency, {
            'trips': 0, 'drivers': set(), 'total_change_minor': 0})
        bucket['trips'] += 1
        bucket['drivers'].add(s.driver_id)
        bucket['total_change_minor'] += change
    result = {}
    for currency, b in sorted(out.items()):
        result[currency] = {
            'trips_affected': b['trips'],
            'drivers_affected': len(b['drivers']),
            'total_change_minor': b['total_change_minor'],
            'average_change_minor': (round(b['total_change_minor'] / b['trips'])
                                     if b['trips'] else 0),
        }
    return {'window_days': PREVIEW_DAYS, 'since': since.isoformat(), 'percent': str(draft.percent),
            'by_currency': result,
            'note': 'Positive change = drivers keep more. Based on settled trips only; '
                    'trips not yet settled are not counted.'}
