"""Self-service MFA for the signed-in operator: status, and new recovery codes.

    GET  /api/v1/auth/admin/mfa/status/
    POST /api/v1/auth/admin/mfa/recovery-codes/regenerate/   {"mfa_code": "123456"}

WHY THESE ARE NOT GATED ON A CAPABILITY
---------------------------------------
`ScopedPermission` answers "may this operator act on that resource, there".
Neither endpoint has a target: each reads or rewrites the CALLER's own
second factor, identified from the bearer token, and returns nothing about
anybody else. An operator with no role assignment yet -- the console shows
them "no access yet" -- still needs to see whether their authenticator is
enrolled and to replace a lost set of recovery codes, so no capability can
gate it. The gate is the one `admin-access/me/` uses: a valid token, then
`is_operator` inside, refused with 403 otherwise. Both routes are therefore
on the allowlist of `tests/test_admin_namespace_is_gated.py`, each with
this reason.

WHY REGENERATION NEEDS A LIVE AUTHENTICATOR CODE
------------------------------------------------
A bearer token is one session. Regenerating recovery codes with the token
alone would let anyone holding a stolen token mint themselves a permanent
way back into the account, which is exactly what a second factor exists to
prevent. So the request must carry a code from the authenticator, now --
and a RECOVERY code is refused here: a recovery code that could mint more
recovery codes would never run out.

THE CODES ARE RETURNED ONCE AND STORED HASHED NOWHERE ELSE
----------------------------------------------------------
django-otp keeps the static tokens as rows and deletes each one as it is
used. This module returns the new set in the one response that creates it,
audits the COUNT, and never logs or stores a code anywhere else. Lost
again means regenerate again, which is the point of requiring the
authenticator for it.
"""

import logging

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base import ops_mfa
from base.permissions import is_operator
from base.utils import error_response, success_response
from servers.admin_audit.services import record_admin_action
from servers.admin_dashboard import login_guard

logger = logging.getLogger(__name__)


def _not_an_operator():
    return error_response(
        code='NOT_AN_OPERATOR',
        message='This account cannot operate the platform.',
        field='general', issue='Not an operator',
        status=status.HTTP_403_FORBIDDEN,
    )


def _status_for(user):
    remaining = ops_mfa.recovery_codes_remaining(user)
    return {
        # Off means off: when the deployment's switch is down the console
        # says so rather than inviting a regeneration that nothing will
        # ever ask for. Production cannot be off (`base/settings.py`).
        'mfa_enabled': ops_mfa.mfa_enabled(),
        'enrolled': ops_mfa.totp_enrolled(user),
        'pending': ops_mfa.pending_totp_device(user) is not None,
        'recovery_codes_remaining': remaining,
        'recovery_codes_low': ops_mfa.recovery_codes_low(remaining),
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def mfa_status(request):
    """The caller's own second-factor state, for the console's Security page.

    Counts and booleans only: no secret, no device id, no code.
    """
    user = request.user
    if not is_operator(user):
        return _not_an_operator()
    return success_response(data=_status_for(user),
                            status_code=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def regenerate_recovery_codes(request):
    """Replace the caller's recovery codes, proving a live authenticator code.

    Every previous code stops working the moment the new set exists
    (`issue_recovery_codes` replaces rather than appends), so a set that
    leaked is dead as soon as the operator notices.
    """
    user = request.user
    if not is_operator(user):
        return _not_an_operator()

    if not ops_mfa.mfa_enabled():
        # Nothing asks for a second factor in this deployment, so a new set
        # would protect nothing and the console would be showing codes that
        # are never checked. Say so, as a conflict with the environment
        # rather than a refusal of the caller.
        return error_response(
            code='MFA_DISABLED',
            message='Multi-factor authentication is switched off in this '
                    'environment, so recovery codes are not in use.',
            field='general', issue='ADMIN_MFA_REQUIRED is false',
            status=status.HTTP_409_CONFLICT,
        )

    if not ops_mfa.totp_enrolled(user):
        return error_response(
            code='MFA_NOT_ENROLLED',
            message='Set up your authenticator first; recovery codes are '
                    'issued when it is confirmed.',
            field='general', issue='No confirmed authenticator',
            status=status.HTTP_409_CONFLICT,
        )

    code = str(request.data.get('mfa_code') or '').strip()
    if not code:
        return error_response(
            code='AUTH_MFA_CODE_REQUIRED',
            message='Enter the current code from your authenticator app.',
            field='mfa_code', issue='Missing mfa_code',
            status=status.HTTP_400_BAD_REQUEST,
        )

    # The same brute-force guard as the sign-in, keyed to this operator and
    # this purpose, so guesses here do not share a counter with sign-in
    # attempts and cannot be used to lock the operator out of signing in.
    guard_key = f'mfa-regen:{user.pk}'
    if login_guard.is_locked(request, guard_key):
        logger.warning('ops_recovery_codes_regenerate_locked user_id=%s', user.pk)
        return _refuse_code()

    # Authenticator ONLY. A recovery code presented here is refused even
    # though it is a valid factor at sign-in -- see the module docstring.
    if ops_mfa.verify_factor(user, code, kinds=('totp',)) is None:
        login_guard.record_failure(request, guard_key)
        logger.warning('ops_recovery_codes_regenerate_refused user_id=%s', user.pk)
        return _refuse_code()
    login_guard.clear(request, guard_key)

    before = ops_mfa.recovery_codes_remaining(user)
    codes = ops_mfa.issue_recovery_codes(user)
    record_admin_action(
        request, action='ops_recovery_codes_regenerated', target_type='operator',
        target_id=user.pk,
        before={'recovery_codes_remaining': before},
        after={'recovery_codes_issued': len(codes), 'shown': 'once'},
        reason='Operator regenerated their recovery codes with a live '
               'authenticator code.',
    )
    return success_response(
        data={
            'recovery_codes': codes,
            'recovery_codes_notice': ops_mfa.RECOVERY_CODES_NOTICE,
            'recovery_codes_remaining': len(codes),
            'recovery_codes_low': False,
        },
        status_code=status.HTTP_200_OK,
    )


def _refuse_code():
    # One answer for a wrong code, an expired code, a recovery code and a
    # locked counter: distinguishing them tells a token thief which it was.
    #
    # 403, NOT 401. The console reads a 401 on any request that carried its
    # bearer token as "the session ended" and signs the operator out; a
    # mistyped code must not do that. The token is fine -- the extra proof
    # this action demands was not given.
    return error_response(
        code='AUTH_INVALID_CODE',
        message='That code did not work. Enter the current code from your '
                'authenticator app.',
        field='mfa_code', issue='Authenticator code rejected',
        status=status.HTTP_403_FORBIDDEN,
    )
