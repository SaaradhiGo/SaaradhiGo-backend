"""The maker-checker API: `api/v1/approvals/admin/`.

CAPABILITIES, AND WHY THESE
---------------------------
  * Reading (`requests/`, `requests/<id>/`, `policies/`) needs `report.read`.
    Requests span finance, pricing, driver status and the policies
    themselves, so no single finance capability describes the reader;
    `report.read` is the shared console read surface and is held by City
    Ops, Finance, Marketing, Analyst and Super Admin. Note the Support
    Agent template does NOT hold it today, so a support maker cannot list
    their own refund requests until the owner decides whether the role
    gains `report.read` or a narrower capability is minted; recorded as a
    decision for the owner, not taken here.
  * `approve/` and `reject/` need `approval.check` (plus, inside the
    service, the action's own capability in the request's scope).
  * `cancel/` is the maker withdrawing their own request. It is gated on
    `report.read` for the same reason reading is, and the service refuses
    anybody but the maker.
  * `policies/change/` needs `platform.settings`; it never writes a policy
    itself, it raises an `approvals.policy_change` request.

Every route carries a `ScopedPermission`, which is what
`tests/test_admin_namespace_is_gated.py` walks the resolver for. Scope on
a single request is decided inside the service: out of scope is 404,
identical to a missing id.
"""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated

from base.utils import error_response, success_response
from servers.admin_access.bridge import ScopedPermission

from . import service
from .actions import ACTIONS
from .models import ALL_STATUSES, REQUESTED, ApprovalPolicy, ApprovalRequest
from .registry import registered_actions
from .scope import may_act, visible


def _paginate(request, queryset, page_size=20, max_page_size=100):
    paginator = PageNumberPagination()
    paginator.page_size = page_size
    paginator.page_size_query_param = 'page_size'
    paginator.max_page_size = max_page_size
    page = paginator.paginate_queryset(queryset, request)
    body = paginator.get_paginated_response([row.as_dict() for row in page]).data
    return success_response(body, status.HTTP_200_OK)


def _load(request, request_id, permission):
    """The request row if it is in the caller's scope, else None.

    `may_act` with `report.read` here mirrors `scoped_or_none`: a row the
    caller's scopes do not cover is reported exactly like a missing one.
    """
    row = (
        ApprovalRequest.objects.select_related('scope_node', 'policy')
        .filter(pk=request_id).first()
    )
    if row is None or not may_act(request.user, permission, row.scope_node):
        return None
    return row


def _not_found(request_id):
    return error_response(
        code='NOT_FOUND', message='Approval request not found',
        field='request_id', issue=f'No approval request matches id {request_id}',
        status=status.HTTP_404_NOT_FOUND,
    )


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('report.read')])
def list_requests(request):
    """Requests in the caller's scope.

    Filters: `?status=`, `?action=`, `?mine=1` (I am the maker),
    `?pending_for_me=1` (open, raised by somebody else -- the checker's
    queue; whether the caller may actually check each one is decided when
    they try), `?target_type=&target_id=`.
    """
    rows = visible(request.user, ApprovalRequest.objects.select_related('scope_node', 'policy'))

    status_filter = (request.query_params.get('status') or '').strip()
    if status_filter:
        if status_filter not in ALL_STATUSES:
            return error_response(
                code='INVALID_STATUS', message='Unknown status filter',
                field='status', issue=f'{status_filter!r} is not a request status',
                status=status.HTTP_400_BAD_REQUEST,
            )
        rows = rows.filter(status=status_filter)
    action = (request.query_params.get('action') or '').strip()
    if action:
        rows = rows.filter(action_key=action)
    if request.query_params.get('mine') in ('1', 'true'):
        rows = rows.filter(maker_id=request.user.pk)
    if request.query_params.get('pending_for_me') in ('1', 'true'):
        rows = rows.filter(status=REQUESTED).exclude(maker_id=request.user.pk)
    target_type = (request.query_params.get('target_type') or '').strip()
    if target_type:
        rows = rows.filter(target_type=target_type)
    target_id = (request.query_params.get('target_id') or '').strip()
    if target_id:
        rows = rows.filter(target_id=target_id)

    return _paginate(request, rows.order_by('-requested_at', '-id'))


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('report.read')])
def request_detail(request, request_id):
    row = _load(request, request_id, 'report.read')
    if row is None:
        return _not_found(request_id)
    return success_response(row.as_dict(), status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('approval.check')])
def approve_request(request, request_id):
    """Second admin says yes. Executes once; a repeat by the same checker
    returns the same outcome."""
    row = _load(request, request_id, 'approval.check')
    if row is None:
        return _not_found(request_id)
    try:
        row = service.approve(
            row, request.user, reason=str(request.data.get('reason', '') or ''),
            http_request=request,
        )
    except service.ApprovalError as exc:
        return exc.response()
    return success_response(row.as_dict(), status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('approval.check')])
def reject_request(request, request_id):
    row = _load(request, request_id, 'approval.check')
    if row is None:
        return _not_found(request_id)
    try:
        row = service.reject(
            row, request.user, reason=str(request.data.get('reason', '') or ''),
            http_request=request,
        )
    except service.ApprovalError as exc:
        return exc.response()
    return success_response(row.as_dict(), status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('report.read')])
def cancel_request(request, request_id):
    """The maker withdraws their own open request."""
    row = _load(request, request_id, 'report.read')
    if row is None:
        return _not_found(request_id)
    try:
        row = service.cancel(
            row, request.user, reason=str(request.data.get('reason', '') or ''),
            http_request=request,
        )
    except service.ApprovalError as exc:
        return exc.response()
    return success_response(row.as_dict(), status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('report.read')])
def list_policies(request):
    """Active thresholds, with the catalogue of actions and executors.

    `?country=` narrows; `?history=1` includes closed versions. Policies
    are configuration rather than rows with a place, so they are not
    scope-filtered: an India finance admin may read that the US has no
    thresholds, which is the point of the refusal being explicit.
    """
    rows = ApprovalPolicy.objects.all()
    country = (request.query_params.get('country') or '').strip().upper()
    if country:
        rows = rows.filter(country=country)
    if request.query_params.get('history') not in ('1', 'true'):
        rows = rows.filter(is_active=True)
    return success_response({
        'policies': [p.as_dict() for p in rows.order_by('country', 'action_key', '-version')[:500]],
        'actions': [
            {'key': a.key, 'description': a.description, 'capability': a.capability,
             'always_checker': a.always_checker, 'has_executor': a.key in registered_actions()}
            for a in ACTIONS.values()
        ],
    }, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('platform.settings')])
def change_policy(request):
    """Propose a threshold. Body: country, action_key, limit_kind
    (money|count|always), currency (money only), maker_limit (int minor
    units or count; null = always a checker), reason (required).

    Returns 202 with the `approvals.policy_change` request a second
    `platform.settings` holder must approve. Nothing is written until then.
    """
    data = request.data or {}
    maker_limit = data.get('maker_limit', None)
    if maker_limit is not None and not isinstance(maker_limit, bool):
        try:
            maker_limit = int(maker_limit)
        except (TypeError, ValueError):
            return error_response(
                code='AMOUNT_NOT_INTEGER', message='maker_limit must be an integer',
                field='maker_limit', issue=f'got {maker_limit!r}',
                status=status.HTTP_400_BAD_REQUEST,
            )
    try:
        outcome = service.propose_policy_change(
            request.user,
            country=str(data.get('country', '') or ''),
            action_key=str(data.get('action_key', '') or ''),
            limit_kind=str(data.get('limit_kind', '') or ''),
            currency=str(data.get('currency', '') or ''),
            maker_limit=maker_limit,
            reason=str(data.get('reason', '') or ''),
            http_request=request,
        )
    except service.ApprovalError as exc:
        return exc.response()
    return success_response(
        {'approval_request': outcome.request.as_dict(), 'duplicate': outcome.duplicate},
        status.HTTP_202_ACCEPTED,
    )
