"""Configure > Integrations > Maps (9 Nov): calls and cost per SKU, cost per completed trip, the
route cache hit rate, the free allowance used, and the monthly budget per country.

    GET /platform/admin/maps/                       platform.integrations.read
    PUT /platform/admin/maps/prices/<sku>/          platform.settings (audited)
    PUT /platform/admin/maps/budget/<country>/      platform.settings (audited)
"""

from decimal import Decimal, InvalidOperation

from django.conf import settings
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.countries import COUNTRIES
from base.utils import error_response, success_response
from servers.admin_access.bridge import ScopedPermission
from servers.admin_audit.models import actor_snapshot
from servers.admin_audit.services import record_admin_action
from servers.ride import maps_budget
from servers.ride.maps_models import MapsBudget, MapsSkuPrice

READ = ScopedPermission('platform.integrations.read')
WRITE = ScopedPermission('platform.settings')


def _price(p):
    return {'sku': p.sku, 'usd_per_1000': str(p.usd_per_1000), 'free_monthly_calls': p.free_monthly_calls,
            'note': p.note, 'updated_by': p.updated_by_label}


def _budget(b, code):
    return {'country': code, 'monthly_usd': str(b.monthly_usd) if b and b.monthly_usd is not None else None,
            'amber_percent': b.amber_percent if b else 80, 'red_percent': b.red_percent if b else 100,
            'degrade_longer_cache': b.degrade_longer_cache if b else True,
            'degrade_straight_line_eta': b.degrade_straight_line_eta if b else True,
            'degrade_autocomplete_5_chars': b.degrade_autocomplete_5_chars if b else True}


def _bad(field, message):
    return error_response(code='INVALID_VALUE', message=message, field=field, issue=message,
                          status=status.HTTP_400_BAD_REQUEST)


@api_view(['GET'])
@permission_classes([IsAuthenticated, READ])
def maps_usage(request):
    budgets = {b.country: b for b in MapsBudget.objects.all()}
    rows = []
    for code in sorted(COUNTRIES):
        cpt = maps_budget.cost_per_completed_trip(code)
        rows.append({
            'country': code, 'usd_to_local': settings.MAPS_FX_TO_LOCAL.get(code),
            'summary': maps_budget.month_summary(code),
            'cost_per_completed_trip_usd': str(cpt) if cpt is not None else None,
            'budget': _budget(budgets.get(code), code),
            'degrade_level': maps_budget.degrade_level(code)})
    return success_response({'countries': rows, 'prices': [_price(p) for p in
                                                           MapsSkuPrice.objects.order_by('sku')],
                             'all': maps_budget.month_summary()}, status.HTTP_200_OK)


@api_view(['PUT'])
@permission_classes([IsAuthenticated, WRITE])
def maps_price_update(request, sku):
    row = MapsSkuPrice.objects.filter(sku=sku).first()
    if row is None:
        return error_response(code='NOT_FOUND', message='No such SKU.', field='sku', issue=sku,
                              status=status.HTTP_404_NOT_FOUND)
    d, before = request.data or {}, _price(row)
    try:
        if 'usd_per_1000' in d:
            row.usd_per_1000 = Decimal(str(d['usd_per_1000']))
            if row.usd_per_1000 < 0:
                raise InvalidOperation
        if 'free_monthly_calls' in d:
            row.free_monthly_calls = int(d['free_monthly_calls'])
            if row.free_monthly_calls < 0:
                raise ValueError
    except (InvalidOperation, TypeError, ValueError):
        return _bad('usd_per_1000', 'Prices and free calls are numbers, not negative.')
    row.updated_by_label = actor_snapshot(request.user).get('actor_label') or f'admin {request.user.pk}'
    row.save()
    record_admin_action(request, action='maps_price_changed', target_type='maps_sku_price',
                        target_id=sku, before=before, after=_price(row), reason=str(d.get('reason') or ''))
    return success_response(_price(row), status.HTTP_200_OK)


@api_view(['PUT'])
@permission_classes([IsAuthenticated, WRITE])
def maps_budget_update(request, country):
    code = str(country).upper()
    if code not in COUNTRIES:
        return error_response(code='NOT_FOUND', message='No such country.', field='country', issue=code,
                              status=status.HTTP_404_NOT_FOUND)
    d = request.data or {}
    row, _ = MapsBudget.objects.get_or_create(country=code)
    before = _budget(row, code)
    try:
        if 'monthly_usd' in d:
            row.monthly_usd = None if d['monthly_usd'] in (None, '') else Decimal(str(d['monthly_usd']))
        for k in ('amber_percent', 'red_percent'):
            if k in d:
                setattr(row, k, int(d[k]))
    except (InvalidOperation, TypeError, ValueError):
        return _bad('monthly_usd', 'The budget and the percents are numbers.')
    for k in ('degrade_longer_cache', 'degrade_straight_line_eta', 'degrade_autocomplete_5_chars'):
        if k in d:
            if not isinstance(d[k], bool):
                return _bad(k, f'{k} is true or false.')
            setattr(row, k, d[k])
    if not 0 < row.amber_percent <= row.red_percent <= 1000:
        return _bad('amber_percent', 'Amber must be above 0 and not above red.')
    row.updated_by_label = actor_snapshot(request.user).get('actor_label') or f'admin {request.user.pk}'
    row.save()
    record_admin_action(request, action='maps_budget_changed', target_type='maps_budget',
                        target_id=code, before=before, after=_budget(row, code),
                        reason=str(d.get('reason') or ''))
    return success_response(_budget(row, code), status.HTTP_200_OK)
