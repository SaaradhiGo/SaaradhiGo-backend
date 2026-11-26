"""The rider booking fee: its own setting per place and class (owner ruling, 26 Nov).

Before, it sat inside the driver fee plan (`rider_platform_fee_minor`). Now it is a versioned row per
place (country, state, city) with an amount per class; a class not named inherits from the level above.
Where drivers are on a daily-fee plan the default is no booking fee: only a booking fee set at that
plan's place (or a more specific one) applies there. A change is a `pricing.change` request (kind
`rider_booking_fee`) with the Edit pattern: before/after, impact, start date, reason, a second person.
The quote still adds it only behind the `rider_platform_fee` flag (CA's tax ruling), and only above 0.
"""

from datetime import date, timedelta

from django.db import transaction

from servers.admin_audit.services import record_admin_action
from servers.approvals.actions import PRICING_CHANGE
from servers.approvals.service import InvalidRequest, NotPermitted, require_approval_or_execute

from .booking_fee_models import ACTIVE, ARCHIVED, RiderBookingFee

KIND = 'rider_booking_fee'
NONE_WORDS = 'No booking fee'
#: The most a booking fee may be, in whole currency units (a typo guard, not a policy).
MAX_UNITS = 100


def money_words(minor, currency):
    from . import fee_plans

    if minor is None:
        return 'not set'
    return NONE_WORDS if int(minor) == 0 else fee_plans._money(int(minor), currency)


def scope_chain(country, scope_code):
    """[scope_code, its parents up to the state..., ''] most specific first."""
    from servers.admin_access.models import GeoNode

    out = []
    node = GeoNode.objects.filter(code=scope_code).first() if scope_code else None
    if scope_code and node is None:
        out.append(scope_code)
    while node is not None and node.kind in ('city', 'state'):
        out.append(node.code)
        node = node.parent
    return out + ['']


def _rows_in_force(country, day):
    rows = {}
    for r in (RiderBookingFee.objects.filter(country=str(country).upper(), status__in=(ACTIVE, ARCHIVED),
                                             effective_from__lte=day)
              .exclude(effective_to__lte=day).order_by('-effective_from', '-version')):
        rows.setdefault(r.scope_code, r)
    return rows


def resolve(country, scopes, vehicle_type, day):
    """(minor, source scope or None). A daily-fee place stops inheritance at the plan's own place."""
    from . import fee_plans

    scopes = list(scopes)
    if '' not in scopes:
        scopes.append('')
    plan, plan_scope = fee_plans.resolve_with_source(country, [s for s in scopes if s], day, vehicle_type)
    limit = len(scopes)
    if plan is not None and plan.plan_type == fee_plans.DAILY_PASS and plan_scope in scopes:
        limit = scopes.index(plan_scope) + 1
    rows = _rows_in_force(country, day)
    for code in scopes[:limit]:
        row = rows.get(code)
        if row is None:
            continue
        amounts = row.amounts or {}
        for key in (vehicle_type, '*'):
            if key in amounts and amounts[key] is not None:
                return int(amounts[key]), code
    return 0, None


def fee_minor_for_zone(zone, vehicle_type, when=None):
    from servers.pricing.commission import country_code_of_zone

    from . import fee_plans

    if zone is None or not vehicle_type:
        return 0
    country = country_code_of_zone(zone)
    day = fee_plans.local_day(country, when, zone)
    return resolve(country, fee_plans.scope_codes_of_zone(zone), str(vehicle_type).lower(), day)[0]


# ---------------------------------------------------------------------------
# The console: places, the Edit preview, the request, the executor
# ---------------------------------------------------------------------------
def _currency(country):
    from base.countries import get_country

    return get_country(country).currency


def _place_name(country, scope_code):
    from .fee_plan_workflow import _node

    node = _node(country, scope_code)
    return node.name if node is not None else (scope_code or country)


def place(country, scope_code, day=None):
    """Per offered class: the fee in force at this place, and where it comes from."""
    from . import edit_pattern, fee_plans

    country = str(country).upper()
    day = day or fee_plans.local_day(country)
    currency = _currency(country)
    chain = scope_chain(country, scope_code)
    own = _rows_in_force(country, day).get(scope_code)
    classes = []
    for vt in edit_pattern.offered_classes(country, scope_code):
        minor, source = resolve(country, chain, vt, day)
        classes.append({'vehicle_type': vt, 'name': edit_pattern.class_word(vt), 'minor': minor,
                        'words': money_words(minor, currency),
                        'source': 'here' if source == scope_code else ('inherited' if source is not None else 'default'),
                        'source_name': _place_name(country, source) if source not in (None, scope_code) else ''})
    plan = fee_plans.resolve(country, [s for s in chain if s], day)
    return {'country': country, 'scope_code': scope_code, 'name': _place_name(country, scope_code),
            'currency': currency, 'classes': classes,
            'daily_fee_place': bool(plan and plan.plan_type == fee_plans.DAILY_PASS),
            'in_force': _row_dict(own) if own else None}


def _row_dict(r):
    return {'id': r.id, 'version': r.version, 'amounts': r.amounts, 'status': r.status,
            'effective_from': r.effective_from.isoformat(),
            'effective_to': r.effective_to.isoformat() if r.effective_to else None,
            'created_by': r.created_by_label, 'reason': r.reason, 'approval_request_id': r.approval_request_id,
            'created_at': r.created_at.isoformat()}


def history(country, scope_code):
    return [_row_dict(r) for r in RiderBookingFee.objects.filter(country=country, scope_code=scope_code)
            .order_by('-version')[:50]]


def _clean_amounts(country, scope_code, amounts):
    from base.money import normalize_currency

    from . import edit_pattern

    if not isinstance(amounts, dict) or not amounts:
        raise InvalidRequest('Give a booking fee for at least one class.', code='AMOUNTS_REQUIRED', field='amounts')
    offered = set(edit_pattern.offered_classes(country, scope_code))
    currency = _currency(country)
    out = {}
    for vt, minor in amounts.items():
        vt = str(vt).lower()
        if vt not in offered:
            raise InvalidRequest(f"{edit_pattern.class_word(vt)} is not sold here.", code='INVALID_CLASS',
                                 field='amounts')
        try:
            minor = int(minor)
        except (TypeError, ValueError):
            raise InvalidRequest('Each booking fee is a number.', code='INVALID_AMOUNT', field='amounts')
        if minor < 0 or minor > MAX_UNITS * 100:
            raise InvalidRequest(f'A booking fee is between 0 and {MAX_UNITS} in the local currency.',
                                 code='INVALID_AMOUNT', field='amounts')
        if normalize_currency(currency) == 'INR' and minor % 100:
            raise InvalidRequest('Booking fees in India are whole rupees.', code='WHOLE_RUPEES', field='amounts')
        out[vt] = minor
    return out


def _start(country, requested):
    from . import fee_plans

    earliest = fee_plans.local_day(country) + timedelta(days=1)
    if not requested:
        return earliest
    try:
        chosen = date.fromisoformat(str(requested)[:10])
    except ValueError:
        raise InvalidRequest('The start date is YYYY-MM-DD.', code='INVALID_DATE', field='effective_from')
    if chosen < earliest:
        raise InvalidRequest(f'The earliest start is {earliest.isoformat()}.', code='TOO_EARLY',
                             field='effective_from')
    return chosen


def preview(country, scope_code, data):
    """The Edit preview: what changes per class, the rider's side, and the first day."""
    from . import edit_pattern
    from .fee_plan_views import average_fare_minor

    country = str(country).upper()
    if scope_code and _place_name(country, scope_code) == scope_code:
        raise InvalidRequest('Unknown place.', code='UNKNOWN_PLACE', field='scope_code')
    amounts = _clean_amounts(country, scope_code, (data or {}).get('amounts'))
    start = _start(country, (data or {}).get('effective_from'))
    now = {c['vehicle_type']: c['minor'] for c in place(country, scope_code)['classes']}
    currency = _currency(country)
    name = _place_name(country, scope_code)
    changes, rows = [], []
    for vt, after in amounts.items():
        before = now.get(vt, 0)
        word = edit_pattern.class_word(vt)
        changes.append({'label': f'{name}, {word}: rider booking fee', 'from': money_words(before, currency),
                        'to': money_words(after, currency)})
        avg = average_fare_minor(country, vt)
        typical = (f'{money_words(avg + before, currency)} → {money_words(avg + after, currency)}'
                   if avg else 'No completed trips in the last 30 days')
        rows.append([word, money_words(before, currency), money_words(after, currency), typical])
    return {'changes': changes,
            'impact': {'title': 'What a rider pays', 'columns': ['Class', 'Booking fee today', 'After the change',
                                                                 'A typical trip (30-day average fare)'],
                       'rows': rows,
                       'note': 'The booking fee is its own line on the fare breakdown, shown only when above 0. '
                               'The driver keeps none of it and pays no fee on it.'},
            'start': {'effective_from': start.isoformat()},
            'amounts': amounts}


def display(body):
    return {'changes': body['changes'], 'impact': body['impact'],
            'effective': f"Starts {body['start']['effective_from']} (local day), for new bookings."}


def propose(actor, country, scope_code, data, *, http_request=None, system_label=''):
    from servers.admin_access.bridge import granted

    from .fee_plan_workflow import _node

    country = str(country).upper()
    reason = str((data or {}).get('reason') or '').strip()
    if len(reason) < 5:
        raise InvalidRequest('Say why, in a few words.', code='REASON_REQUIRED', field='reason')
    node = _node(country, scope_code)
    if node is None:
        raise InvalidRequest('Unknown place.', code='UNKNOWN_PLACE', field='scope_code')
    if actor is not None and not granted(actor, 'pricing.write', node)[0]:
        raise NotPermitted('You do not hold pricing.write here.', code='SCOPE_NOT_HELD', field='scope_code')
    body = preview(country, scope_code, data)
    if all(c['from'] == c['to'] for c in body['changes']):
        raise InvalidRequest('Nothing is changed.', code='NO_CHANGE', field='amounts')
    from servers.approvals.models import ApprovalRequest

    if ApprovalRequest.objects.filter(action_key=PRICING_CHANGE, target_type=KIND, status='requested',
                                      target_id=f'{country}:{scope_code}').exists():
        raise InvalidRequest('A booking fee change for this place is already waiting: withdraw it or wait.',
                             code='CHANGE_WAITING', field='general')
    payload = {'kind': KIND, 'country': country, 'scope_code': scope_code, 'amounts': body['amounts'],
               'requested_from': body['start']['effective_from'], 'display': display(body)}
    import hashlib
    import json

    last = RiderBookingFee.objects.filter(country=country, scope_code=scope_code).order_by('-version').first()
    digest = hashlib.sha256(json.dumps(payload['amounts'], sort_keys=True).encode()).hexdigest()[:12]
    return require_approval_or_execute(
        PRICING_CHANGE, actor=actor, country=country, scope_node=node, target_type=KIND,
        target_id=f'{country}:{scope_code}', payload=payload,
        idempotency_key=f"{KIND}:{country}:{scope_code}:v{last.version if last else 0}:{digest}",
        reason=reason, http_request=http_request, system_label=system_label)


def execute(request_row):
    from . import fee_plans

    p = request_row.payload or {}
    country, scope_code = p['country'], p.get('scope_code') or ''
    start = max(fee_plans.local_day(country) + timedelta(days=1), date.fromisoformat(p['requested_from']))
    with transaction.atomic():
        current = list(RiderBookingFee.objects.select_for_update()
                       .filter(country=country, scope_code=scope_code, status=ACTIVE))
        base = {}
        for old in sorted(current, key=lambda r: r.effective_from):
            if old.effective_from < start:
                base = dict(old.amounts or {})
            old.status, old.effective_to = ARCHIVED, start
            old.save(update_fields=['status', 'effective_to'])
        last = RiderBookingFee.objects.filter(country=country, scope_code=scope_code).order_by('-version').first()
        row = RiderBookingFee.objects.create(
            country=country, scope_code=scope_code, currency=_currency(country), amounts={**base, **p['amounts']},
            version=(last.version + 1) if last else 1, status=ACTIVE, effective_from=start,
            approval_request_id=request_row.id, created_by_label=request_row.maker_label or '',
            reason=request_row.request_reason or '')
        record_admin_action(None, action='rider_booking_fee_changed', target_type=KIND, target_id=str(row.id),
                            before={'amounts': base}, after=_row_dict(row),
                            reason=request_row.decision_reason or request_row.request_reason)
    return {'booking_fee_id': row.id, 'effective_from': start.isoformat()}


__all__ = ['KIND', 'execute', 'fee_minor_for_zone', 'place', 'preview', 'propose', 'resolve']
