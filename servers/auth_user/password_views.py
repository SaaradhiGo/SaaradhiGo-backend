"""Password lifecycle and email ownership.

  POST /auth/password/change/          authenticated: old + new
  POST /auth/password/forgot/          public: send a reset link
  POST /auth/password/reset/           public: uid + token + new password
  POST /auth/email/verify/request/     authenticated: send a verify link
  POST /auth/email/verify/confirm/     public: uid + token

NO CRYPTOGRAPHY IS WRITTEN HERE
-------------------------------
Both token flows use `django.contrib.auth.tokens.PasswordResetTokenGenerator`.
It is HMAC over the user's primary key, a timestamp, the password hash and
`last_login`, keyed by `SECRET_KEY`. Three properties come free and are the
reason not to invent anything:

  * **single use** — the token covers the password hash, so using it changes
    the hash and invalidates the token (and every other outstanding one);
  * **expiry** — `PASSWORD_RESET_TIMEOUT`, set below to 2 hours;
  * **invalidated by a login** — it covers `last_login`.

For email verification the same generator is subclassed with a different
`key_salt`, so a reset token cannot be replayed as a verification token or
vice versa. That is the only "crypto" decision here and it is a one-line
salt change, not an algorithm.

WHY FORGOT-PASSWORD ALWAYS ANSWERS 200
--------------------------------------
A different answer for "no such address" turns the endpoint into an account
enumerator — and for a ride-hailing platform, confirming that a given email
belongs to a driver is itself a disclosure. So every call gets the same
message, and the four cases that do nothing are logged server-side:

  * no account with that address
  * the account's address is not verified
  * the account cannot use a password (OTP-only, never had one set) — it is
    still sent a *set-password* link, because that is a legitimate way for a
    rider to adopt email login
  * rate-limited

WHAT REQUIRES OWNER CONFIGURATION
---------------------------------
**Email delivery is not configured in this deployment.** There is no
`EMAIL_BACKEND`, `EMAIL_HOST` or `DEFAULT_FROM_EMAIL` anywhere in settings or
in the QA environment, so Django falls back to SMTP on localhost:25, which
does not exist on Railway. Receipt email has the same problem and fails
silently today.

Consequence: these endpoints are correct and tested, and **no reset email
will arrive until SMTP/SES credentials are supplied.** `base/auth_email.py`
refuses to pretend otherwise — it logs at ERROR and reports failure rather
than returning success for a message that went nowhere.
"""

import logging

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated

from base.auth_email import send_password_reset, send_email_verification
from base.throttles import PasswordResetThrottle
from base.utils import error_response, success_response
from servers.admin_audit.services import record_admin_action
from servers.admin_dashboard import login_guard

from .session_revocation import revoke_after_credential_change
from .identity import (
    InvalidEmail,
    assert_email_available,
    email_is_verified,
    normalize_email,
)

logger = logging.getLogger(__name__)

user_model = get_user_model()

#: The same answer for every forgot-password call. See the module docstring.
FORGOT_MESSAGE = (
    'If that address belongs to an account, we have sent a link to it. '
    'Check your inbox, including spam.'
)


class EmailVerificationTokenGenerator(PasswordResetTokenGenerator):
    """A reset token and a verification token must not be interchangeable.

    Same algorithm, different `key_salt`, so a token minted for one purpose
    does not validate for the other. Without this, a verification link —
    which is sent to an address that has NOT yet been proven — could be used
    to set a password.
    """

    key_salt = 'saaradhigo.auth_user.EmailVerificationTokenGenerator'


reset_tokens = PasswordResetTokenGenerator()
verify_tokens = EmailVerificationTokenGenerator()


def _uid(user):
    return urlsafe_base64_encode(force_bytes(user.pk))


def _user_from_uid(uidb64):
    """The account a token refers to, or None. Never raises."""
    try:
        pk = force_str(urlsafe_base64_decode(uidb64))
        return user_model.objects.filter(pk=pk).first()
    except Exception:  # noqa: BLE001
        return None


def _validate_new_password(password, user):
    """Django's validators, with the account for similarity checks.

    Returns an error response or None. The messages are the validators' own
    and are written for an end user ("This password is too short…").
    """
    if not password:
        return error_response(
            code='AUTH_PASSWORD_REQUIRED',
            message='A new password is required.',
            field='new_password',
            issue='Missing new_password',
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        validate_password(password, user=user)
    except ValidationError as exc:
        return error_response(
            code='AUTH_PASSWORD_WEAK',
            message=' '.join(exc.messages),
            field='new_password',
            issue='Password failed validation',
            status=status.HTTP_400_BAD_REQUEST,
        )
    return None


# ---------------------------------------------------------------------------
# Change, for a signed-in user
# ---------------------------------------------------------------------------

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def change_password(request):
    """
    POST /api/v1/auth/password/change/
        {"current_password": "...", "new_password": "..."}

    Requires the current password even though the caller holds a valid token:
    a stolen token should not be enough to lock the owner out of their own
    account.
    """
    user = request.user
    current = request.data.get('current_password')
    new_password = request.data.get('new_password')

    # An account that has never had a password (OTP-only) sets one through
    # the reset flow, which proves control of the email first.
    if user.has_usable_password():
        if not current or not user.check_password(current):
            login_guard.record_failure(request, f'pwchange:{user.pk}')
            logger.warning('password_change_failed reason=bad_current user_id=%s',
                           user.pk)
            return error_response(
                code='AUTH_INVALID_CREDENTIALS',
                message='Your current password is not correct.',
                field='current_password',
                issue='Current password rejected',
                status=status.HTTP_401_UNAUTHORIZED,
            )
    elif current:
        # They sent one anyway; harmless, but do not accept it as proof.
        logger.info('password_change_ignoring_current user_id=%s', user.pk)

    invalid = _validate_new_password(new_password, user)
    if invalid is not None:
        return invalid

    user.set_password(new_password)
    user.save(update_fields=['password'])

    # Outstanding RESET tokens are already invalid: the generator hashes the
    # password hash. JWTs are not -- a refresh token lives 14 days and keeps
    # rotating -- so changing a password used to leave every existing session
    # alive, including an attacker's. Revoke them here.
    revoked = revoke_after_credential_change(
        user, request, reason='Password changed by the account holder.')
    logger.info('password_changed user_id=%s sessions_revoked=%s',
                user.pk, revoked)
    return success_response(
        data={'message': 'Your password has been changed.'},
        status_code=status.HTTP_200_OK,
    )


# ---------------------------------------------------------------------------
# Forgot / reset
# ---------------------------------------------------------------------------

@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([PasswordResetThrottle])
def forgot_password(request):
    """
    POST /api/v1/auth/password/forgot/   {"email": "..."}

    Always 200 with the same message. See the module docstring.
    """
    raw_email = request.data.get('email')

    def _answer():
        return success_response(
            data={'message': FORGOT_MESSAGE},
            status_code=status.HTTP_200_OK,
        )

    try:
        email = normalize_email(raw_email)
    except InvalidEmail:
        logger.info('password_forgot_ignored reason=invalid_address')
        return _answer()
    if not email:
        return _answer()

    user = user_model.objects.filter(email__iexact=email).first()
    if user is None:
        logger.info('password_forgot_ignored reason=no_account')
        return _answer()
    if not user.is_active:
        logger.warning('password_forgot_ignored reason=inactive user_id=%s',
                       user.pk)
        return _answer()
    if not email_is_verified(user):
        # The address exists on the account but was never proven. Sending a
        # reset here would let whoever set it take the account over.
        logger.warning(
            'password_forgot_ignored reason=email_unverified user_id=%s',
            user.pk,
        )
        return _answer()

    token = reset_tokens.make_token(user)
    sent = send_password_reset(user=user, uid=_uid(user), token=token)
    logger.info('password_forgot_sent user_id=%s delivered=%s', user.pk, sent)
    return _answer()


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([PasswordResetThrottle])
def reset_password(request):
    """
    POST /api/v1/auth/password/reset/
        {"uid": "...", "token": "...", "new_password": "..."}

    Single-use and time-limited by construction — see the module docstring.
    """
    uidb64 = request.data.get('uid')
    token = request.data.get('token')
    new_password = request.data.get('new_password')

    user = _user_from_uid(uidb64)

    # One refusal for a bad uid, a bad token and an expired token alike: the
    # three are indistinguishable to someone who should not have a valid one.
    if user is None or not token or not reset_tokens.check_token(user, token):
        logger.warning('password_reset_failed reason=bad_token')
        return error_response(
            code='AUTH_RESET_TOKEN_INVALID',
            message='That link is invalid or has expired. Request a new one.',
            field='token',
            issue='Reset token rejected',
            status=status.HTTP_400_BAD_REQUEST,
        )

    invalid = _validate_new_password(new_password, user)
    if invalid is not None:
        return invalid

    user.set_password(new_password)

    # Using a reset link delivered to the address proves control of it, so an
    # account that reached here through a verified address stays verified, and
    # an invited account becomes verified now.
    fields = ['password']
    if not user.email_verified_at and user.email:
        user.email_verified_at = timezone.now()
        fields.append('email_verified_at')
    user.save(update_fields=fields)

    login_guard.clear(request, (user.email or '').lower())

    # A reset is the flow someone uses when they have LOST control of the
    # account, so ending every existing session matters more here than in a
    # routine change.
    revoked = revoke_after_credential_change(
        user, request, reason='Password reset through a verified address.')
    logger.info('password_reset_ok user_id=%s sessions_revoked=%s',
                user.pk, revoked)
    return success_response(
        data={'message': 'Your password has been set. You can sign in now.'},
        status_code=status.HTTP_200_OK,
    )


# ---------------------------------------------------------------------------
# Email ownership
# ---------------------------------------------------------------------------

@api_view(['POST'])
@permission_classes([IsAuthenticated])
@throttle_classes([PasswordResetThrottle])
def request_email_verification(request):
    """
    POST /api/v1/auth/email/verify/request/   {"email": "..."}  (optional)

    With an address, links it to the account first (refusing one that belongs
    to someone else) and then sends a verification link. Without one, re-sends
    for the address already on the account.
    """
    user = request.user
    raw_email = request.data.get('email')

    if raw_email:
        try:
            email = normalize_email(raw_email)
            assert_email_available(email, for_user=user)
        except InvalidEmail:
            return error_response(
                code='AUTH_EMAIL_INVALID',
                message='That does not look like an email address.',
                field='email',
                issue='Invalid address',
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as exc:  # EmailAlreadyLinked
            return error_response(
                code='AUTH_EMAIL_IN_USE',
                message=str(exc),
                field='email',
                issue='Address belongs to another account',
                status=status.HTTP_409_CONFLICT,
            )

        if email != (user.email or ''):
            # Changing the address drops verification: the new one is
            # unproven, and leaving the old flag set would let someone swap
            # in an address they do not control and keep reset access.
            user.email = email
            user.email_verified_at = None
            user.save(update_fields=['email', 'email_verified_at'])

    if not user.email:
        return error_response(
            code='AUTH_EMAIL_REQUIRED',
            message='Add an email address first.',
            field='email',
            issue='No address on the account',
            status=status.HTTP_400_BAD_REQUEST,
        )

    token = verify_tokens.make_token(user)
    sent = send_email_verification(user=user, uid=_uid(user), token=token)
    logger.info('email_verify_sent user_id=%s delivered=%s', user.pk, sent)

    return success_response(
        data={'message': 'We have sent a verification link to your email.'},
        status_code=status.HTTP_200_OK,
    )


@api_view(['POST'])
@permission_classes([AllowAny])
def confirm_email_verification(request):
    """
    POST /api/v1/auth/email/verify/confirm/   {"uid": "...", "token": "..."}
    """
    user = _user_from_uid(request.data.get('uid'))
    token = request.data.get('token')

    if user is None or not token or not verify_tokens.check_token(user, token):
        logger.warning('email_verify_failed reason=bad_token')
        return error_response(
            code='AUTH_VERIFY_TOKEN_INVALID',
            message='That link is invalid or has expired. Request a new one.',
            field='token',
            issue='Verification token rejected',
            status=status.HTTP_400_BAD_REQUEST,
        )

    if not user.email_verified_at:
        user.email_verified_at = timezone.now()
        user.save(update_fields=['email_verified_at'])

        # Verifying an operator's address changes who can reset a privileged
        # account, so it belongs in the audit trail. A rider's does not.
        from base.permissions import is_operator
        if is_operator(user):
            record_admin_action(
                request,
                action='operator_email_verified',
                target_type='user',
                target_id=user.pk,
                after={'email_verified': True},
                reason='Operator confirmed email ownership',
            )

    logger.info('email_verified user_id=%s', user.pk)
    return success_response(
        data={'message': 'Your email address is verified.'},
        status_code=status.HTTP_200_OK,
    )
