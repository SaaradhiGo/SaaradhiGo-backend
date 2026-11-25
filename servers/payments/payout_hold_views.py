"""POST /payments/admin/drivers/<id>/payout-hold/ (owner ruling 25 Nov).

{reason_kind: suspected_fraud | dispute | compliance_check | payout_details_verification, note, days?,
 idempotency_key}. A second person approves (finance.payout_hold, always a checker); at most 14 days;
the driver is told. Releasing early is the existing release route (no checker: it only pays out).
"""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.utils import error_response, success_response
from servers.admin_access.bridge import ScopedPermission
from servers.approvals.service import ApprovalError
from servers.driver.models import Driver

from .scope import fetch_one


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('finance.payout')])
def driver_payout_hold(request, driver_id):
    from servers.fraud import money

    driver, refusal = fetch_one(request.user, Driver, 'finance.payout', pk=driver_id,
                                queryset=Driver.objects.select_related('user_id', 'home_city'))
    if refusal == 'not_found':
        return error_response(code='NOT_FOUND', message='Driver not found', field='driver_id',
                              issue='not found', status=status.HTTP_404_NOT_FOUND)
    if refusal:
        return error_response(code='PERMISSION_DENIED', message='You do not have permission to do that here.',
                              field='permission', issue='finance.payout', status=status.HTTP_403_FORBIDDEN)
    data = request.data or {}
    key = str(data.get('idempotency_key') or '').strip()
    if not key:
        return error_response(code='IDEMPOTENCY_KEY_REQUIRED', message='idempotency_key is required.',
                              field='idempotency_key', issue='missing', status=status.HTTP_400_BAD_REQUEST)
    days = data.get('days')
    try:
        days = int(days) if days not in (None, '') and not isinstance(days, bool) else days
    except (TypeError, ValueError):
        days = -1
    try:
        outcome = money.request_driver_payout_hold(
            request.user, driver, str(data.get('reason_kind') or ''), data.get('note'), days, key,
            http_request=request)
    except money.MoneyError as exc:
        return error_response(code=exc.code, message=exc.message, field=exc.field, issue=exc.code,
                              status=exc.status)
    except ApprovalError as exc:
        return exc.response()
    row = outcome.request
    return success_response({'approval_request': row.as_dict(), 'duplicate': outcome.duplicate,
                             'state': 'pending_approval' if row.status == 'requested' else row.status},
                            status.HTTP_202_ACCEPTED)
