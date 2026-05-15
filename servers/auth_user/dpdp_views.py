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

What we anonymise on /me/delete/:
  - phone_number → "+91-deleted-{user_id}"
  - email → ""
  - full_name → "Deleted user"
  - dob, address fields, emergency_contact, avatar, fcm_token → cleared
  - is_active=False so the user can no longer log in
  - all refresh tokens blacklisted so existing sessions die

What we keep:
  - User row with the placeholder phone (FKs from Trip/Payment continue
    to work)
  - Trip rows
  - Payment / TransactionHistory rows
  - Rating rows

The deletion event is recorded in AdminAuditLog (actor=self).
"""

import logging

from django.db import transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.utils import success_response, error_response

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
            _anonymise_user(user)
            _blacklist_user_refresh_tokens(user)
            _record_audit(user, reason, request)
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
        w = Wallet.objects.filter(user_id=user).first()
        if not w:
            return None
        return {'balance': str(w.balance)}
    except Exception:
        return None


def _serialize_trips(user):
    from servers.ride.models import Trip
    qs = (
        Trip.objects.filter(user_id=user)
        .order_by('-requested_at')
        .values(
            'id', 'pickup_address', 'destination_address',
            'estimated_fare', 'final_fare', 'payment_method',
            'payment_status', 'requested_at', 'completed_at',
            'cancelled_at',
        )[:500]
    )
    return [{k: str(v) if v is not None else None for k, v in row.items()} for row in qs]


def _serialize_payments(user):
    from servers.payments.models import Payment
    qs = (
        Payment.objects.filter(user_id=user)
        .order_by('-created_at')
        .values('id', 'trip_id', 'amount', 'method', 'status', 'created_at')[:500]
    )
    return [{k: str(v) if v is not None else None for k, v in row.items()} for row in qs]


def _serialize_wallet_transactions(user):
    try:
        from servers.rider.models import WalletTransaction
        qs = (
            WalletTransaction.objects.filter(user_id=user)
            .order_by('-created_at')
            .values('id', 'amount', 'txn_type', 'status', 'created_at')[:500]
        )
        return [{k: str(v) if v is not None else None for k, v in row.items()} for row in qs]
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


def _anonymise_user(user):
    """Replace PII fields with placeholders. Keep the User row so FKs
    from Trip / Payment / TransactionHistory keep working."""
    placeholder_phone = f"+91-deleted-{user.id}"
    fields_cleared = []
    user.phone_number = placeholder_phone
    fields_cleared.append('phone_number')
    for name in (
        'email', 'full_name', 'gender', 'dob', 'house_no', 'street',
        'city', 'zip_code', 'emergency_contact', 'avatar', 'fcm_token',
    ):
        if hasattr(user, name):
            try:
                setattr(user, name, None if name in ('dob', 'avatar') else '')
                fields_cleared.append(name)
            except Exception:
                pass
    user.is_active = False
    user.save()
    logger.info(f"DPDP anonymise: user {user.id} fields cleared: {fields_cleared}")


def _blacklist_user_refresh_tokens(user):
    """Best-effort revocation of every active refresh token for this
    user. Requires rest_framework_simplejwt.token_blacklist (already
    installed in this project)."""
    try:
        from rest_framework_simplejwt.token_blacklist.models import OutstandingToken
        for ot in OutstandingToken.objects.filter(user=user):
            try:
                from rest_framework_simplejwt.tokens import RefreshToken
                RefreshToken(ot.token).blacklist()
            except Exception:
                # ignore tokens already blacklisted or malformed
                continue
    except Exception as e:
        logger.warning(f"refresh-token blacklist on delete failed: {e}")


def _record_audit(user, reason, request):
    try:
        from servers.admin_audit.services import record_admin_action
        record_admin_action(
            request,
            action='dpdp_delete',
            target_type='user',
            target_id=user.id,
            before={'is_active': True},
            after={'is_active': False, 'anonymised': True},
            reason=reason or 'self-service DPDP delete',
        )
    except Exception:
        # Auditing is best-effort; user has already been anonymised by
        # the time we reach this. Don't fail their delete.
        pass
