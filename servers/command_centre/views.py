"""The Command Centre's operator API.

  GET  /api/v1/command-centre/admin/alerts/                   report.read
  POST /api/v1/command-centre/admin/alerts/<id>/acknowledge/  safety.respond (+ the kind's own permission)

Both under `admin/` so the structural guard (`tests/test_admin_namespace_is_gated`)
finds them by path. Not scoped: see `models` -- an alert exists because a row
could not be placed. Acknowledging is audited (`operator_alert_acknowledged`)
because "who took the unroutable SOS" is the question an incident review asks.
"""

import logging

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.utils import error_response, success_response
from servers.admin_access.bridge import ScopedPermission, granted
from servers.admin_audit.services import record_admin_action

from .models import ACK_PERMISSION, OperatorAlert
from .services import acknowledge

logger = logging.getLogger(__name__)

STATUSES = ('open', 'acknowledged', 'all')


def _serialize(alert):
    return {
        'id': alert.id,
        'kind': alert.kind,
        'severity': alert.severity,
        'title': alert.title,
        'payload': alert.payload,
        'source_type': alert.source_type,
        'source_id': alert.source_id,
        'created_at': alert.created_at.isoformat(),
        'acknowledged_at': alert.acknowledged_at.isoformat() if alert.acknowledged_at else None,
        'acknowledged_by': alert.acknowledged_by_id,
        'acknowledged_note': alert.acknowledged_note,
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('report.read')])
def admin_list_alerts(request):
    """Query params: status=open|acknowledged|all (default open), limit, offset."""
    wanted = (request.query_params.get('status') or 'open').strip()
    if wanted not in STATUSES:
        return error_response(
            code='INVALID_STATUS', message=f'status must be one of: {", ".join(STATUSES)}',
            field='status', issue=f'Got {wanted!r}', status=status.HTTP_400_BAD_REQUEST,
        )
    qs = OperatorAlert.objects.all()
    if wanted == 'open':
        qs = qs.filter(acknowledged_at__isnull=True)
    elif wanted == 'acknowledged':
        qs = qs.filter(acknowledged_at__isnull=False)

    try:
        limit = min(int(request.query_params.get('limit', 50)), 200)
        offset = max(int(request.query_params.get('offset', 0)), 0)
    except (TypeError, ValueError):
        limit, offset = 50, 0

    return success_response(
        {
            'count': qs.count(),
            'open_count': OperatorAlert.objects.filter(acknowledged_at__isnull=True).count(),
            'limit': limit,
            'offset': offset,
            'results': [_serialize(a) for a in qs[offset:offset + limit]],
        },
        status.HTTP_200_OK,
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('safety.respond')])
def acknowledge_alert(request, alert_id):
    """Body: {"note": "..."} optional. Idempotent: a second ack is a 200 no-op.

    Taking an alert is for the people who can act on what it points at.
    Every kind today is a safety alert, so the route is gated on
    `safety.respond`; the kind's own permission (`ACK_PERMISSION`) is asked
    again inside, so a future kind owned by another team is refused here
    until it gets a route gated on its own capability. An operator who may
    only LIST (`report.read`) cannot dismiss an unroutable SOS.
    """
    alert = OperatorAlert.objects.filter(id=alert_id).first()
    if alert is None:
        return error_response(
            code='NOT_FOUND', message='Alert not found', field='alert_id',
            issue=f'No alert {alert_id}', status=status.HTTP_404_NOT_FOUND,
        )

    needed = ACK_PERMISSION.get(alert.kind)
    allowed = needed is not None and granted(request.user, needed)[0]
    if not allowed:
        return error_response(
            code='FORBIDDEN',
            message='You do not hold the capability that owns this alert.',
            field='alert_id',
            issue=f'{alert.kind} is acknowledged by holders of {needed or "(no capability: not acknowledgeable)"}',
            status=status.HTTP_403_FORBIDDEN,
        )

    note = (request.data.get('note') or '') if hasattr(request, 'data') else ''
    changed = acknowledge(alert, by=request.user, note=note)
    if changed:
        record_admin_action(
            request, action='operator_alert_acknowledged',
            target_type='operator_alert', target_id=alert.id,
            before={'acknowledged': False},
            after={'acknowledged': True, 'kind': alert.kind,
                   'source_type': alert.source_type, 'source_id': alert.source_id},
            reason=note,
        )
    return success_response(_serialize(alert), status.HTTP_200_OK)
