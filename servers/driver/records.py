"""MVAG 2025 driver records: health insurance, term insurance, induction training (2026-11-09).

The values are config per country and state (`pricing.MarketRegulation`: India 5,00,000 INR
health, 10,00,000 INR term, 40 hours). The GATE is off until operations is ready
(`driver_records_gate`); while it is off nothing here blocks anyone and the console only shows
each driver's status. With it on, a missing, expired or too-small record blocks the driver
through the same daily sweeper as an expired vehicle document, and a driver who is not
compliant cannot accept a ride.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from django.utils import timezone

LABELS = {
    'health_insurance': 'Health insurance',
    'term_insurance': 'Term insurance',
    'induction_training': 'Induction training',
}


@dataclass(frozen=True)
class Gate:
    enabled: bool
    health_min_minor: Optional[int]
    term_min_minor: Optional[int]
    induction_min_hours: Optional[Decimal]


def market_of(driver):
    """`(country, state_code)` of a driver: the home city's state, else the phone's country."""
    from servers.driver.compliance import driver_country_code

    node = getattr(driver, 'home_city', None)
    while node is not None and node.kind != 'state':
        node = node.parent
    state = node.code.split('-')[-1].upper() if node is not None else ''
    return driver_country_code(driver), state


def gate_for(driver):
    from servers.pricing.regulation_models import MarketRegulation

    country, state = market_of(driver)
    rows = {r.state_code: r for r in MarketRegulation.objects.filter(
        country=country, state_code__in=['', state])}

    def pick(field):
        for row in (rows.get(state) if state else None, rows.get('')):
            if row is not None and getattr(row, field) is not None:
                return getattr(row, field)
        return None

    return Gate(enabled=bool(pick('driver_records_gate')),
                health_min_minor=pick('health_insurance_min_minor'),
                term_min_minor=pick('term_insurance_min_minor'),
                induction_min_hours=pick('induction_min_hours'))


def items(driver, gate=None, today=None):
    """Each record with whether it meets the market's values: `[{key, label, ok, problem, ...}]`."""
    gate = gate or gate_for(driver)
    today = today or timezone.localdate()
    out = []
    for key, minimum in (('health_insurance', gate.health_min_minor),
                         ('term_insurance', gate.term_min_minor)):
        expiry = getattr(driver, f'{key}_expiry')
        insured = getattr(driver, f'{key}_sum_insured_minor')
        problem = None
        if not getattr(driver, f'{key}_policy_number') or expiry is None:
            problem = 'missing'
        elif expiry < today:
            problem = 'expired'
        elif minimum is not None and (insured or 0) < minimum:
            problem = 'below_minimum'
        out.append({'key': key, 'label': LABELS[key], 'ok': problem is None, 'problem': problem,
                    'expiry': expiry.isoformat() if expiry else None,
                    'sum_insured_minor': insured, 'minimum_minor': minimum,
                    'has_document': bool(getattr(driver, f'{key}_doc'))})
    hours = driver.induction_training_hours
    problem = None
    if hours is None or driver.induction_training_date is None:
        problem = 'missing'
    elif gate.induction_min_hours is not None and hours < gate.induction_min_hours:
        problem = 'below_minimum'
    out.append({'key': 'induction_training', 'label': LABELS['induction_training'],
                'ok': problem is None, 'problem': problem,
                'hours': str(hours) if hours is not None else None,
                'minimum_hours': (str(gate.induction_min_hours)
                                  if gate.induction_min_hours is not None else None),
                'date': (driver.induction_training_date.isoformat()
                         if driver.induction_training_date else None),
                'has_document': bool(driver.induction_training_certificate)})
    return out


def status(driver, today=None):
    """What the console shows for one driver."""
    gate = gate_for(driver)
    rows = items(driver, gate, today)
    return {'gate_enabled': gate.enabled, 'compliant': all(r['ok'] for r in rows),
            'blocks_driver': gate.enabled and not all(r['ok'] for r in rows), 'items': rows}


def blocking_problems(driver, today=None):
    """The records that keep this driver offline, or [] (always [] while the gate is off)."""
    gate = gate_for(driver)
    if not gate.enabled:
        return []
    return [r['label'] for r in items(driver, gate, today) if not r['ok']]


def any_gate_enabled():
    from servers.pricing.regulation_models import MarketRegulation

    return MarketRegulation.objects.filter(driver_records_gate=True).exists()
