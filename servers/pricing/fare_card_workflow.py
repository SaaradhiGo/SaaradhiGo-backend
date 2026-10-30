"""Fare cards through maker-checker, and the rider-fare preview.

NO FARE VALUE CHANGES HERE
--------------------------
This module changes HOW a fare card changes, never what any card says. Creating a
new version, retiring a card, and changing a surge cap or a night multiplier (which
are pricing fields, so a new version) all raise a `pricing.change` request. Nothing
is written until a different holder of `approval.check` + `pricing.write` approves.
Lifecycle-only edits (notes) stay direct, and are audited.

The executor never backdates: a card cannot take effect before the moment it is
approved (a trip's price must not change after the fact).

THE PREVIEW
-----------
`preview_fares` quotes sample trips (default 3, 8 and 15 km, off-peak and at night)
under the CURRENT card and a DRAFT's values, in the zone's currency. It repeats the
metered part of `services.quote_fare` (base + distance + time, night multiplier, the
surge cap, the minimum fare, tax and the flat fee) with no demand surge, which needs
live Redis supply. `tests/test_pricing_module.py` holds it equal to `quote_fare` so
the two cannot drift.
"""

import hashlib
import json
import logging
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from base.money import quantize
from servers.admin_audit.services import record_admin_action
from servers.approvals import registry
from servers.approvals.actions import PRICING_CHANGE
from servers.approvals.service import InvalidRequest, require_approval_or_execute

from .models import RateCard

logger = logging.getLogger(__name__)

KIND_CREATE = 'rate_card_create'
KIND_RETIRE = 'rate_card_retire'
KINDS = (KIND_CREATE, KIND_RETIRE)

#: The fields a draft card carries. commission_percent is deliberately absent: it is
#: deprecated and nothing reads it (commission is the CommissionRule model).
MONEY_FIELDS = ('base_fare', 'per_km_fare', 'per_min_fare', 'min_fare')
VALUE_FIELDS = MONEY_FIELDS + (
    'night_surge_multiplier', 'night_surge_start_hour', 'night_surge_end_hour',
    'surge_cap_multiplier', 'gst_percent')

SAMPLE_KM = (3, 8, 15)
#: Assumed urban pace for the sample trips: 3 minutes per km (20 km/h).
MINUTES_PER_KM = 3


def card_values(card):
    return {f: getattr(card, f) for f in VALUE_FIELDS}


def jsonable(values):
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in values.items()}


def _decimals(values):
    out = {}
    for k, v in values.items():
        out[k] = Decimal(str(v)) if k in MONEY_FIELDS + (
            'night_surge_multiplier', 'surge_cap_multiplier', 'gst_percent') else int(v)
    return out


def check_values(values):
    """Same rules as RateCardSerializer.validate, for a values dict."""
    for f in MONEY_FIELDS:
        if values[f] < 0:
            raise InvalidRequest(f'{f} must be non-negative.', code='INVALID_FARE', field=f)
    if values['surge_cap_multiplier'] < 1:
        raise InvalidRequest('surge_cap_multiplier must be >= 1.00.', code='INVALID_FARE',
                             field='surge_cap_multiplier')
    if values['night_surge_multiplier'] < 0:
        raise InvalidRequest('night_surge_multiplier must be non-negative.',
                             code='INVALID_FARE', field='night_surge_multiplier')


def country_of(zone):
    from .commission import country_code_of_zone

    return country_code_of_zone(zone) or zone.country


def current_card(zone, vehicle_type):
    return (RateCard.objects.filter(zone=zone, vehicle_type=vehicle_type, is_active=True,
                                    effective_to__isnull=True)
            .order_by('-effective_from', '-version').first())


# ---------------------------------------------------------------------------
# Proposing
# ---------------------------------------------------------------------------

def _key(kind, zone_id, vt_id, body):
    raw = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:24]
    # Grouped so the key never contains a long run of digits: the key is shown in the
    # approvals API and a long digit run reads as a phone number to the PII sweep.
    digest = '-'.join(raw[i:i + 6] for i in range(0, 24, 6))
    return f'{kind}:{zone_id}:{vt_id}:{digest}'


def propose_new_version(actor, *, zone, vehicle_type, values, base_card=None,
                        effective_from=None, notes='', reason='', http_request=None):
    """Raise the request to create a card (or a new version of `base_card`)."""
    values = _decimals(values)
    check_values(values)
    if not (reason or '').strip():
        raise InvalidRequest('A reason is required.', code='REASON_REQUIRED', field='reason')
    newest = current_card(zone, vehicle_type)
    if (newest.id if newest else None) != (base_card.id if base_card else None):
        raise InvalidRequest(
            f'This class already has a newer version (v{newest.version}'
            f'{", scheduled" if newest.effective_from > timezone.now() else ""}); base the '
            f'change on that one.' if newest else 'A card now exists for this class; base the '
            'change on it.', code='NEWER_VERSION_EXISTS', field='card')
    now = timezone.now()
    eff = effective_from or now
    payload = {'kind': KIND_CREATE, 'zone': zone.id, 'zone_code': zone.code,
               'vehicle_type': vehicle_type.id, 'vehicle_type_name': vehicle_type.type,
               'values': jsonable(values), 'effective_from': eff.isoformat(),
               'notes': notes or '', 'base_card': base_card.id if base_card else None,
               'base_version': base_card.version if base_card else None}
    return require_approval_or_execute(
        PRICING_CHANGE, actor=actor, country=country_of(zone), scope_node=zone.geo_node,
        target_type='rate_card', target_id=str(base_card.id) if base_card else f'new:{zone.id}',
        payload=payload,
        idempotency_key=_key(KIND_CREATE, zone.id, vehicle_type.id, payload),
        reason=reason, http_request=http_request)


def propose_retire(actor, *, card, reason='', http_request=None):
    if not (reason or '').strip():
        raise InvalidRequest('A reason is required to retire a fare card.',
                             code='REASON_REQUIRED', field='reason')
    zone = card.zone
    payload = {'kind': KIND_RETIRE, 'card': card.id, 'zone': zone.id, 'zone_code': zone.code,
               'vehicle_type': card.vehicle_type_id, 'version': card.version}
    return require_approval_or_execute(
        PRICING_CHANGE, actor=actor, country=country_of(zone), scope_node=zone.geo_node,
        target_type='rate_card', target_id=str(card.id), payload=payload,
        idempotency_key=f'{KIND_RETIRE}:{card.id}', reason=reason, http_request=http_request)


# ---------------------------------------------------------------------------
# Executing
# ---------------------------------------------------------------------------

def execute(kind, request_row):
    if kind == KIND_CREATE:
        return _execute_create(request_row)
    return _execute_retire(request_row)


def _execute_create(request_row):
    from servers.driver.models import VehicleType

    from .models import ServiceZone

    p = request_row.payload or {}
    now = timezone.now()
    eff = max(_parse(p.get('effective_from')) or now, now)
    values = _decimals(p['values'])
    with transaction.atomic():
        zone = ServiceZone.objects.filter(pk=p['zone']).first()
        vt = VehicleType.objects.filter(pk=p['vehicle_type']).first()
        if zone is None or vt is None:
            raise registry.ExecutionRefused('TARGET_GONE', 'The zone or vehicle class is gone.')
        current = (RateCard.objects.select_for_update()
                   .filter(zone=zone, vehicle_type=vt, is_active=True, effective_to__isnull=True)
                   .order_by('-effective_from', '-version').first())
        base = p.get('base_card')
        if (current.id if current else None) != base:
            raise registry.ExecutionRefused(
                'CARD_CHANGED', 'The active fare card changed after this request was raised; '
                'raise it again.')
        if current is not None and eff <= current.effective_from:
            eff = max(now, current.effective_from + timedelta(seconds=1))
        if current is not None:
            card = current.new_version(effective_from=eff, notes=p.get('notes') or current.notes,
                                       **values)
        else:
            card = RateCard.objects.create(
                zone=zone, vehicle_type=vt, effective_from=eff, version=1,
                notes=p.get('notes') or '', **values)
        record_admin_action(
            None, action='rate_card_version_created', target_type='rate_card',
            target_id=card.id,
            before={'card': current.id, 'version': current.version,
                    **jsonable(card_values(current))} if current else {},
            after={'card': card.id, 'version': card.version, 'zone': zone.code,
                   'vehicle_type': vt.type, 'effective_from': eff.isoformat(),
                   **jsonable(card_values(card))},
            reason=request_row.decision_reason or request_row.request_reason)
    return {'rate_card_id': card.id, 'version': card.version, 'effective_from': eff.isoformat()}


def _execute_retire(request_row):
    p = request_row.payload or {}
    now = timezone.now()
    with transaction.atomic():
        card = RateCard.objects.select_for_update().filter(pk=p['card']).first()
        if card is None or not card.is_active:
            raise registry.ExecutionRefused('CARD_NOT_ACTIVE',
                                            'That fare card is no longer active.')
        before = {'is_active': card.is_active,
                  'effective_to': card.effective_to.isoformat() if card.effective_to else None}
        # Lifecycle columns only: the queryset guard allows it.
        RateCard.objects.filter(pk=card.pk).update(is_active=False, effective_to=min(
            now, card.effective_to) if card.effective_to else now)
        record_admin_action(
            None, action='rate_card_retired', target_type='rate_card', target_id=card.id,
            before=before, after={'is_active': False, 'version': card.version,
                                  'zone': card.zone.code},
            reason=request_row.decision_reason or request_row.request_reason)
    return {'rate_card_id': card.id, 'retired': True}


def _parse(value):
    from django.utils.dateparse import parse_datetime

    parsed = parse_datetime(value) if value else None
    if parsed is not None and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


# ---------------------------------------------------------------------------
# The rider-fare preview
# ---------------------------------------------------------------------------

def quote_from_values(zone, values, km, minutes, *, night):
    """The metered fare for these card values. Mirrors `services.quote_fare` with no
    demand surge. Returns a dict of Decimals in the zone's currency."""
    from base.countries import get_country

    base, per_km = values['base_fare'], values['per_km_fare']
    per_min, min_fare = values['per_min_fare'], values['min_fare']
    night_mult, cap = values['night_surge_multiplier'], values['surge_cap_multiplier']
    km, minutes = Decimal(str(km)), Decimal(str(minutes))
    subtotal = base + per_km * km + per_min * minutes
    surge = Decimal('1.00')
    night_applied = False
    if night and night_mult > Decimal('1.00'):
        subtotal = subtotal * night_mult
        surge = surge * night_mult
        night_applied = True
    if surge > cap:
        subtotal = (base + per_km * km + per_min * minutes) * cap
        surge = cap
    min_applied = False
    if subtotal < min_fare:
        subtotal, min_applied = min_fare, True
    country = get_country(getattr(zone, 'country', None))
    currency = getattr(zone, 'currency', None) or country.currency
    total = quantize(subtotal, currency)
    tax_mode = getattr(zone, 'tax_mode', '') or 'inclusive'
    tax = Decimal('0.00')
    fee = quantize(Decimal(str(getattr(zone, 'per_trip_fee', None) or '0')), currency)
    if tax_mode == 'additive':
        tax = quantize(total * Decimal(str(values['gst_percent'] or 0)) / Decimal('100'), currency)
        total = total + tax + fee
    return {'total': total, 'currency': currency, 'night_applied': night_applied,
            'min_fare_applied': min_applied, 'surge': surge}


def preview_fares(zone, old_values, new_values, *, distances=SAMPLE_KM):
    """Old vs new rider fares for sample trips, off-peak and at night."""
    from base.money import to_minor

    rows = []
    currency = None
    for km in distances:
        minutes = Decimal(km) * MINUTES_PER_KM
        for label, night in (('off_peak', False), ('night', True)):
            old = quote_from_values(zone, old_values, km, minutes, night=night) \
                if old_values else None
            new = quote_from_values(zone, new_values, km, minutes, night=night)
            currency = new['currency']
            rows.append({
                'distance_km': km, 'minutes': int(minutes), 'period': label,
                'old_minor': to_minor(old['total'], currency) if old else None,
                'new_minor': to_minor(new['total'], currency),
                'change_minor': (to_minor(new['total'], currency)
                                 - to_minor(old['total'], currency)) if old else None,
                'night_applied': new['night_applied'],
            })
    return {'currency': currency, 'assumes': f'{MINUTES_PER_KM} min per km, no demand surge',
            'rows': rows}
