"""Changing the account's mobile number (owner, 10 Nov).

The number is the sign-in identity, so `/auth/update/` keeps it read-only. It changes only
here, with a `phone_proof` for the NEW number from the existing `/auth/otp/` +
`/auth/otp/verify/` flow; the proof is spent whatever happens next.
"""
import logging

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.countries import normalize_phone
from base.utils import error_response, success_response

from . import phone_proof
from .registration_views import _PROOF_MESSAGES

logger = logging.getLogger(__name__)
User = get_user_model()


def _refuse(code, message, issue):
    return error_response(code=code, message=message, field='phone_number', issue=issue,
                          status=status.HTTP_400_BAD_REQUEST)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def change_phone(request):
    """{"phone_number": the new number, "phone_proof": from /auth/otp/verify/} -> the profile."""
    user = request.user
    raw = request.data.get('phone_number')
    phone = normalize_phone(raw) if isinstance(raw, str) else None
    if not phone:
        return _refuse('AUTH_PHONE_INVALID', 'Enter a valid mobile number.', 'Not a phone number')
    proof_error = phone_proof.redeem(
        request.data.get('phone_proof'), phone, user.role,
        same_phone=lambda proved, submitted: normalize_phone(proved) == submitted)
    if proof_error is not None:
        return _refuse(proof_error, _PROOF_MESSAGES.get(proof_error, _PROOF_MESSAGES[phone_proof.INVALID]),
                       'Verify the new number with the SMS code first')
    if phone == user.phone_number:
        return _refuse('AUTH_PHONE_UNCHANGED', 'That is already your mobile number.', 'Same number')
    if User.objects.filter(phone_number=phone).exclude(pk=user.pk).exists():
        return _refuse('AUTH_PHONE_TAKEN', 'That mobile number belongs to another account.', 'Taken')
    old = user.phone_number
    fields = ['phone_number']
    user.phone_number = phone
    if user.username == old:
        user.username = phone
        fields.append('username')
    try:
        with transaction.atomic():
            user.save(update_fields=fields)
    except IntegrityError:
        return _refuse('AUTH_PHONE_TAKEN', 'That mobile number belongs to another account.', 'Taken')
    logger.info('phone_changed user_id=%s', user.pk)
    from .serializers import UserModelSerializer
    return success_response(UserModelSerializer(user).data, status.HTTP_200_OK)
