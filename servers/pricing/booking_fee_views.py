"""Pricing > Booking fees (owner ruling, 26 Nov).

    GET   /pricing/admin/booking-fees/                     zone.read: places, fee per class, waiting changes
    POST  /pricing/admin/booking-fees/preview/             zone.read: {country, scope_code, amounts, effective_from}
    POST  /pricing/admin/booking-fees/edit/                pricing.write -> 202: the change, for a second person
"""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.utils import success_response
from servers.approvals.service import ApprovalError

from . import booking_fees
from .fee_plan_views import READ, WRITE, _approval_body


def _places():
    from .booking_fee_models import RiderBookingFee
    from .fee_plan_models import ACTIVE, DriverFeePlan

    seen = set()
    for c, s in DriverFeePlan.objects.filter(status=ACTIVE).values_list('country', 'scope_code'):
        seen.add((c, s))
    for c, s in RiderBookingFee.objects.values_list('country', 'scope_code'):
        seen.add((c, s))
    return sorted(seen)


def _waiting(country, scope_code):
    from servers.approvals.actions import PRICING_CHANGE
    from servers.approvals.models import ApprovalRequest

    rows = (ApprovalRequest.objects.filter(action_key=PRICING_CHANGE, target_type=booking_fees.KIND,
                                           target_id=f'{country}:{scope_code}')
            .order_by('-requested_at')[:10])
    return [{'id': r.id, 'ref': r.public_ref, 'status': r.status, 'maker': r.maker_label,
             'maker_id': r.maker_id, 'reason': r.request_reason, 'decision_reason': r.decision_reason,
             'checker': r.checker_label, 'requested_at': r.requested_at.isoformat(),
             'amounts': (r.payload or {}).get('amounts'), 'requested_from': (r.payload or {}).get('requested_from')}
            for r in rows]


@api_view(['GET'])
@permission_classes([IsAuthenticated, READ])
def booking_fees_list(request):
    out = []
    for country, scope_code in _places():
        row = booking_fees.place(country, scope_code)
        row['history'] = booking_fees.history(country, scope_code)
        row['requests'] = _waiting(country, scope_code)
        out.append(row)
    return success_response({'results': out}, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, READ])
def booking_fees_preview(request):
    d = request.data or {}
    try:
        body = booking_fees.preview(str(d.get('country') or ''), str(d.get('scope_code') or ''), d)
    except ApprovalError as exc:
        return exc.response()
    return success_response(body, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, WRITE])
def booking_fees_edit(request):
    d = request.data or {}
    try:
        outcome = booking_fees.propose(request.user, str(d.get('country') or ''), str(d.get('scope_code') or ''),
                                       d, http_request=request)
    except ApprovalError as exc:
        return exc.response()
    return success_response(_approval_body(outcome), status.HTTP_202_ACCEPTED)
