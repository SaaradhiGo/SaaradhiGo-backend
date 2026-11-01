"""The audit-log viewer API: `api/v1/admin-audit/admin/`. Read-only. Capability `audit.read`.

  GET /admin-audit/admin/entries/?actor&action&target_type&target_id&from&to&city&page&page_size
        newest first, scoped (see `viewer`), plus `chain`: hash-chain verification for the range
  GET /admin-audit/admin/entries/<id>/        one entry with before/after, masked
  GET /admin-audit/admin/entries/export/?...  CSV of the same filters; audited, rate limited,
                                              refused if the export cannot be logged

An entry outside the caller's scope is 404, exactly like a missing id; a caller without
`audit.read` is 403.
"""

from django.http import HttpResponse
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.throttling import UserRateThrottle

from base.utils import error_response, success_response
from servers.admin_access.bridge import ScopedPermission

from . import viewer
from .models import AdminAuditLog
from .services import record_admin_action

READ = ScopedPermission('audit.read')


class AuditExportThrottle(UserRateThrottle):
    scope = 'audit_export'

    def get_cache_key(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return None
        return self.cache_format % {'scope': self.scope, 'ident': request.user.pk}


def _bad(exc):
    return error_response(code='VALIDATION_ERROR', message=exc.message, field=exc.field,
                          issue=exc.message, status=status.HTTP_400_BAD_REQUEST)


def _not_found():
    return error_response(code='NOT_FOUND', message='Entry not found', field='id',
                          issue='No entry matches', status=status.HTTP_404_NOT_FOUND)


@api_view(['GET'])
@permission_classes([IsAuthenticated, READ])
def entry_list(request):
    try:
        qs = viewer.filtered(request.user, request.query_params)
    except viewer.FilterError as exc:
        return _bad(exc)
    paginator = PageNumberPagination()
    paginator.page_size = viewer.PAGE_SIZE
    paginator.page_size_query_param = 'page_size'
    paginator.max_page_size = viewer.MAX_PAGE_SIZE
    page = paginator.paginate_queryset(qs, request)
    body = paginator.get_paginated_response([viewer.entry_row(e) for e in page]).data
    # The chain is verified over the filtered date window. It is a verdict and ids only.
    body['chain'] = viewer.verify_range(qs) if request.query_params.get('verify', '1') != '0' \
        else None
    return success_response(body, status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated, READ])
def entry_detail(request, entry_id):
    entry = viewer.scope_queryset(request.user, AdminAuditLog.objects.filter(pk=entry_id)).first()
    if entry is None:
        return _not_found()
    return success_response(viewer.entry_detail(entry), status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated, READ])
@throttle_classes([AuditExportThrottle])
def entry_export(request):
    try:
        qs = viewer.filtered(request.user, request.query_params)
    except viewer.FilterError as exc:
        return _bad(exc)
    text, count = viewer.csv_text(qs)
    filters = {k: request.query_params.get(k) for k in
               ('actor', 'action', 'target_type', 'target_id', 'from', 'to', 'city')
               if request.query_params.get(k)}
    audit = record_admin_action(
        request, action=viewer.EXPORT_ACTION, target_type='audit_log', target_id='export',
        after={'filters': filters, 'row_count': count,
               'truncated': count >= viewer.EXPORT_MAX_ROWS}, reason='')
    if audit is None:
        return error_response(
            code='AUDIT_UNAVAILABLE', message='The export could not be logged, so it was not produced.',
            field='general', issue='audit write failed', status=status.HTTP_503_SERVICE_UNAVAILABLE)
    response = HttpResponse(text, content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = 'attachment; filename="audit-log.csv"'
    response['Cache-Control'] = 'no-store'
    return response
