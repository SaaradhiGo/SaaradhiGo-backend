"""Email + password sign-in, for every role.

WHAT THIS REUSES RATHER THAN REINVENTS
--------------------------------------
Nothing here is new security machinery. All of it already existed and was
reachable only from the operator console:

  * `django.contrib.auth.hashers` via `check_password` — Django's PBKDF2.
    No hashing is implemented here.
  * `servers.admin_dashboard.login_guard` — per-identity and per-IP failure
    counting with a 15-minute lockout, and a deliberate fail-open on a cache
    outage (documented in that module).
  * `base.ops_mfa` — django-otp TOTP plus static recovery codes, already
    enforced for operators on the console.
  * `rest_framework_simplejwt` — the same `AccessToken` / `RefreshToken` pair
    and the same response envelope as `/auth/login/`, so a client can swap
    one login for the other without touching the rest of its code.

WHAT IS GENUINELY NEW
---------------------
Only that a rider, a driver or an operator may present an email and a
password instead of a phone and an OTP, and that an operator doing so must
also present a second factor.

THE REFUSALS ARE ALL THE SAME STRING
------------------------------------
Unknown address, wrong password, no password ever set, locked out, inactive
account: one message, one code, one status. `/auth/admin/login/` already
learned this the hard way — it used to answer 'Incorrect password' for a real
account and something else for an unknown one, which is a free account
enumerator. The only refusal that differs is the MFA one, and that is only
reachable AFTER the password has already been accepted, so it reveals nothing
to someone who does not already hold the password.

WHAT THIS DOES NOT DO
---------------------
It does not create accounts. `/auth/otp/` creates a rider or a driver on
first login because a phone number proves possession of that phone; an email
address proves nothing until it is verified, so an email login against an
unknown address is a refusal, not a signup. Account creation by email is an
operator action — see `invite_views.py`.
"""

import logging

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from base import ops_mfa
from base.permissions import is_operator
from base.utils import error_response, success_response
from servers.admin_dashboard import login_guard

from .identity import (
    InvalidEmail,
    can_use_password_login,
    email_is_verified,
    normalize_email,
)
from .serializers import UserModelSerializer

logger = logging.getLogger(__name__)

user_model = get_user_model()

#: One refusal for every credential failure. See the module docstring.
GENERIC_REFUSAL = 'Invalid email or password.'
GENERIC_CODE = 'AUTH_INVALID_CREDENTIALS'


def _refuse():
    return error_response(
        code=GENERIC_CODE,
        message=GENERIC_REFUSAL,
        field='email',
        # The `issue` field is returned to the client, so it must not say
        # which of the possible causes applied.
        issue='Credentials were not accepted',
        status=status.HTTP_401_UNAUTHORIZED,
    )


def _issue_tokens(user):
    """The same envelope `/auth/login/` returns, deliberately."""
    return {
        'token': str(AccessToken.for_user(user)),
        'refresh_token': str(RefreshToken.for_user(user)),
        'user': UserModelSerializer(user).data,
    }


@api_view(['POST'])
@permission_classes([AllowAny])
def email_login(request):
    """
    POST /api/v1/auth/email/login/

    Request:
        {
          "email": "someone@example.com",
          "password": "...",
          "mfa_code": "123456"      # operators only
        }

    Responses:
        200  {token, refresh_token, user}
        401  AUTH_INVALID_CREDENTIALS      -- every credential failure
        401  AUTH_MFA_REQUIRED             -- password OK, second factor needed
        401  AUTH_MFA_INVALID              -- password OK, wrong second factor
    """
    raw_email = request.data.get('email')
    password = request.data.get('password')

    if not raw_email or not password:
        # Shaped like the generic refusal rather than a field-validation
        # error, so a probe cannot distinguish "no such field" from "no such
        # account" by the response shape.
        return _refuse()

    try:
        email = normalize_email(raw_email)
    except InvalidEmail:
        return _refuse()
    if not email:
        return _refuse()

    # Lockout is checked BEFORE any password work, so a locked identity costs
    # no hashing time. The guard's key is an opaque string; passing an email
    # where it says "phone" is intentional -- one namespace, and a phone and
    # an email can never collide as strings.
    if login_guard.is_locked(request, email):
        logger.warning('email_login_locked')
        return _refuse()

    user = user_model.objects.filter(email__iexact=email).first()

    # A deliberate, constant-ish amount of work regardless of whether the
    # account exists: Django's `check_password` on a dummy hash. Without it,
    # an unknown address returns measurably faster than a wrong password and
    # the endpoint becomes a timing-based enumerator.
    if user is None or not can_use_password_login(user):
        user_model().set_password(password)  # hash and discard
        login_guard.record_failure(request, email)
        logger.info('email_login_failed reason=no_account_or_no_password')
        return _refuse()

    if not user.check_password(password):
        login_guard.record_failure(request, email)
        logger.info('email_login_failed reason=bad_password user_id=%s', user.pk)
        return _refuse()

    if not user.is_active:
        login_guard.record_failure(request, email)
        logger.warning('email_login_failed reason=inactive user_id=%s', user.pk)
        return _refuse()

    # The address has to be PROVEN, not merely typed.
    #
    # Self-registration lets anyone create an account against any address.
    # Without this check, verification would be decorative: the password set
    # at registration would work immediately, and someone could register with
    # a stranger's address and hold a working account on it.
    #
    # Reached only with a correct password, so naming the reason tells an
    # attacker nothing they did not already know, and telling the real owner
    # 'verify your email' instead of 'invalid credentials' is the difference
    # between a solvable problem and a support ticket.
    #
    # Accounts created by `bootstrap_qa_accounts` or by an operator invite
    # are verified at the point their password is set, so this does not lock
    # out the operator console.
    if not email_is_verified(user):
        logger.info('email_login_unverified user_id=%s', user.pk)
        return error_response(
            code='AUTH_EMAIL_UNVERIFIED',
            message='Confirm your email address before signing in. We sent you a link when you registered.',
            field='email',
            issue='Email address not yet verified',
            status=status.HTTP_403_FORBIDDEN,
        )

    # --- second factor, for operators only --------------------------------
    #
    # Reached only with a correct password, so the distinct responses below
    # tell an attacker nothing they did not already have.
    if ops_mfa.mfa_required_for(user):
        code = request.data.get('mfa_code')
        if not code:
            logger.info('email_login_mfa_required user_id=%s', user.pk)
            return error_response(
                code='AUTH_MFA_REQUIRED',
                message='Enter the 6-digit code from your authenticator app.',
                field='mfa_code',
                issue='Second factor required for this account',
                status=status.HTTP_401_UNAUTHORIZED,
            )
        if not ops_mfa.verify_code(user, code):
            # Counted against the lockout: an MFA code is brute-forceable in a
            # way a password is not (six digits), so it needs the same cap.
            login_guard.record_failure(request, email)
            logger.warning('email_login_mfa_invalid user_id=%s', user.pk)
            return error_response(
                code='AUTH_MFA_INVALID',
                message='That code was not accepted.',
                field='mfa_code',
                issue='Second factor rejected',
                status=status.HTTP_401_UNAUTHORIZED,
            )

    login_guard.clear(request, email)

    user.last_login = timezone.now()
    user.save(update_fields=['last_login'])

    logger.info(
        'email_login_ok user_id=%s role=%s operator=%s',
        user.pk, user.role, is_operator(user),
    )
    return success_response(data=_issue_tokens(user),
                           status_code=status.HTTP_200_OK)
