"""DPDP Act 2023 data-subject endpoints.

Phase-0 baseline:
  GET  /api/v1/me/export/  — returns the caller's full data as JSON
  POST /api/v1/me/delete/  — soft-deletes the account; anonymises PII

We anonymise rather than hard-delete because:
  - Financial records (Payment, TransactionHistory, WithdrawalRequest)
    have legal retention requirements (7 years under Income Tax + GST).
  - Trip history is needed for safety / dispute resolution audits.
  - The user's right to erasure under DPDP is satisfied by making the
    PII unreadable from then on; aggregate / pseudonymised data may
    legitimately be retained.

The anonymisation itself is `servers.auth_user.erasure.anonymise_user`, the
same service the `anonymise_user` management command runs for a request
that arrives by email or ticket. There is ONE definition of what erasure
wipes and what it keeps, and it lives there; this view only decides that a
signed-in rider may ask for it about themselves. The event is recorded in
AdminAuditLog as `account_anonymised`, with the person as the actor, their
identifiers snapshotted as they were before the wipe.
"""

import logging

from django.db import transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.utils import success_response, error_response

from .erasure import AnonymisationRefused, anonymise_user

logger = logging.getLogger(__name__)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def me_export(request):
    """Return everything we know about the caller. Used to satisfy the
    DPDP "right of access". Format: JSON with one key per data domain.

    Heavy queries are intentionally bounded — pagination should be
    handled by a follow-up job for users with deep history. For Phase-0
    riders this is fine; trip volume per rider is small.
    """
    user = request.user

    payload = {
        'user': _serialize_user(user),
        'rider': _serialize_rider(user),
        'driver': _serialize_driver(user),
        'wallet': _serialize_wallet(user),
        'trips': _serialize_trips(user),
        'payments': _serialize_payments(user),
        'wallet_transactions': _serialize_wallet_transactions(user),
        'notifications': _serialize_notifications(user),
    }
    return success_response(payload, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def me_delete(request):
    """Soft-delete the caller's account. Body:
        {"confirm": "DELETE", "reason": "<optional free text>"}

    The literal string "DELETE" in the confirm field is required to
    reduce accidental deletes via misclicks or stale tokens. Once the
    request succeeds, all subsequent requests authenticated with the
    pre-deletion JWT will start to fail as soon as the access token
    expires (15 min); we also blacklist every active refresh token
    immediately so refresh paths die.
    """
    if request.data.get('confirm') != 'DELETE':
        return error_response(
            code='CONFIRM_REQUIRED',
            message='Set "confirm": "DELETE" in the request body to proceed.',
            field='confirm',
            issue='Account deletion requires explicit confirmation.',
            status=status.HTTP_400_BAD_REQUEST,
        )

    user = request.user
    reason = (request.data.get('reason') or '')[:1000]

    try:
        with transaction.atomic():
            anonymise_user(
                user,
                reason=reason or 'self-service DPDP delete',
                # The request IS the reference for a self-service erasure:
                # there is no ticket, and the row's target and timestamp
                # identify it.
                reference=f'self-service user {user.pk}',
                actor=user,
                request=request,
            )
    except AnonymisationRefused as e:
        # An operator signed in to the rider app and asked to be erased.
        # Staff are deactivated through the access tooling, never wiped.
        return error_response(
            code='NOT_ALLOWED',
            message='This account cannot be deleted through self-service. Contact operations.',
            field='general',
            issue=str(e),
            status=status.HTTP_403_FORBIDDEN,
        )
    except Exception as e:
        logger.exception(f"me_delete failed for user {user.id}: {e}")
        return error_response(
            code='INTERNAL_ERROR',
            message='Could not delete account. Please try again or contact support.',
            field='general',
            issue=str(e),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    logger.warning(f"DPDP me_delete: user {user.id} anonymised")
    return success_response(
        {
            'message': 'Account anonymised. Financial records retained per Indian retention rules. '
                       'Any existing app sessions will sign out within 15 minutes.',
        },
        status.HTTP_200_OK,
    )


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def _serialize_user(user):
    return {
        'id': user.id,
        'phone_number': getattr(user, 'phone_number', None),
        'email': getattr(user, 'email', None),
        'full_name': getattr(user, 'full_name', None),
        'gender': getattr(user, 'gender', None),
        'dob': str(getattr(user, 'dob', None) or ''),
        'house_no': getattr(user, 'house_no', None),
        'street': getattr(user, 'street', None),
        'city': getattr(user, 'city', None),
        'zip_code': getattr(user, 'zip_code', None),
        'emergency_contact': getattr(user, 'emergency_contact', None),
        'role': getattr(user, 'role', None),
        'created_at': str(getattr(user, 'created_at', '') or ''),
        'is_active': bool(getattr(user, 'is_active', False)),
    }


def _serialize_rider(user):
    rider = getattr(user, 'rider', None)
    if not rider:
        return None
    return {'id': rider.id, 'rating': str(getattr(rider, 'rating', '0'))}


def _serialize_driver(user):
    driver = getattr(user, 'driver', None)
    if not driver:
        return None
    return {
        'id': driver.id,
        'approved': bool(driver.approved),
        'status': driver.status,
        'total_trips': driver.total_trips,
        'ratings': str(driver.ratings),
        'license_expiry': str(driver.license_expiry or ''),
    }


def _serialize_wallet(user):
    try:
        from servers.rider.models import Wallet
        w = Wallet.objects.filter(user_id=user, scope=Wallet.SCOPE_RIDER).first()
        if not w:
            return None
        return {'balance': str(w.balance)}
    except Exception:
        return None


def _export_rows(qs, fields, money=()):
    """`fields` of each row as strings, with each `money` field read from its
    minor-unit column and written in major units -- '135.68', the shape this
    export has always had."""
    from base.money import from_minor

    columns = [f'{f}_minor' if f in money else f for f in fields]
    if money:
        columns.append('currency')
    out = []
    for row in qs.values(*columns)[:500]:
        record = {}
        for f in fields:
            v = from_minor(row[f'{f}_minor'], row['currency']) if f in money else row[f]
            record[f] = str(v) if v is not None else None
        out.append(record)
    return out


def _serialize_trips(user):
    from servers.ride.models import Trip
    return _export_rows(
        Trip.objects.filter(user_id=user).order_by('-requested_at'),
        ['id', 'pickup_address', 'destination_address',
         'estimated_fare', 'final_fare', 'payment_method',
         'payment_status', 'requested_at', 'completed_at',
         'cancelled_at'],
        money=('estimated_fare', 'final_fare'),
    )


def _serialize_payments(user):
    from servers.payments.models import Payment
    return _export_rows(
        Payment.objects.filter(user_id=user).order_by('-created_at'),
        ['id', 'trip_id', 'amount', 'method', 'status', 'created_at'],
        money=('amount',),
    )


def _serialize_wallet_transactions(user):
    try:
        from servers.rider.models import WalletTransaction
        return _export_rows(
            WalletTransaction.objects.filter(user_id=user).order_by('-created_at'),
            ['id', 'amount', 'txn_type', 'status', 'created_at'],
            money=('amount',),
        )
    except Exception:
        return []


def _serialize_notifications(user):
    try:
        from servers.rider.models import Notification
        qs = (
            Notification.objects.filter(user_id=user)
            .order_by('-created_at')
            .values('id', 'title', 'message', 'created_at')[:500]
        )
        return [{k: str(v) if v is not None else None for k, v in row.items()} for row in qs]
    except Exception:
        return []
