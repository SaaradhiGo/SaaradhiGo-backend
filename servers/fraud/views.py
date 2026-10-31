"""The fraud flag list (read-only). Module 13 adds review actions, assignment and the rest.

  GET /fraud/admin/flags/    fraud.read    scoped list: ?kind= &status= &limit= &offset=

A flag is anchored at its subject's city (else country); out of scope never appears. The subject
is a masked reference (`rider #123`), never a name or a contact, and the evidence is signal
names, ids and amounts, never a raw value (see `fraud.models`).
"""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.utils import error_response, success_response
from servers.admin_access.bridge import ScopedPermission
from servers.admin_access.scoping import ScopeNotHeld, scoped_to_request

from .models import KIND_CHOICES, STATUS_CHOICES, RiskFlag

READ = ScopedPermission('fraud.read')
DEFAULT_LIMIT, MAX_LIMIT = 50, 100


def flag_dict(flag):
    role = flag.subject.role if flag.subject.role in ('rider', 'driver') else 'user'
    return {
        'id': flag.id, 'kind': flag.kind, 'status': flag.status,
        'subject': {'ref': f'{role} #{flag.subject_id}', 'role': role,
                    'user_id': flag.subject_id},
        'evidence': flag.evidence, 'source_ref': flag.source_ref,
        'scope': flag.anchor_node.code if flag.anchor_node_id else None,
        'scope_name': flag.anchor_node.name if flag.anchor_node_id else None,
        'created_at': flag.created_at.isoformat(),
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated, READ])
def flag_list(request):
    try:
        qs = scoped_to_request(request, RiskFlag.objects.select_related('subject', 'anchor_node'))
    except ScopeNotHeld as exc:
        return error_response(code='SCOPE_NOT_HELD', message=str(exc), field='scope',
                              issue=str(exc), status=status.HTTP_403_FORBIDDEN)
    params = request.query_params
    kind, state = (params.get('kind') or '').strip(), (params.get('status') or '').strip()
    if kind:
        if kind not in {k for k, _ in KIND_CHOICES}:
            return error_response(code='INVALID_FILTER', message='Unknown kind', field='kind',
                                  issue=kind, status=status.HTTP_400_BAD_REQUEST)
        qs = qs.filter(kind=kind)
    if state:
        if state not in {s for s, _ in STATUS_CHOICES}:
            return error_response(code='INVALID_FILTER', message='Unknown status',
                                  field='status', issue=state,
                                  status=status.HTTP_400_BAD_REQUEST)
        qs = qs.filter(status=state)
    try:
        limit = max(1, min(int(params.get('limit', DEFAULT_LIMIT)), MAX_LIMIT))
        offset = max(0, int(params.get('offset', 0)))
    except (TypeError, ValueError):
        limit, offset = DEFAULT_LIMIT, 0
    total = qs.count()
    rows = qs.order_by('-created_at', '-id')[offset:offset + limit]
    return success_response(
        {'count': total, 'limit': limit, 'offset': offset,
         'results': [flag_dict(f) for f in rows]}, status.HTTP_200_OK)
