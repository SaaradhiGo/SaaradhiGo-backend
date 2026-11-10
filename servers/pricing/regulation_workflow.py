"""Changing the MVAG 2025 limits: a regulation row, or a state commission cap.

Both are `pricing.change` maker-checker requests carried by the one executor in
`commission_workflow.execute_pricing_change`, and both are audited like the country
commission config. The executor re-checks the row version, so an approval of a stale
request is refused rather than overwriting a newer value.
"""

from decimal import Decimal, InvalidOperation

from django.db import transaction

from base.countries import COUNTRIES
from servers.admin_access.models import GeoNode
from servers.admin_audit.services import record_admin_action
from servers.approvals import registry
from servers.approvals.actions import PRICING_CHANGE
from servers.approvals.service import (
    InvalidRequest, NotInScope, NotPermitted, require_approval_or_execute,
)

from . import commission as resolver
from . import regulation
from .commission_models import CommissionStateCap
from .regulation_models import BASE_FARE_SOURCE_CHOICES, MarketRegulation

KIND_REGULATION = 'market_regulation'
KIND_STATE_CAP = 'commission_state_cap'
KINDS = (KIND_REGULATION, KIND_STATE_CAP)

#: The editable fields of a `MarketRegulation` row: `(kind, low, high)`. Decimals travel as
#: strings in the request payload; NULL means "not set here" (the country value applies).
FIELDS = {
    'base_fare_source': ('choice', None, None),
    'max_dynamic_multiplier': ('decimal', Decimal('1.00'), Decimal('5.00')),
    'min_dynamic_multiplier': ('decimal', Decimal('0.01'), Decimal('1.00')),
    'cancel_penalty_percent': ('decimal', Decimal('0'), Decimal('100')),
    'cancel_penalty_cap_minor': ('int', 0, 10 ** 12),
    'cancel_free_seconds': ('int', 0, 3600),
    'driver_records_gate': ('bool', None, None),
    'health_insurance_min_sum': ('int', 0, 10 ** 12),
    'term_insurance_min_sum': ('int', 0, 10 ** 12),
    'induction_min_hours': ('decimal', Decimal('0'), Decimal('500')),
    # Driver requirements (10 Nov; servers.driver.requirements).
    'driver_requirements': ('requirements', None, None),
    'min_driver_age': ('int', 16, 30),
    'max_vehicle_age_years': ('int', 1, 40),
    'languages': ('languages', None, None),
}
#: The site's languages; a market offers a subset.
LANGUAGE_CODES = ('en', 'te', 'hi', 'es')


def _out(value):
    return str(value) if isinstance(value, Decimal) else value


def regulation_dict(row, country='', state_code=''):
    return {
        'country': row.country if row else country,
        'state_code': row.state_code if row else state_code,
        'configured': row is not None,
        **{name: (_out(getattr(row, name)) if row else None) for name in FIELDS},
        'base_fare_source': (row.base_fare_source or None) if row else None,
        'note': row.note if row else '',
        'version': row.version if row else 0,
        'updated_at': row.updated_at.isoformat() if row else None,
        'updated_by': row.updated_by_label if row else '',
    }


def state_cap_dict(row, country='', state_code=''):
    return {
        'country': row.country if row else country,
        'state_code': row.state_code if row else state_code,
        'max_percent': str(row.max_percent) if row and row.max_percent is not None else None,
        'version': row.version if row else 0,
        'updated_at': row.updated_at.isoformat() if row else None,
        'updated_by': row.updated_by_label if row else '',
    }


def _scope(actor, country, state_code):
    """`(country, STATE, node)` after the scope and permission checks."""
    from servers.admin_access.bridge import granted
    from servers.admin_access.scoping import scoped_or_none

    country = str(country or '').upper()
    if country not in COUNTRIES:
        raise InvalidRequest(f'Unknown country {country!r}.', code='UNKNOWN_COUNTRY',
                             field='country')
    state_code = str(state_code or '').upper()
    code = f'{country}-{state_code}' if state_code else country
    node = (GeoNode.objects.filter(code=code).first()
            or GeoNode.objects.filter(kind='country', code=country).first())
    if node is not None and scoped_or_none(actor, GeoNode.objects.filter(pk=node.pk)) is None:
        raise NotInScope('No such country or state.', field='state_code' if state_code
                         else 'country')
    if not granted(actor, 'pricing.write', node)[0]:
        raise NotPermitted('You do not hold pricing.write here.', code='SCOPE_NOT_HELD',
                           field='country')
    return country, state_code, node


def _parse(name, value):
    kind, low, high = FIELDS[name]
    if kind == 'choice':
        if value not in {k for k, _ in BASE_FARE_SOURCE_CHOICES}:
            raise InvalidRequest('base_fare_source must be state_notified or platform.',
                                 code='INVALID_VALUE', field=name)
        return value
    if kind == 'requirements':
        from servers.driver.requirements import ITEMS, OFF, OPTIONAL, REQUIRED

        if not isinstance(value, dict) or any(k not in ITEMS for k in value):
            raise InvalidRequest('driver_requirements names an unknown item.', code='INVALID_VALUE', field=name)
        out = {}
        for key, v in value.items():
            need = (v or {}).get('need')
            if need not in (REQUIRED, OPTIONAL, OFF):
                raise InvalidRequest(f'{key}: need must be required, optional or off.', code='INVALID_VALUE',
                                     field=name)
            out[key] = {'need': need, 'blocks_online': bool((v or {}).get('blocks_online')) and need == REQUIRED}
        return out
    if kind == 'languages':
        if (not isinstance(value, list) or not value or 'en' not in value
                or any(v not in LANGUAGE_CODES for v in value)):
            raise InvalidRequest(f'languages must include en and only {", ".join(LANGUAGE_CODES)}.',
                                 code='INVALID_VALUE', field=name)
        return list(dict.fromkeys(value))
    if kind == 'bool':
        if not isinstance(value, bool):
            raise InvalidRequest(f'{name} must be true or false.', code='INVALID_VALUE', field=name)
        return value
    try:
        out = (int(value) if kind == 'int'
               else Decimal(str(value)).quantize(Decimal('0.01')))
    except (InvalidOperation, TypeError, ValueError):
        raise InvalidRequest(f'{name} must be a number.', code='INVALID_VALUE', field=name)
    if not low <= out <= high:
        raise InvalidRequest(f'{name} must be {low} to {high}.', code='INVALID_VALUE', field=name)
    return out


def propose_regulation(actor, *, country, state_code='', changes=None, clear=(), note=None,
                       reason='', http_request=None):
    """Raise a request to set the fields in `changes` (or unset those in `clear`) on one row."""
    country, state_code, node = _scope(actor, country, state_code)
    if not (reason or '').strip():
        raise InvalidRequest('A reason is required.', code='REASON_REQUIRED', field='reason')
    current = MarketRegulation.objects.filter(country=country, state_code=state_code).first()
    values = {name: getattr(current, name) if current else None for name in FIELDS}
    for name, value in (changes or {}).items():
        if name in FIELDS and value not in (None, ''):
            values[name] = _parse(name, value)
    for name in clear or ():
        if name in values:
            values[name] = None
    values['base_fare_source'] = values['base_fare_source'] or ''
    payload = {'kind': KIND_REGULATION, 'country': country, 'state_code': state_code,
               **{name: _out(v) for name, v in values.items()},
               'note': (current.note if current else '') if note is None else str(note)[:255],
               'base_version': current.version if current else 0}
    import hashlib
    import json
    key = 'market_regulation:' + hashlib.sha256(json.dumps(
        [payload[k] for k in ('country', 'state_code', *FIELDS, 'base_version')], sort_keys=True,
        default=str).encode()).hexdigest()
    return require_approval_or_execute(
        PRICING_CHANGE, actor=actor, country=country, scope_node=node,
        target_type=KIND_REGULATION, target_id=f'{country}-{state_code}' if state_code else country,
        payload=payload, idempotency_key=key[:200], reason=reason, http_request=http_request)


def propose_state_cap(actor, *, country, state_code, max_percent=None, clear=False, reason='',
                      http_request=None):
    country, state_code, node = _scope(actor, country, state_code)
    if not state_code:
        raise InvalidRequest('A state is required.', code='STATE_REQUIRED', field='state_code')
    if not (reason or '').strip():
        raise InvalidRequest('A reason is required.', code='REASON_REQUIRED', field='reason')
    current = CommissionStateCap.objects.filter(country=country, state_code=state_code).first()
    new = None
    if not clear:
        try:
            new = Decimal(str(max_percent)).quantize(Decimal('0.01'))
        except (InvalidOperation, ValueError):
            raise InvalidRequest('max_percent must be a number.', code='INVALID_PERCENT',
                                 field='max_percent')
        if not Decimal('0') <= new <= Decimal('100'):
            raise InvalidRequest('max_percent must be 0 to 100.', code='INVALID_PERCENT',
                                 field='max_percent')
        country_cap = resolver.max_percent(country)
        if country_cap is not None and new > country_cap:
            raise InvalidRequest(
                f'A state cap can only be lower than the {country} cap of {country_cap}%.',
                code='STATE_CAP_ABOVE_COUNTRY', field='max_percent')
    payload = {'kind': KIND_STATE_CAP, 'country': country, 'state_code': state_code,
               'max_percent': str(new) if new is not None else None,
               'base_version': current.version if current else 0}
    key = (f'commission_state_cap:{country}:{state_code}:{payload["max_percent"] or "-"}:'
           f'{payload["base_version"]}')
    return require_approval_or_execute(
        PRICING_CHANGE, actor=actor, country=country, scope_node=node,
        target_type=KIND_STATE_CAP, target_id=f'{country}-{state_code}', payload=payload,
        idempotency_key=key, reason=reason, http_request=http_request)


def _stale():
    return registry.ExecutionRefused(
        'CONFIG_CHANGED', 'The setting changed after this request was raised; raise it again.')


def execute(kind, request_row):
    p = request_row.payload or {}
    reason = request_row.decision_reason or request_row.request_reason
    if kind == KIND_REGULATION:
        with transaction.atomic():
            row = (MarketRegulation.objects.select_for_update()
                   .filter(country=p['country'], state_code=p['state_code']).first())
            if (row.version if row else 0) != p.get('base_version'):
                raise _stale()
            before = regulation_dict(row, p['country'], p['state_code'])
            row = row or MarketRegulation(country=p['country'], state_code=p['state_code'],
                                          version=0)
            for name, (kind_, _, _) in FIELDS.items():
                value = p.get(name)
                if kind_ == 'decimal' and value is not None:
                    value = Decimal(value)
                setattr(row, name, value)
            row.base_fare_source = p.get('base_fare_source') or ''
            row.note = p.get('note') or ''
            row.version += 1
            row.updated_by_label = request_row.maker_label
            row.reason = request_row.request_reason
            row.save()
            record_admin_action(None, action='market_regulation_changed',
                                target_type='market_regulation', target_id=str(row.id),
                                before=before, after=regulation_dict(row), reason=reason)
        return {'market_regulation_id': row.id, 'version': row.version}
    with transaction.atomic():
        row = (CommissionStateCap.objects.select_for_update()
               .filter(country=p['country'], state_code=p['state_code']).first())
        if (row.version if row else 0) != p.get('base_version'):
            raise _stale()
        cap = Decimal(p['max_percent']) if p.get('max_percent') is not None else None
        country_cap = resolver.max_percent(p['country'])
        if cap is not None and country_cap is not None and cap > country_cap:
            raise registry.ExecutionRefused(
                'STATE_CAP_ABOVE_COUNTRY', 'A state cap can only be lower than the country cap.')
        before = state_cap_dict(row, p['country'], p['state_code'])
        row = row or CommissionStateCap(country=p['country'], state_code=p['state_code'],
                                        version=0)
        row.max_percent = cap
        row.version += 1
        row.updated_by_label = request_row.maker_label
        row.reason = request_row.request_reason
        row.save()
        record_admin_action(None, action='commission_state_cap_changed',
                            target_type='commission_state_cap', target_id=str(row.id),
                            before=before, after=state_cap_dict(row), reason=reason)
    regulation.alert_rules_above_cap()
    return {'commission_state_cap_id': row.id, 'version': row.version}
