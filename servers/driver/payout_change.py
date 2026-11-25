"""25 Nov (owner): staff update a driver's payout details (a lost card, a closed account), with a
second person's approval.

The maker holds driver.write at the driver's place; the checker holds approval.check and
finance.payout there. The account number or UPI ID travels in the request ENCRYPTED (base.encrypted)
and is shown masked; nothing readable is stored on the approval row. On approval the driver's own
path runs (profile_views.set_bank / set_upi): the bank check with the provider, a 24-hour pause on
payouts, and a notice to the driver -- always, since the driver did not make this change.
India only: a US driver's payouts are set up with Stripe by the driver.
"""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.encrypted import decrypt, encrypt, mask
from base.utils import error_response, success_response
from servers.admin_access.bridge import ScopedPermission, granted
from servers.admin_access.scoping import scoped_or_none
from servers.approvals import registry
from servers.approvals import service as approvals
from servers.approvals.actions import DRIVER_PAYOUT_DETAILS

from .models import Driver


def _bad(code, message, field, http=status.HTTP_400_BAD_REQUEST):
    return error_response(code=code, message=message, field=field, issue=message, status=http)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('driver.write')])
def admin_payout_details(request, driver_id):
    """POST /driver/admin/<id>/payout-details/
    {method: upi, upi_id, reason} or {method: bank, holder_name, account_number, account_number_confirm, ifsc, reason}"""
    from .profile_views import UPI_RE, _country, clean_bank

    driver = scoped_or_none(request.user, Driver.objects.select_related('user_id', 'home_city'), id=driver_id)
    if driver is None:
        return _bad('NOT_FOUND', 'Driver not found.', 'driver_id', status.HTTP_404_NOT_FOUND)
    if not granted(request.user, 'driver.write', driver.home_city)[0]:
        return _bad('PERMISSION_DENIED', 'You do not hold driver.write for this driver.', 'driver_id',
                    status.HTTP_403_FORBIDDEN)
    country = _country(driver)
    if country != 'IN':
        return _bad('USE_STRIPE', 'In the USA the driver sets up payouts with Stripe in the app.', 'method')
    d = request.data or {}
    reason = ' '.join(str(d.get('reason') or '').split())
    if len(reason) < 10:
        return _bad('REASON_REQUIRED', 'Say why, in at least 10 characters (for example, how the driver asked).',
                    'reason')
    method = d.get('method')
    if method == 'upi':
        upi = str(d.get('upi_id') or '').strip()
        if not UPI_RE.match(upi):
            return _bad('UPI_INVALID', 'Enter a UPI ID like name@okaxis.', 'upi_id')
        payload = {'driver_id': driver.pk, 'method': 'upi', 'upi_masked': mask(upi, 5), 'upi_enc': encrypt(upi)}
        key = f'payout_details:{driver.pk}:upi:{mask(upi, 5)}'
    elif method == 'bank':
        try:
            holder, number, ifsc = clean_bank(d)
        except ValueError as exc:
            return _bad(*exc.args)
        payload = {'driver_id': driver.pk, 'method': 'bank', 'holder_name': holder, 'ifsc': ifsc,
                   'account_masked': mask(number), 'account_enc': encrypt(number)}
        key = f'payout_details:{driver.pk}:bank:{ifsc}:{mask(number)}'
    else:
        return _bad('METHOD_INVALID', 'Choose a bank account or UPI ID.', 'method')
    try:
        outcome = approvals.require_approval_or_execute(
            DRIVER_PAYOUT_DETAILS, actor=request.user, country=country, scope_node=driver.home_city,
            target_type='driver', target_id=driver.pk, payload=payload,
            idempotency_key=key[:200], reason=reason[:2000], http_request=request)
    except approvals.ApprovalError as exc:
        return exc.response()
    row = outcome.request
    return success_response({'approval_request': {'id': row.pk, 'ref': row.public_ref, 'status': row.status},
                             'duplicate': outcome.duplicate}, status.HTTP_202_ACCEPTED)


@registry.register(DRIVER_PAYOUT_DETAILS)
def execute_payout_details(request_row):
    from .profile_views import set_bank, set_upi

    p = request_row.payload or {}
    driver = Driver.objects.filter(pk=p.get('driver_id')).select_related('user_id').first()
    if driver is None:
        raise registry.ExecutionRefused('NO_DRIVER', 'The driver no longer exists.')
    if p.get('method') == 'upi':
        set_upi(driver, decrypt(p['upi_enc']), notify_always=True)
    elif p.get('method') == 'bank':
        set_bank(driver, p['holder_name'], decrypt(p['account_enc']), p['ifsc'], notify_always=True)
    else:
        raise registry.ExecutionRefused('BAD_REQUEST', 'The request is not valid.')
    return {'driver_id': driver.pk, 'method': p.get('method')}
