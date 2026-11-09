"""Reading the MVAG 2025 limits: the fare band per zone, the commission cap per state.

`fare_limits` is what `quote_fare` applies; `commission_cap` is what a commission rule is
checked against; `rules_above_cap` lists the active rules a cap now forbids (they are never
changed silently: a red console alert and the report name them for the owner).
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

ONE = Decimal('1.00')

#: The central guideline's band, used only to seed the India country row (data migration).
INDIA_DEFAULTS = {'base_fare_source': 'state_notified',
                  'max_dynamic_multiplier': Decimal('2.00'),
                  'min_dynamic_multiplier': Decimal('0.50')}
TELANGANA_NOTE = 'state notification not confirmed'


@dataclass(frozen=True)
class FareLimits:
    cap: Decimal
    floor: Optional[Decimal]
    base_fare_source: str
    #: Which level set the cap: 'rate_card' | 'state' | 'country'.
    cap_source: str

    def as_dict(self):
        return {'max_dynamic_multiplier': str(self.cap),
                'min_dynamic_multiplier': str(self.floor) if self.floor is not None else None,
                'base_fare_source': self.base_fare_source or None,
                'cap_source': self.cap_source}


def state_code_of_zone(zone):
    """The zone's state, walking up to the parent zones when a sub-zone leaves it blank."""
    while zone is not None:
        if getattr(zone, 'state_code', ''):
            return str(zone.state_code).upper()
        zone = getattr(zone, 'parent', None)
    return ''


def _rows(country, state_code):
    from .regulation_models import MarketRegulation

    country = str(country or '').upper()
    rows = {r.state_code: r for r in MarketRegulation.objects.filter(
        country=country, state_code__in=['', str(state_code or '').upper()])}
    return rows.get(''), (rows.get(str(state_code).upper()) if state_code else None)


def fare_limits(country, state_code, card_cap):
    """The dynamic-pricing band for one zone. See `regulation_models` for the rule."""
    country_row, state_row = _rows(country, state_code)
    cap, source = Decimal(str(card_cap)), 'rate_card'
    floors = []
    base_source = ''
    for row, level in ((state_row, 'state'), (country_row, 'country')):
        if row is None:
            continue
        if row.max_dynamic_multiplier is not None and row.max_dynamic_multiplier < cap:
            cap, source = row.max_dynamic_multiplier, level
        if row.min_dynamic_multiplier is not None:
            floors.append(row.min_dynamic_multiplier)
        base_source = base_source or row.base_fare_source
    return FareLimits(cap=cap, floor=max(floors) if floors else None,
                      base_fare_source=base_source, cap_source=source)


def limits_for_zone(zone, card_cap):
    if zone is None:
        return FareLimits(cap=Decimal(str(card_cap)), floor=None, base_fare_source='',
                          cap_source='rate_card')
    from .commission import country_code_of_zone

    return fare_limits(country_code_of_zone(zone), state_code_of_zone(zone), card_cap)


@dataclass(frozen=True)
class CancelPolicy:
    percent: Decimal
    cap_minor: Optional[int]
    free_seconds: int

    def as_dict(self):
        return {'percent': str(self.percent), 'cap_minor': self.cap_minor,
                'free_seconds': self.free_seconds}


def cancel_policy(country, state_code=''):
    """The cancellation penalty for a market (state value, else country value), or None when
    no percentage is configured: then no cancellation is ever charged."""
    country_row, state_row = _rows(country, state_code)

    def pick(field):
        for row in (state_row, country_row):
            if row is not None and getattr(row, field) is not None:
                return getattr(row, field)
        return None

    percent = pick('cancel_penalty_percent')
    if not percent:
        return None
    return CancelPolicy(percent=percent, cap_minor=pick('cancel_penalty_cap_minor'),
                        free_seconds=pick('cancel_free_seconds') or 0)


def demand_label(multiplier):
    """What the rider reads for the dynamic multiplier. Never the word "surge"."""
    if multiplier > ONE:
        return 'Higher demand'
    if multiplier < ONE:
        return 'Lower demand'
    return None


# ---------------------------------------------------------------------------
# Commission cap
# ---------------------------------------------------------------------------

def commission_cap(country, state_code=None):
    """The lowest of the country cap and (when given) the state cap; None when neither is set."""
    from . import commission as resolver
    from .commission_models import CommissionStateCap

    caps = [resolver.max_percent(country)]
    if state_code:
        row = CommissionStateCap.objects.filter(country=str(country).upper(),
                                                state_code=str(state_code).upper()).first()
        caps.append(row.max_percent if row else None)
    caps = [c for c in caps if c is not None]
    return min(caps) if caps else None


def states_governed(country, node):
    """The states a rule at `node` governs: its city's state, or for a country-wide rule every
    state with a cap of its own (the strictest of them binds)."""
    from .commission_models import CommissionStateCap

    while node is not None and node.kind not in ('state', 'country'):
        node = node.parent
    if node is not None and node.kind == 'state':
        return [node.code.split('-')[-1].upper()]
    return list(CommissionStateCap.objects.filter(country=str(country).upper(),
                                                  max_percent__isnull=False)
                .values_list('state_code', flat=True))


def cap_for(country, node):
    """The cap a rule anchored at `node` must respect, or None when no cap is set."""
    caps = [commission_cap(country)] + [commission_cap(country, s)
                                        for s in states_governed(country, node)]
    caps = [c for c in caps if c is not None]
    return min(caps) if caps else None


def rules_above_cap():
    """Active rules above the cap that governs them: `[(rule, cap)]`."""
    from .commission_models import ACTIVE, CommissionRule

    out = []
    for rule in CommissionRule.objects.filter(status=ACTIVE).select_related('geo_node'):
        cap = cap_for(rule.country, rule.geo_node)
        if cap is not None and rule.percent > cap:
            out.append((rule, cap))
    return out


def alert_rules_above_cap():
    """One critical Finance alert per active rule above its cap. Never edits the rule."""
    from servers.command_centre.models import COMMISSION_ABOVE_CAP, CRITICAL
    from servers.command_centre.services import raise_alert

    raised = []
    for rule, cap in rules_above_cap():
        raise_alert(
            COMMISSION_ABOVE_CAP, severity=CRITICAL,
            title=(f'Commission rule {rule.id} ({rule.country}, {rule.percent}%) is above the '
                   f'{cap}% cap: review it')[:200],
            payload={'rule_id': rule.id, 'country': rule.country, 'percent': str(rule.percent),
                     'cap': str(cap),
                     'reason': 'MVAG 2025: own-vehicle driver keeps at least 80%'},
            source_type='commission_rule', source_id=str(rule.id))
        raised.append(rule.id)
    return raised

