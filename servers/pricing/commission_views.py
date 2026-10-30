"""Admin API for module 8: the commission matrix, rules, config, fare-card overview.

  GET    /pricing/admin/commission/matrix/            zone.read, scoped
  GET    /pricing/admin/commission/rules/             zone.read, scoped (filters)
  POST   /pricing/admin/commission/rules/             pricing.write -> 201 draft
  GET    /pricing/admin/commission/rules/<id>/        zone.read, scoped (404 out of scope)
  PATCH  /pricing/admin/commission/rules/<id>/        pricing.write, a draft only
  DELETE /pricing/admin/commission/rules/<id>/        pricing.write, a draft only
  GET    /pricing/admin/commission/rules/<id>/preview/  zone.read: driver-earnings preview
  POST   /pricing/admin/commission/rules/<id>/submit/   pricing.write -> 202 (a checker decides)
  GET    /pricing/admin/commission/config/            zone.read: notice days and cap per country
  PUT    /pricing/admin/commission/config/<country>/  pricing.write -> 202
  GET    /pricing/admin/fare-cards/overview/          zone.read, scoped
  POST   /pricing/admin/fare-cards/preview/           zone.read: rider-fare preview, old vs new

A rule outside the caller's scope is "not found", never "forbidden".
"""

import logging

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.countries import COUNTRIES
from base.utils import error_response, success_response
from servers.admin_access.bridge import ScopedPermission
from servers.admin_access.models import GeoNode
from servers.admin_access.scoping import scoped, scoped_or_none
from servers.approvals.scope import may_act
from servers.approvals.service import ApprovalError
from servers.driver.models import VehicleType

from . import commission as resolver
from . import commission_workflow as flow
from . import fare_card_workflow as cards
from .commission_models import DRAFT, CommissionCountryConfig, CommissionRule
from .models import RateCard, ServiceZone

logger = logging.getLogger(__name__)

READ = ScopedPermission('zone.read')
WRITE = ScopedPermission('pricing.write')


def _not_found(what='rule', field='id'):
    return error_response(code='NOT_FOUND', message=f'No such {what}', field=field,
                          issue=f'No such {what}', status=status.HTTP_404_NOT_FOUND)


def _rule_or_none(user, pk):
    return scoped_or_none(
        user, CommissionRule.objects.select_related('geo_node', 'vehicle_type'), pk=pk)


def _write_gate(user, rule):
    """None when `user` may change this rule, else a 403 response. The rule is in
    scope already (the caller got it through `scoped`)."""
    if may_act(user, 'pricing.write', rule.geo_node):
        return None
    return error_response(
        code='SCOPE_NOT_HELD', message='You do not hold pricing.write here.', field='scope',
        issue='pricing.write', status=status.HTTP_403_FORBIDDEN)


def _approval_body(outcome):
    row = outcome.request
    return {'approval_request': row.as_dict(), 'duplicate': outcome.duplicate,
            'state': 'pending_approval' if row.status == 'requested' else row.status}


# ---------------------------------------------------------------------------
# Matrix
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([IsAuthenticated, READ])
def commission_matrix(request):
    """Effective rate and its source for every country x city x vehicle class in the
    caller's scope. `?country=` narrows it."""
    wanted = (request.query_params.get('country') or '').upper().strip()
    cities = list(scoped(request.user, GeoNode.objects.filter(kind='city'))
                  .select_related('parent').order_by('path'))
    # A country is in scope if its node is, OR any of its cities is: a city-scoped
    # operator sees the country's rate and its settings for the cities they run.
    codes = {n.code for n in scoped(request.user, GeoNode.objects.filter(kind='country'))}
    by_country = {}
    for city in cities:
        c = city.country
        if c is not None:
            codes.add(c.code)
            by_country.setdefault(c.code, []).append(city)
    countries = sorted(c for c in codes if c in COUNTRIES and (not wanted or c == wanted))
    vehicle_types = list(VehicleType.objects.order_by('type'))
    rows = resolver.matrix(countries, by_country, vehicle_types)
    configs = {c.country: c for c in CommissionCountryConfig.objects.filter(country__in=countries)}
    defaults = []
    for code in countries:
        rule = resolver.resolve(code, None, None)
        defaults.append({'country': code, 'rule': flow.rule_dict(rule) if rule else None})
    return success_response({
        'rows': rows,
        'country_defaults': defaults,
        'config': [flow.config_dict(c, configs.get(c)) for c in countries],
        'vehicle_types': [{'id': v.id, 'name': v.type} for v in vehicle_types],
        'kinds': [{'kind': 'percentage', 'supported': True, 'label': 'Percentage of the fare'},
                  {'kind': 'flat_fee', 'supported': False, 'label': 'Flat fee per trip',
                   'note': flow.NOT_SUPPORTED},
                  {'kind': 'subscription', 'supported': False, 'label': 'Driver subscription',
                   'note': flow.NOT_SUPPORTED}],
    }, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated, READ])
def rules(request):
    if request.method == 'POST':
        return _create_rule(request)
    qs = scoped(request.user, CommissionRule.objects.select_related('geo_node', 'vehicle_type'))
    params = request.query_params
    if params.get('country'):
        qs = qs.filter(country=params['country'].upper())
    if params.get('city'):
        qs = qs.filter(geo_node__code=params['city'])
    if params.get('vehicle_type'):
        qs = qs.filter(vehicle_type_id=params['vehicle_type'])
    if params.get('status'):
        qs = qs.filter(status=params['status'])
    qs = qs.order_by('-created_at', '-id')
    total = qs.count()
    return success_response({'count': total,
                             'results': [flow.rule_dict(r) for r in qs[:200]]},
                            status.HTTP_200_OK)


def _create_rule(request):
    if not WRITE.has_permission(request, None):
        return error_response(code='PERMISSION_DENIED', message=WRITE.message, field='general',
                              issue='pricing.write', status=status.HTTP_403_FORBIDDEN)
    d = request.data or {}
    try:
        rule = flow.create_draft(
            request.user, country=d.get('country'), city_code=d.get('city') or None,
            vehicle_type_id=d.get('vehicle_type') or None, kind=d.get('kind') or 'percentage',
            percent=d.get('percent'), effective_from=d.get('effective_from'),
            reason=str(d.get('reason') or ''), http_request=request)
    except ApprovalError as exc:
        return exc.response()
    return success_response(flow.rule_dict(rule), status.HTTP_201_CREATED)


@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([IsAuthenticated, READ])
def rule_detail(request, rule_id):
    rule = _rule_or_none(request.user, rule_id)
    if rule is None:
        return _not_found()
    if request.method == 'GET':
        history = (CommissionRule.objects.filter(
            country=rule.country, geo_node=rule.geo_node, vehicle_type=rule.vehicle_type)
            .select_related('geo_node', 'vehicle_type').order_by('-version', '-id'))
        notices = [{'id': n.id, 'status': n.status, 'old_percent': str(n.old_percent)
                    if n.old_percent is not None else None,
                    'new_percent': str(n.new_percent),
                    'effective_from': n.effective_from.isoformat(),
                    'created_at': n.created_at.isoformat()} for n in rule.notices.all()]
        return success_response({
            'rule': flow.rule_dict(rule),
            'history': [flow.rule_dict(r) for r in history],
            'driver_notices': notices,
            'driver_notice_note': 'Queued in the outbox; sending is stubbed until the '
                                  'Notifications module exists.',
            'config': flow.config_dict(rule.country, resolver.country_config(rule.country)),
        }, status.HTTP_200_OK)
    refused = _write_gate(request.user, rule)
    if refused is not None:
        return refused
    try:
        if request.method == 'DELETE':
            flow.discard_draft(request.user, rule, http_request=request)
            return success_response({'discarded': True}, status.HTTP_200_OK)
        d = request.data or {}
        flow.update_draft(request.user, rule, percent=d.get('percent'),
                          effective_from=d.get('effective_from'), reason=d.get('reason'),
                          http_request=request)
    except ApprovalError as exc:
        return exc.response()
    return success_response(flow.rule_dict(rule), status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated, READ])
def rule_preview(request, rule_id):
    """Driver-earnings preview for a rule (a draft, usually): the last 30 days of
    completed trips in its scope, replayed at the rule's percent."""
    rule = _rule_or_none(request.user, rule_id)
    if rule is None:
        return _not_found()
    result = resolver.earnings_preview(rule)
    result['rule'] = flow.rule_dict(rule)
    result['increase'] = flow.is_increase(
        rule.country, rule.geo_node, rule.vehicle_type_id, rule.percent, timezone.now())
    result['notice_days'] = resolver.notice_days(rule.country)
    return success_response(result, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, WRITE])
def rule_submit(request, rule_id):
    rule = _rule_or_none(request.user, rule_id)
    if rule is None:
        return _not_found()
    refused = _write_gate(request.user, rule)
    if refused is not None:
        return refused
    try:
        outcome = flow.submit(request.user, rule, reason=str((request.data or {}).get('reason')
                                                             or ''), http_request=request)
    except ApprovalError as exc:
        return exc.response()
    return success_response({**_approval_body(outcome), 'rule': flow.rule_dict(
        CommissionRule.objects.select_related('geo_node', 'vehicle_type').get(pk=rule.pk))},
        status.HTTP_202_ACCEPTED)


# ---------------------------------------------------------------------------
# Country config
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([IsAuthenticated, READ])
def config_list(request):
    codes = {n.code for n in scoped(request.user, GeoNode.objects.filter(kind='country'))}
    for city in scoped(request.user, GeoNode.objects.filter(kind='city')).select_related('parent'):
        if city.country is not None:
            codes.add(city.country.code)
    codes = sorted(c for c in codes if c in COUNTRIES)
    rows = {c.country: c for c in CommissionCountryConfig.objects.filter(country__in=codes)}
    return success_response({'results': [flow.config_dict(c, rows.get(c)) for c in codes]},
                            status.HTTP_200_OK)


@api_view(['PUT'])
@permission_classes([IsAuthenticated, WRITE])
def config_update(request, country):
    d = request.data or {}
    node = GeoNode.objects.filter(kind='country', code=country.upper()).first()
    if node is not None and scoped_or_none(
            request.user, GeoNode.objects.filter(pk=node.pk)) is None:
        return _not_found('country', 'country')
    try:
        outcome = flow.propose_country_config(
            request.user, country=country, notice_days=d.get('notice_days'),
            max_percent=d.get('max_percent'), clear_cap=bool(d.get('clear_cap')),
            reason=str(d.get('reason') or ''), http_request=request)
    except ApprovalError as exc:
        return exc.response()
    return success_response(_approval_body(outcome), status.HTTP_202_ACCEPTED)


# ---------------------------------------------------------------------------
# Fare cards
# ---------------------------------------------------------------------------

def _card_row(card, now):
    return {
        'id': card.id, 'version': card.version, 'is_active': card.is_active,
        'effective_from': card.effective_from.isoformat(),
        'effective_to': card.effective_to.isoformat() if card.effective_to else None,
        'state': ('retired' if not card.is_active else 'scheduled' if card.effective_from > now
                  else 'superseded' if card.effective_to and card.effective_to <= now
                  else 'live'),
        'notes': card.notes,
        **cards.jsonable(cards.card_values(card)),
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated, READ])
def fare_cards_overview(request):
    """Per in-scope zone x vehicle class: the live card, a scheduled one, and the
    version history. `?zone=` narrows it."""
    now = timezone.now()
    zones = scoped(request.user, ServiceZone.objects.filter(is_active=True)
                   .select_related('geo_node')).order_by('code')
    if request.query_params.get('zone'):
        zones = zones.filter(pk=request.query_params['zone'])
    zones = list(zones[:100])
    vts = list(VehicleType.objects.order_by('type'))
    by_key = {}
    for card in (RateCard.objects.filter(zone__in=zones)
                 .order_by('-version', '-effective_from')):
        by_key.setdefault((card.zone_id, card.vehicle_type_id), []).append(card)
    out = []
    for zone in zones:
        classes = []
        for vt in vts:
            history = by_key.get((zone.id, vt.id), [])
            live = next((c for c in history if _card_row(c, now)['state'] == 'live'), None)
            scheduled = next((c for c in history if _card_row(c, now)['state'] == 'scheduled'),
                             None)
            classes.append({
                'vehicle_type': vt.id, 'vehicle_type_name': vt.type,
                'live': _card_row(live, now) if live else None,
                'scheduled': _card_row(scheduled, now) if scheduled else None,
                'history': [_card_row(c, now) for c in history[:25]],
            })
        out.append({'zone': zone.id, 'code': zone.code, 'name': zone.name,
                    'country': zone.country, 'currency': zone.currency,
                    'distance_unit': zone.distance_unit, 'classes': classes})
    return success_response({'zones': out}, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, READ])
def fare_cards_preview(request):
    """Rider-fare preview: sample trips at off-peak and at night, the current card
    against a draft. Body: zone, vehicle_type, values (any of the card fields; the
    rest are taken from the current card)."""
    d = request.data or {}
    zone = scoped_or_none(request.user, ServiceZone.objects.all(), pk=d.get('zone'))
    if zone is None:
        return _not_found('zone', 'zone')
    vt = VehicleType.objects.filter(pk=d.get('vehicle_type')).first()
    if vt is None:
        return _not_found('vehicle class', 'vehicle_type')
    # "Old" is what a rider pays NOW (the card in force), "new" starts from the newest
    # version (a scheduled one, if any) with the draft's values on top.
    from .services import get_active_rate_card

    current = get_active_rate_card(zone, vt)
    newest = cards.current_card(zone, vt)
    old = cards.card_values(current) if current else None
    merged = dict(cards.card_values(newest) if newest else {})
    try:
        merged.update(cards._decimals({k: v for k, v in (d.get('values') or {}).items()
                                       if k in cards.VALUE_FIELDS}))
        missing = [f for f in cards.VALUE_FIELDS if f not in merged]
        if missing:
            return error_response(
                code='VALUES_REQUIRED', message='There is no current card; give every value.',
                field='values', issue=', '.join(missing), status=status.HTTP_400_BAD_REQUEST)
        cards.check_values(merged)
    except ApprovalError as exc:
        return exc.response()
    except (ValueError, ArithmeticError):
        return error_response(code='INVALID_FARE', message='Values must be numbers.',
                              field='values', issue='not numeric',
                              status=status.HTTP_400_BAD_REQUEST)
    return success_response({
        'zone': zone.code, 'vehicle_type': vt.type,
        'current_card': current.id if current else None,
        'preview': cards.preview_fares(zone, old, merged),
    }, status.HTTP_200_OK)


__all__ = ['DRAFT']
