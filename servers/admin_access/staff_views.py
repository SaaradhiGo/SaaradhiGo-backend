"""Staff and roles: who operates the console, with which role, where.

  GET  /admin-access/staff/                       operators, filterable
  GET  /admin-access/staff/options/               roles and places a role can be held at
  GET  /admin-access/staff/<id>/                  one operator, assignments and history
  POST /admin-access/staff/<id>/assignments/      grant a role at a place
  POST /admin-access/assignments/<id>/revoke/     take one away
  POST /admin-access/staff/<id>/deactivate/       switch the account off
  POST /admin-access/staff/<id>/reactivate/       switch it back on

Every route needs `admin.manage` held at Global (Super Admin): granting
authority is the one thing a narrower holder must never do for the world.
Writes also need a session that has cleared MFA, a reason, and refuse
the caller's own account, the break-glass account and the Super Admin
floor. Inviting a new operator stays `POST /auth/admin/invite/`.
"""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Prefetch, Q
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base import ops_mfa
from base.utils import error_response, success_response
from servers.admin_audit.context import audit_context
from servers.admin_audit.models import AdminAuditLog
from servers.admin_audit.services import record_admin_action

from .bridge import ScopedPermission, granted
from .masking import mask_phone
from .models import GLOBAL, AdminAssignment, AdminRole, GeoNode
from .roles import MIN_SUPER_ADMINS, PERMISSIONS, SUPER_ADMIN
from .services import (
    WouldLeaveTooFewSuperAdmins,
    active_super_admin_ids,
    revoke_assignment,
)

User = get_user_model()

MIN_REASON = 5
PAGE_SIZE = 50
HISTORY_LIMIT = 50


def _refuse(code, message, http_status=status.HTTP_400_BAD_REQUEST, field='general'):
    return error_response(code=code, message=message, field=field, issue=message,
                          status=http_status)


def _global_node():
    return GeoNode.objects.filter(kind=GLOBAL).first()


def _holds_globally(user):
    allowed, _how = granted(user, 'admin.manage', _global_node())
    return allowed


def _gate(request, *, write=False):
    """None when the caller may go on, else the refusal to return."""
    if not _holds_globally(request.user):
        return _refuse('FORBIDDEN', 'Managing staff needs admin.manage at Global.',
                       status.HTTP_403_FORBIDDEN)
    if write and not ops_mfa.console_access_allowed(request):
        return _refuse('AUTH_MFA_REQUIRED',
                       'Clear multi-factor authentication before changing staff access.',
                       status.HTTP_401_UNAUTHORIZED, 'mfa')
    return None


def _reason(request):
    return (request.data.get('reason') or '').strip()


def _operators():
    return User.objects.filter(Q(role='admin', is_staff=True) | Q(is_superuser=True))


def _assignment(a):
    return {
        'id': a.id,
        'role': a.role.code,
        'role_name': a.role.name,
        'scope': a.scope.code,
        'scope_name': a.scope.name,
        'scope_kind': a.scope.kind,
        'is_active': a.is_active,
        'granted_by': (a.granted_by.full_name or a.granted_by.email) if a.granted_by_id else None,
        'reason': a.reason,
        'created_at': a.created_at.isoformat(),
    }


def _person(user, assignments):
    return {
        'id': user.pk,
        'ref': f'OP-{user.pk}',
        'name': user.full_name or '',
        'email': user.email or '',
        'phone': mask_phone(user.phone_number),
        'is_active': user.is_active,
        'is_superuser': user.is_superuser,
        'is_break_glass': bool(getattr(user, 'is_break_glass', False)),
        'invite_pending': bool(getattr(user, 'must_change_password', False)),
        'mfa_enrolled': ops_mfa.totp_enrolled(user),
        'last_login': user.last_login.isoformat() if user.last_login else None,
        'created_at': user.created_at.isoformat() if getattr(user, 'created_at', None) else None,
        'assignments': [_assignment(a) for a in assignments],
    }


def _with_assignments(queryset):
    return queryset.prefetch_related(Prefetch(
        'admin_assignments',
        queryset=AdminAssignment.objects.select_related('role', 'scope', 'granted_by')
        .order_by('-is_active', 'scope__path'),
    ))


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('admin.manage')])
def staff_list(request):
    refused = _gate(request)
    if refused:
        return refused
    qs = _operators()
    state = request.query_params.get('status', 'active')
    if state == 'active':
        qs = qs.filter(is_active=True)
    elif state == 'inactive':
        qs = qs.filter(is_active=False)
    role = request.query_params.get('role')
    if role:
        qs = qs.filter(admin_assignments__role__code=role,
                       admin_assignments__is_active=True).distinct()
    q = (request.query_params.get('q') or '').strip()
    if q:
        qs = qs.filter(Q(full_name__icontains=q) | Q(email__icontains=q))
    qs = qs.order_by('full_name', 'pk')
    try:
        page = max(1, int(request.query_params.get('page', 1)))
    except ValueError:
        page = 1
    count = qs.count()
    rows = _with_assignments(qs)[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
    return success_response({
        'count': count,
        'results': [_person(u, u.admin_assignments.all()) for u in rows],
        'super_admins': len(active_super_admin_ids()),
    }, status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('admin.manage')])
def staff_options(request):
    refused = _gate(request)
    if refused:
        return refused
    roles = AdminRole.objects.filter(is_active=True).order_by('name')
    places = GeoNode.objects.exclude(status='paused').order_by('path')
    return success_response({
        'roles': [{
            'code': r.code, 'name': r.name, 'description': r.description,
            'global_only': r.code == SUPER_ADMIN or r.is_global_only,
            'permissions': [{'code': p, 'label': PERMISSIONS.get(p, p)}
                            for p in sorted(r.permissions or [])],
        } for r in roles],
        'scopes': [{'code': n.code, 'name': n.name, 'kind': n.kind} for n in places],
    }, status.HTTP_200_OK)


def _history(user):
    assignment_ids = [str(i) for i in
                      AdminAssignment.objects.filter(admin=user).values_list('id', flat=True)]
    rows = (AdminAuditLog.objects
            .filter(Q(target_type='User', target_id=str(user.pk))
                    | Q(target_type='AdminAssignment', target_id__in=assignment_ids))
            .order_by('-created_at')[:HISTORY_LIMIT])
    return [{
        'id': r.id, 'action': r.action, 'actor': r.actor_label or r.actor_email or 'system',
        'reason': r.reason, 'at': r.created_at.isoformat(),
        'role': (r.after or {}).get('role') or (r.before or {}).get('role'),
        'scope': (r.after or {}).get('scope') or (r.before or {}).get('scope'),
    } for r in rows]


def _get_operator(pk):
    return _with_assignments(_operators()).filter(pk=pk).first()


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('admin.manage')])
def staff_detail(request, pk):
    refused = _gate(request)
    if refused:
        return refused
    user = _get_operator(pk)
    if user is None:
        return _refuse('NOT_FOUND', 'No operator matches that id.', status.HTTP_404_NOT_FOUND)
    body = _person(user, user.admin_assignments.all())
    body['history'] = _history(user)
    body['is_self'] = user.pk == request.user.pk
    return success_response(body, status.HTTP_200_OK)


def _write_guard(request, user):
    """Refusals shared by every write on another operator's account."""
    if user is None:
        return _refuse('NOT_FOUND', 'No operator matches that id.', status.HTTP_404_NOT_FOUND)
    if user.pk == request.user.pk:
        return _refuse('SELF_CHANGE', 'Another Super Admin must change your own access.',
                       status.HTTP_403_FORBIDDEN)
    if getattr(user, 'is_break_glass', False):
        return _refuse('BREAK_GLASS', 'The break-glass account is managed from a shell only.',
                       status.HTTP_403_FORBIDDEN)
    if len(_reason(request)) < MIN_REASON:
        return _refuse('REASON_REQUIRED', 'Give a reason; it goes in the audit log.',
                       field='reason')
    return None


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('admin.manage')])
def staff_grant(request, pk):
    refused = _gate(request, write=True) or _write_guard(request, _get_operator(pk))
    if refused:
        return refused
    user = _get_operator(pk)
    if not user.is_active:
        return _refuse('INACTIVE', 'Reactivate the account before granting a role.')
    role = AdminRole.objects.filter(code=request.data.get('role'), is_active=True).first()
    scope = GeoNode.objects.filter(code=request.data.get('scope')).first()
    if role is None or scope is None:
        return _refuse('INVALID', 'Choose a role and a place.', field='role')
    existing = AdminAssignment.objects.filter(admin=user, role=role, scope=scope).first()
    if existing and existing.is_active:
        return _refuse('DUPLICATE', f'{user.full_name or user.email} already holds {role.name} '
                                    f'at {scope.name}.')
    reason = _reason(request)
    try:
        with transaction.atomic(), audit_context(actor=request.user, request=request,
                                                 reason=reason, action='staff_role_granted'):
            if existing:
                existing.is_active = True
                existing.granted_by = request.user
                existing.reason = reason
                existing.save(update_fields=['is_active', 'granted_by', 'reason'])
                row = existing
            else:
                row = AdminAssignment.objects.create(admin=user, role=role, scope=scope,
                                                     granted_by=request.user, reason=reason)
    except ValidationError as exc:
        return _refuse('INVALID', ' '.join(exc.messages), field='scope')
    row.refresh_from_db()
    return success_response(_assignment(row), status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('admin.manage')])
def assignment_revoke(request, pk):
    refused = _gate(request, write=True)
    if refused:
        return refused
    row = AdminAssignment.objects.select_related('role', 'scope', 'admin').filter(pk=pk).first()
    if row is None:
        return _refuse('NOT_FOUND', 'No assignment matches that id.', status.HTTP_404_NOT_FOUND)
    refused = _write_guard(request, _get_operator(row.admin_id))
    if refused:
        return refused
    if not row.is_active:
        return _refuse('ALREADY_REVOKED', 'That role has already been taken away.')
    try:
        with audit_context(actor=request.user, request=request, reason=_reason(request),
                           action='staff_role_revoked'):
            revoke_assignment(row, actor=request.user, reason=_reason(request))
    except WouldLeaveTooFewSuperAdmins as exc:
        return _refuse('SUPER_ADMIN_FLOOR', ' '.join(exc.messages), status.HTTP_409_CONFLICT)
    row.refresh_from_db()
    return success_response(_assignment(row), status.HTTP_200_OK)


def _set_active(request, pk, active):
    refused = _gate(request, write=True)
    if refused:
        return refused
    user = _get_operator(pk)
    refused = _write_guard(request, user)
    if refused:
        return refused
    if user.is_active == active:
        return _refuse('NO_CHANGE', 'The account is already ' + ('active.' if active else 'off.'))
    if not active and user.pk in active_super_admin_ids():
        remaining = active_super_admin_ids() - {user.pk}
        if len(active_super_admin_ids()) >= MIN_SUPER_ADMINS > len(remaining):
            return _refuse('SUPER_ADMIN_FLOOR', 'This would leave too few Super Admins. '
                                                'Appoint another first.', status.HTTP_409_CONFLICT)
    with transaction.atomic():
        user.is_active = active
        # Saving inactive revokes every session (auth_user.session_revocation).
        user.save(update_fields=['is_active'])
        record_admin_action(request, action='staff_reactivated' if active else 'staff_deactivated',
                            target_type='User', target_id=str(user.pk),
                            before={'is_active': not active}, after={'is_active': active},
                            reason=_reason(request))
    user = _get_operator(pk)
    return success_response(_person(user, user.admin_assignments.all()), status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('admin.manage')])
def staff_deactivate(request, pk):
    return _set_active(request, pk, False)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('admin.manage')])
def staff_reactivate(request, pk):
    return _set_active(request, pk, True)
