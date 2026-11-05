"""Data requests: a rider's or driver's export or erasure, handled from the console.

  GET  /auth/admin/data-requests/                 ?status=open|closed|all&kind=
  POST /auth/admin/data-requests/                 {subject: "R-57"|"D-12", kind, channel, reference, note}
  GET  /auth/admin/data-requests/<id>/            the request, the person, its history
  POST /auth/admin/data-requests/<id>/export/     the export as JSON; marks an export completed
  POST /auth/admin/data-requests/<id>/erase/      {reason} raises a `privacy.erasure` request
  POST /auth/admin/data-requests/<id>/refuse/     {reason}

All `privacy.manage` (Super Admin). Erasure is `servers.auth_user.erasure.anonymise_user`
and always waits for a second approver; nothing here deletes a row. Self-service
requests from the app are logged by `log_self_service`, already completed.
"""

from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.countries import country_of_phone
from base.utils import error_response, success_response
from servers.admin_access.bridge import ScopedPermission
from servers.admin_access.masking import mask_phone
from servers.admin_audit.models import AdminAuditLog
from servers.admin_audit.services import record_admin_action
from servers.approvals import registry
from servers.approvals import service as approvals
from servers.approvals.actions import PRIVACY_ERASURE
from servers.approvals.service import ApprovalError

from .models import DataRequest

User = get_user_model()
PERM = 'privacy.manage'
MIN_REASON = 10
PAGE_SIZE = 50


def due_days():
    # 30 days is our working deadline; counsel to confirm the DPDP / CCPA figure.
    return int(getattr(settings, 'DATA_REQUEST_DUE_DAYS', 30))


def _refuse(code, message, http_status=status.HTTP_400_BAD_REQUEST, field='general'):
    return error_response(code=code, message=message, field=field, issue=message,
                          status=http_status)


def _role(user):
    return 'driver' if getattr(user, 'role', '') == 'driver' else 'rider'


def _ref(user):
    if _role(user) == 'driver':
        driver = _driver_of(user)
        if driver is not None:
            return f'D-{driver.pk}'
    return f'R-{user.pk}'


def _driver_of(user):
    from servers.driver.models import Driver
    return Driver.objects.filter(user_id=user).first()


def _new(user, kind, channel, *, created_by=None, reference='', note='', status_=None):
    now = timezone.now()
    return DataRequest.objects.create(
        kind=kind, subject=user, subject_role=_role(user),
        country=getattr(country_of_phone(user.phone_number), 'code', '') or '',
        channel=channel, reference=reference, note=note,
        status=status_ or DataRequest.OPEN, received_at=now,
        due_at=now + timedelta(days=due_days()), created_by=created_by,
        closed_at=now if status_ == DataRequest.COMPLETED else None,
    )


def log_self_service(user, kind):
    """A request made and fulfilled in the app, logged so the register is complete."""
    if _role(user) not in ('rider', 'driver') or getattr(user, 'is_staff', False):
        return None
    return _new(user, kind, DataRequest.APP, status_=DataRequest.COMPLETED,
                reference=f'self-service user {user.pk}')


def _row(r):
    now = timezone.now()
    open_ = r.status in (DataRequest.OPEN, DataRequest.WAITING)
    return {
        'id': r.id, 'ref': f'DR-{r.id}', 'kind': r.kind, 'kind_label': r.get_kind_display(),
        'subject_id': r.subject_id, 'subject_ref': _ref(r.subject),
        'subject_role': r.subject_role, 'subject_name': r.subject.full_name or '',
        'subject_phone': mask_phone(r.subject.phone_number),
        'country': r.country, 'channel': r.channel, 'channel_label': r.get_channel_display(),
        'reference': r.reference, 'note': r.note,
        'status': r.status, 'status_label': r.get_status_display(),
        'received_at': r.received_at, 'due_at': r.due_at, 'closed_at': r.closed_at,
        'overdue': open_ and r.due_at < now, 'outcome': r.outcome,
        'approval_request_id': r.approval_request_id,
        'created_by': _who(r.created_by), 'handled_by': _who(r.handled_by),
    }


def _who(user):
    if user is None:
        return None
    return user.full_name or user.email or f'Operator #{user.pk}'


def _subject(ref):
    ref = (ref or '').strip().upper()
    if ref.startswith('D-') and ref[2:].isdigit():
        driver = _driver_of_pk(int(ref[2:]))
        return driver.user_id if driver else None
    if ref.startswith('R-') and ref[2:].isdigit():
        return User.objects.filter(pk=int(ref[2:]), role='rider').first()
    return None


def _driver_of_pk(pk):
    from servers.driver.models import Driver
    return Driver.objects.select_related('user_id').filter(pk=pk).first()


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated, ScopedPermission(PERM)])
def data_requests(request):
    if request.method == 'POST':
        return _create(request)
    qs = DataRequest.objects.select_related('subject', 'created_by', 'handled_by')
    state = request.query_params.get('status', 'open')
    if state == 'open':
        qs = qs.filter(status__in=(DataRequest.OPEN, DataRequest.WAITING))
    elif state == 'closed':
        qs = qs.filter(status__in=(DataRequest.COMPLETED, DataRequest.REFUSED))
    kind = request.query_params.get('kind')
    if kind in (DataRequest.EXPORT, DataRequest.ERASURE):
        qs = qs.filter(kind=kind)
    try:
        page = max(1, int(request.query_params.get('page', 1)))
    except ValueError:
        page = 1
    open_qs = DataRequest.objects.filter(status__in=(DataRequest.OPEN, DataRequest.WAITING))
    return success_response({
        'count': qs.count(),
        'results': [_row(r) for r in qs[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]],
        'open': open_qs.count(),
        'overdue': open_qs.filter(due_at__lt=timezone.now()).count(),
        'due_days': due_days(),
    }, status.HTTP_200_OK)


def _create(request):
    user = _subject(request.data.get('subject'))
    if user is None:
        return _refuse('NOT_FOUND', 'No rider or driver matches that reference (R-… or D-…).',
                       field='subject')
    if getattr(user, 'is_staff', False) or user.role == 'admin':
        return _refuse('NOT_ALLOWED', 'Operator accounts are deactivated in Staff and roles, '
                                      'not erased.', field='subject')
    kind = request.data.get('kind')
    channel = request.data.get('channel') or DataRequest.EMAIL
    if kind not in (DataRequest.EXPORT, DataRequest.ERASURE):
        return _refuse('INVALID', 'Choose export or erasure.', field='kind')
    if channel not in dict(DataRequest.CHANNEL_CHOICES) or channel == DataRequest.APP:
        return _refuse('INVALID', 'Choose how the request arrived.', field='channel')
    reference = (request.data.get('reference') or '').strip()[:128]
    if not reference:
        return _refuse('INVALID', 'Give the ticket number or email subject it came with.',
                       field='reference')
    with transaction.atomic():
        row = _new(user, kind, channel, created_by=request.user, reference=reference,
                   note=(request.data.get('note') or '').strip())
        record_admin_action(request, action='data_request_logged', target_type='DataRequest',
                            target_id=str(row.pk), after={'kind': kind, 'channel': channel},
                            reason=reference)
    return success_response(_row(row), status.HTTP_201_CREATED)


def _get(pk):
    return DataRequest.objects.select_related('subject', 'created_by', 'handled_by').filter(
        pk=pk).first()


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission(PERM)])
def data_request_detail(request, pk):
    row = _get(pk)
    if row is None:
        return _refuse('NOT_FOUND', 'No data request matches that id.', status.HTTP_404_NOT_FOUND)
    body = _row(row)
    logs = (AdminAuditLog.objects
            .filter(Q(target_type='DataRequest', target_id=str(row.pk))
                    | Q(target_type='ApprovalRequest', target_id=str(row.approval_request_id or 0)))
            .order_by('-created_at')[:50])
    body['history'] = [{'id': e.id, 'action': e.action, 'actor': e.actor_label or e.actor_email
                        or 'system', 'reason': e.reason, 'at': e.created_at} for e in logs]
    return success_response(body, status.HTTP_200_OK)


def _open_or_refuse(row):
    if row is None:
        return _refuse('NOT_FOUND', 'No data request matches that id.', status.HTTP_404_NOT_FOUND)
    if row.status != DataRequest.OPEN:
        return _refuse('CLOSED', f'This request is {row.get_status_display().lower()}.',
                       status.HTTP_409_CONFLICT)
    return None


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission(PERM)])
def data_request_export(request, pk):
    from .dpdp_views import export_payload

    row = _get(pk)
    if row is None:
        return _refuse('NOT_FOUND', 'No data request matches that id.', status.HTTP_404_NOT_FOUND)
    if row.kind != DataRequest.EXPORT:
        return _refuse('INVALID', 'Only an export request produces an export.')
    if row.status == DataRequest.REFUSED:
        return _refuse('CLOSED', 'This request was refused.', status.HTTP_409_CONFLICT)
    payload = export_payload(row.subject)
    with transaction.atomic():
        if row.status == DataRequest.OPEN:
            row.status, row.closed_at, row.handled_by = (DataRequest.COMPLETED, timezone.now(),
                                                         request.user)
            row.outcome = 'Export produced and downloaded from the console.'
            row.save(update_fields=['status', 'closed_at', 'handled_by', 'outcome'])
        record_admin_action(request, action='data_export_downloaded', target_type='DataRequest',
                            target_id=str(row.pk), reason=row.reference)
    return success_response({'request': _row(row), 'export': payload}, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission(PERM)])
def data_request_erase(request, pk):
    row = _get(pk)
    refused = _open_or_refuse(row)
    if refused:
        return refused
    if row.kind != DataRequest.ERASURE:
        return _refuse('INVALID', 'Only an erasure request can erase an account.')
    reason = (request.data.get('reason') or '').strip()
    if len(reason) < MIN_REASON:
        return _refuse('REASON_REQUIRED', 'Say how the person was verified and why it is due.',
                       field='reason')
    try:
        with transaction.atomic():
            outcome = approvals.require_approval_or_execute(
                PRIVACY_ERASURE, actor=request.user, country=row.country or 'IN',
                target_type='DataRequest', target_id=str(row.pk),
                payload={'data_request_id': row.pk}, reason=reason,
                idempotency_key=f'privacy.erasure:{row.pk}', http_request=request)
            row.approval_request = outcome.request
            row.status = DataRequest.COMPLETED if outcome.executed else DataRequest.WAITING
            row.handled_by = request.user
            row.save(update_fields=['approval_request', 'status', 'handled_by'])
    except ApprovalError as exc:
        return _refuse(exc.code or 'APPROVAL', exc.message, status.HTTP_409_CONFLICT)
    row.refresh_from_db()
    return success_response(_row(row), status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission(PERM)])
def data_request_refuse(request, pk):
    row = _get(pk)
    refused = _open_or_refuse(row)
    if refused:
        return refused
    reason = (request.data.get('reason') or '').strip()
    if len(reason) < MIN_REASON:
        return _refuse('REASON_REQUIRED', 'Say why; the person is told this.', field='reason')
    with transaction.atomic():
        row.status, row.closed_at, row.handled_by, row.outcome = (
            DataRequest.REFUSED, timezone.now(), request.user, reason)
        row.save(update_fields=['status', 'closed_at', 'handled_by', 'outcome'])
        record_admin_action(request, action='data_request_refused', target_type='DataRequest',
                            target_id=str(row.pk), reason=reason)
    return success_response(_row(row), status.HTTP_200_OK)


def execute_erasure(request_row):
    """Run an approved erasure: anonymise the subject, close the data request."""
    from django.contrib.auth import get_user_model as _users

    from .erasure import anonymise_user

    with transaction.atomic():
        row = DataRequest.objects.select_for_update().get(
            pk=(request_row.payload or {}).get('data_request_id'))
        if row.status == DataRequest.COMPLETED:
            return {'data_request_id': row.pk, 'status': row.status}
        checker = _users().objects.filter(pk=request_row.checker_id).first()
        anonymise_user(row.subject, reason=request_row.request_reason or 'erasure request',
                       reference=row.reference or f'DR-{row.pk}', actor=checker or None,
                       actor_label='' if checker else 'privacy.erasure')
        row.status, row.closed_at = DataRequest.COMPLETED, timezone.now()
        row.outcome = 'Account anonymised; financial records kept for the legal retention period.'
        row.save(update_fields=['status', 'closed_at', 'outcome'])
    return {'data_request_id': row.pk, 'status': row.status}


def _erasure_closed(request_row, outcome):
    """Rejected, cancelled or expired: the data request goes back to open."""
    DataRequest.objects.filter(approval_request=request_row, status=DataRequest.WAITING).update(
        status=DataRequest.OPEN, outcome=f'Erasure approval {outcome}; decide again.')


registry.register(PRIVACY_ERASURE)(execute_erasure)
registry.on_closed(PRIVACY_ERASURE)(_erasure_closed)
