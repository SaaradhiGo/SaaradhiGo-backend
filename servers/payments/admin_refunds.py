"""Refunds, in one list: GET /payments/admin/refunds/

  ?view=waiting          refund requests waiting for a checker (approvals)
  ?view=pending_gateway  approved gateway refunds not yet sent to the provider
  ?view=done             completed or recorded refunds
  ?view=all              every refund ledger row (default)
  &trip=<id>  &currency=  &from=&to=  &scope=

Read-only, `finance.read`, scoped to the trip's city like every money list.
A refund is a `TripMoneyAction(kind=refund)` written by the approvals
executor; one still waiting for a checker is only an `ApprovalRequest`.
"""

from django.db.models import Sum
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.utils import success_response
from servers.admin_access.bridge import ScopedPermission
from servers.approvals import scope as approval_scope
from servers.approvals.actions import FINANCE_REFUND, SUPPORT_REFUND
from servers.approvals.models import REQUESTED, ApprovalRequest
from servers.ride.models import TripMoneyAction

from .admin_finance import _bad, _city, _page, _scope_node, _totals_by_currency
from .finance_common import BadParam, city_day_q, parse_currency, parse_int, parse_range
from .scope import money_visible_to

READ = 'finance.read'
VIEWS = ('all', 'waiting', 'pending_gateway', 'done')
REFUND_ACTIONS = (SUPPORT_REFUND, FINANCE_REFUND)


def _who(user):
    if user is None:
        return None
    return user.full_name or user.email or f'Operator #{user.pk}'


def refund_row(a):
    trip = a.trip
    return {
        'id': a.id, 'source': 'ledger', 'trip_id': a.trip_id,
        'amount_minor': a.amount_minor, 'currency': a.currency,
        'method': a.method, 'method_label': a.get_method_display(),
        'status': a.status, 'reason': a.reason,
        'city': _city(trip.zone.geo_node if trip.zone_id else None),
        'created_at': a.created_at, 'created_by': _who(a.created_by),
        'approval_request_id': a.approval_request_id,
    }


def waiting_row(r):
    trip_id = r.target_id if r.target_type == 'trip' else None
    return {
        'id': r.id, 'source': 'approval', 'trip_id': int(trip_id) if trip_id else None,
        'amount_minor': r.amount, 'currency': r.currency,
        'method': (r.payload or {}).get('method', ''), 'method_label': '',
        'status': 'waiting', 'reason': r.request_reason,
        'city': _city(r.scope_node), 'created_at': r.requested_at,
        'created_by': r.maker_label or r.maker_email, 'approval_request_id': r.id,
        'expires_at': r.expires_at,
    }


def _waiting(request, node):
    qs = approval_scope.visible(request.user, ApprovalRequest.objects.select_related('scope_node')
                                .filter(action_key__in=REFUND_ACTIONS, status=REQUESTED))
    if node is not None:
        qs = qs.filter(scope_node__path__startswith=node.path)
    currency = parse_currency(request)
    if currency:
        qs = qs.filter(currency=currency)
    trip = parse_int(request, 'trip')
    if trip:
        qs = qs.filter(target_type='trip', target_id=str(trip))
    return qs.order_by('-requested_at', '-id')


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission(READ)])
def finance_refunds(request):
    """GET /payments/admin/refunds/ — see the module docstring."""
    node, refusal = _scope_node(request)
    if refusal:
        return refusal
    view = request.query_params.get('view') or 'all'
    if view not in VIEWS:
        view = 'all'
    try:
        waiting = _waiting(request, node)
        counts = {'waiting': waiting.count()}
        if view == 'waiting':
            return success_response(_page(request, waiting, waiting_row, {
                'view': view, 'counts': counts,
                'totals': _totals_by_currency(waiting, amount_minor=Sum('amount')),
            }), status.HTTP_200_OK)

        qs = money_visible_to(
            request.user, TripMoneyAction, node=node, permission=READ,
            queryset=TripMoneyAction.objects.filter(kind=TripMoneyAction.REFUND)
            .select_related('trip__zone__geo_node', 'created_by'))
        currency = parse_currency(request)
        if currency:
            qs = qs.filter(currency=currency)
        trip = parse_int(request, 'trip')
        if trip:
            qs = qs.filter(trip_id=trip)
        d_from, d_to = parse_range(request)
        if d_from or d_to:
            qs = qs.filter(city_day_q('trip__zone__geo_node_id', 'created_at', d_from, d_to,
                                      include_unplaced=False))
    except BadParam as exc:
        return _bad(exc)
    counts['pending_gateway'] = qs.filter(status=TripMoneyAction.PENDING_GATEWAY).count()
    if view == 'pending_gateway':
        qs = qs.filter(status=TripMoneyAction.PENDING_GATEWAY)
    elif view == 'done':
        qs = qs.exclude(status=TripMoneyAction.PENDING_GATEWAY)
    qs = qs.order_by('-created_at', '-id')
    return success_response(_page(request, qs, refund_row, {
        'view': view, 'counts': counts,
        'totals': _totals_by_currency(qs, amount_minor=Sum('amount_minor')),
    }), status.HTTP_200_OK)
