"""Pricing > Trip charges (owner brief, 26 Nov): the catalogue of tolls, airport fees, waiting and permits.

    GET   /pricing/admin/trip-charges/                    zone.read: per country, items, scheduled, requests
    GET   /pricing/admin/trip-charges/history/            zone.read: ?country=&code= every version
    POST  /pricing/admin/trip-charges/preview/            zone.read: {country, code?, op?, item fields}
    POST  /pricing/admin/trip-charges/edit/               pricing.write -> 202: the change, for a second person
"""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.utils import success_response
from servers.approvals.service import ApprovalError

from . import trip_charges
from .fee_plan_views import READ, WRITE, _approval_body


def _countries():
    from .models import ServiceZone
    from .trip_charge_models import TripChargeItem

    return sorted({c.upper() for c in ServiceZone.objects.values_list('country', flat=True) if c}
                  | set(TripChargeItem.objects.values_list('country', flat=True)))


def _places(country):
    from servers.admin_access.models import GeoNode

    country_node = GeoNode.objects.filter(kind='country', code=country).first()
    rows = [{'scope_code': '', 'name': country_node.name if country_node else country}]
    rows += [{'scope_code': n.code, 'name': n.name}
             for n in GeoNode.objects.filter(kind__in=('state', 'city'), code__startswith=f'{country}-')
             .order_by('kind', 'name')]
    return rows


def _requests(country):
    from servers.approvals.actions import PRICING_CHANGE
    from servers.approvals.models import ApprovalRequest

    rows = (ApprovalRequest.objects.filter(action_key=PRICING_CHANGE, target_type=trip_charges.KIND,
                                           target_id__startswith=f'{country}:')
            .order_by('-requested_at')[:30])
    return [{'id': r.id, 'ref': r.public_ref, 'status': r.status, 'maker': r.maker_label, 'maker_id': r.maker_id,
             'reason': r.request_reason, 'decision_reason': r.decision_reason, 'checker': r.checker_label,
             'requested_at': r.requested_at.isoformat(), 'code': (r.payload or {}).get('code'),
             'op': (r.payload or {}).get('op'), 'name': ((r.payload or {}).get('item') or {}).get('name'),
             'requested_from': (r.payload or {}).get('requested_from'),
             'changes': ((r.payload or {}).get('display') or {}).get('changes')}
            for r in rows]


def _toll_checks(country):
    """This month's Google toll checks and their list cost, from the Maps meter."""
    from django.db.models import Sum
    from django.utils import timezone

    from servers.ride.maps_budget import list_cost_usd
    from servers.ride.maps_models import MapsUsage

    from .toll_check import SKU

    today = timezone.now().date()
    n = (MapsUsage.objects.filter(sku=SKU, country=country, day__gte=today.replace(day=1))
         .aggregate(n=Sum('count'))['n'] or 0)
    try:
        cost = str(list_cost_usd({SKU: n}))
    except Exception:  # noqa: BLE001
        cost = None
    return {'calls_this_month': n, 'list_cost_usd': cost}


@api_view(['GET'])
@permission_classes([IsAuthenticated, READ])
def trip_charges_list(request):
    from . import edit_pattern

    out = []
    for country in _countries():
        row = trip_charges.catalogue(country)
        row['places'] = _places(country)
        row['classes'] = [{'vehicle_type': vt, 'name': edit_pattern.class_word(vt)}
                          for vt in edit_pattern.offered_classes(country)]
        row['requests'] = _requests(country)
        row['toll_checks'] = _toll_checks(country)
        out.append(row)
    from django.conf import settings

    # A QA sample value may be marked only outside production (it is shown as such everywhere).
    return success_response({'results': out, 'sample_allowed': not getattr(settings, 'IS_PRODUCTION', False)},
                            status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated, READ])
def trip_charges_history(request):
    country = str(request.query_params.get('country') or '').upper()
    code = str(request.query_params.get('code') or '')
    return success_response({'results': trip_charges.history(country, code)}, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, READ])
def trip_charges_preview(request):
    d = request.data or {}
    try:
        body = trip_charges.preview(str(d.get('country') or ''), d)
    except ApprovalError as exc:
        return exc.response()
    return success_response(body, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, WRITE])
def trip_charges_edit(request):
    d = request.data or {}
    try:
        outcome = trip_charges.propose(request.user, str(d.get('country') or ''), d, http_request=request)
    except ApprovalError as exc:
        return exc.response()
    return success_response(_approval_body(outcome), status.HTTP_202_ACCEPTED)
