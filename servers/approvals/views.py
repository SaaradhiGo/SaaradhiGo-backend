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

from django.conf import settings
from django.db.models import F
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated

from base.utils import error_response, success_response
from servers.admin_access.bridge import ScopedPermission

from . import service
from base import refs

from .actions import ACTIONS, FINANCE_PAYOUT
from .models import ALL_STATUSES, REQUESTED, ApprovalPolicy, ApprovalRequest
from .registry import registered_actions
from .scope import may_act, visible


#: `?ordering=` on the request list: the console's column sorts, done by the database.
SORTS = {
    'id': 'id',
    'requested': 'requested_at',
    'decided': 'decided_at',
    'expires': 'expires_at',
    'amount': 'amount',
    'status': 'status',
    'action': 'action_key',
    'maker': 'maker_label',
}
DEFAULT_SORT = '-requested'


def _paginate(request, queryset, page_size=20, max_page_size=100):
    paginator = PageNumberPagination()
    paginator.page_size = page_size
    paginator.page_size_query_param = 'page_size'
    paginator.max_page_size = max_page_size
    page = paginator.paginate_queryset(queryset, request)
    body = paginator.get_paginated_response([shown(row, request.user) for row in page]).data
    # Whether the sole-Super-Admin exception is switched on here, so the
    # Command Centre can say so (owner, 2026-11-03).
    from .service import self_approval_switch
    switch = self_approval_switch()
    body['self_approval_on'] = switch['on']
    # 12 Nov: who turned it on and when, for the banner.
    body['self_approval'] = switch
    return success_response(body, status.HTTP_200_OK)


def _withdrawal_facts(row):
    """The payout a `finance.payout` request would release, and what stops it."""
    from servers.driver.models import WithdrawalRequest
    from servers.payments.payout_destination import (
        MISSING_FOR_OPERATORS, has_payout_destination,
    )

    payload = row.payload or {}
    ids = payload.get('withdrawal_ids') or [payload.get('withdrawal_id')]
    ids = [int(i) for i in ids if str(i or '').isdigit()]
    rows = (WithdrawalRequest.objects.filter(id__in=ids[:50])
            .select_related('driver', 'driver__user_id'))
    subjects, blockers = [], []
    for w in rows:
        name = (w.driver.user_id.full_name or '').strip()
        subjects.append({
            'ref': refs.ref(refs.PAYOUT, w.id), 'withdrawal_id': w.id,
            'driver_id': w.driver_id, 'driver_ref': refs.ref(refs.DRIVER, w.driver_id),
            'driver_name': name or f'Driver #{w.driver_id}',
            'amount_minor': w.amount_minor, 'currency': w.currency, 'status': w.status,
        })
        if not has_payout_destination(w.driver):
            blockers.append(f'{MISSING_FOR_OPERATORS} for {refs.ref(refs.DRIVER, w.driver_id)}')
    return subjects, blockers


def shown(row, user):
    """A request as the console shows it: the row, plus what it is about, what
    stops it, and whether THIS caller may decide it (and if not, why not)."""
    data = row.as_dict()
    action = ACTIONS.get(row.action_key)
    data['ref'] = refs.ref(refs.APPROVAL, row.id)
    data['title'] = action.description if action else row.action_key
    data['subjects'], data['blockers'] = [], []
    if row.action_key == FINANCE_PAYOUT:
        data['subjects'], data['blockers'] = _withdrawal_facts(row)
    if row.status == REQUESTED:
        problem = service.checker_problem(row, user)
        data['can_decide'] = problem is None
        data['cannot_decide_reason'] = problem
        data['can_cancel'] = row.maker_id == user.pk
        # Decidable only through the sole-Super-Admin exception: the console
        # asks for a reason and says it will be recorded as self-approved.
        data['self_approval'] = problem is None and row.maker_id == user.pk
    else:
        data['self_approval'] = False
        data['can_decide'] = False
        data['cannot_decide_reason'] = None
        data['can_cancel'] = False
    return data


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

    Sorted by `?ordering=` (one of `SORTS`, prefix `-` for descending;
    newest first by default). Filters: `?status=`, `?action=`, `?mine=1` (I am the maker),
    `?pending_for_me=1` (open and decidable by the caller -- the checker's
    queue), `?self_approved=1` (decided by their own maker under
    APPROVALS_SOLO_SUPER_ADMIN), `?target_type=&target_id=`.
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
        # Only what THIS caller can decide: a request they would be refused on is
        # not waiting for them (owner's new-hire run, 2026-11-03).
        # Their own requests count only under the sole-Super-Admin exception,
        # which `checker_problem` decides per row.
        rows = rows.filter(status=REQUESTED)
        if not settings.APPROVALS_SOLO_SUPER_ADMIN:
            rows = rows.exclude(maker_id=request.user.pk)
        decidable = [r.pk for r in rows.select_related('scope_node', 'policy')[:500]
                     if service.checker_problem(r, request.user) is None]
        rows = rows.filter(pk__in=decidable)
    if request.query_params.get('self_approved') in ('1', 'true'):
        rows = rows.filter(self_approved=True)
    target_type = (request.query_params.get('target_type') or '').strip()
    if target_type:
        rows = rows.filter(target_type=target_type)
    target_id = (request.query_params.get('target_id') or '').strip()
    if target_id:
        rows = rows.filter(target_id=target_id)

    ordering = (request.query_params.get('ordering') or DEFAULT_SORT).strip()
    key = ordering.lstrip('-')
    if key not in SORTS:
        return error_response(
            code='INVALID_ORDERING', message='Unknown sort', field='ordering',
            issue=f'ordering must be one of {", ".join(sorted(SORTS))} (prefix - for descending)',
            status=status.HTTP_400_BAD_REQUEST,
        )
    # Empty values (no amount, not decided yet) go last both ways: PostgreSQL puts
    # NULLs FIRST when descending, which filled "largest amount" with blanks.
    field = F(SORTS[key])
    first = field.desc(nulls_last=True) if ordering.startswith('-') else field.asc(nulls_last=True)
    return _paginate(request, rows.order_by(first, '-id' if ordering.startswith('-') else 'id'))


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('report.read')])
def request_detail(request, request_id):
    row = _load(request, request_id, 'report.read')
    if row is None:
        return _not_found(request_id)
    return success_response(shown(row, request.user), status.HTTP_200_OK)


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
    return success_response(shown(row, request.user), status.HTTP_200_OK)


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
    return success_response(shown(row, request.user), status.HTTP_200_OK)


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
    return success_response(shown(row, request.user), status.HTTP_200_OK)


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


# ---------------------------------------------------------------------------
# Self-approval (12 Nov): who turned it on, until when; a Super Admin sets or ends it
# ---------------------------------------------------------------------------

@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated, ScopedPermission('report.read')])
def self_approval(request):
    """GET  self-approval/  {on, source, by, at, expires_at, reason}
    POST self-approval/  {reason, days (1-7)}  Super Admin only: turn it on for a while.
    POST self-approval/  {end: true, reason}   Super Admin only: turn it off now."""
    from datetime import timedelta

    from django.utils import timezone

    from servers.admin_access import breakglass
    from servers.admin_access.access import is_super_admin
    from servers.admin_audit.services import record_admin_action

    from .models import SelfApprovalGrant
    from .service import self_approval_switch

    if request.method == 'GET':
        return success_response(self_approval_switch(), status.HTTP_200_OK)
    if not is_super_admin(request.user) or breakglass.is_break_glass(request.user):
        return error_response(code='SUPER_ADMIN_REQUIRED', message='Only a Super Admin can change this.',
                              field='', issue='not a super admin', status=status.HTTP_403_FORBIDDEN)
    reason = str(request.data.get('reason') or '').strip()
    if not reason:
        return error_response(code='REASON_REQUIRED', message='Say why.', field='reason',
                              issue='reason missing', status=status.HTTP_400_BAD_REQUEST)
    now = timezone.now()
    if request.data.get('end'):
        changed = SelfApprovalGrant.objects.filter(revoked_at__isnull=True, expires_at__gt=now).update(
            revoked_at=now, revoked_by=request.user)
        record_admin_action(request, action='approvals_self_approval_ended', target_type='self_approval',
                            target_id=0, before={'grants': changed}, after={'on': False}, reason=reason[:500])
        return success_response(self_approval_switch(), status.HTTP_200_OK)
    try:
        days = int(request.data.get('days') or 0)
    except (TypeError, ValueError):
        days = 0
    if not 1 <= days <= SelfApprovalGrant.MAX_DAYS:
        return error_response(code='INVALID_EXPIRY', message='Choose 1 to 7 days.', field='days',
                              issue='expiry out of range', status=status.HTTP_400_BAD_REQUEST)
    grant = SelfApprovalGrant.objects.create(
        granted_by=request.user, granted_by_label=(request.user.full_name or request.user.email or '')[:255],
        reason=reason[:2000], expires_at=now + timedelta(days=days))
    record_admin_action(request, action='approvals_self_approval_granted', target_type='self_approval',
                        target_id=grant.id, before={'on': self_approval_switch(now)['on']},
                        after={'on': True, 'expires_at': grant.expires_at.isoformat(), 'days': days},
                        reason=reason[:500])
    return success_response(self_approval_switch(), status.HTTP_201_CREATED)
