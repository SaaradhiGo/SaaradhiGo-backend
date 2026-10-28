import logging
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from base.utils import success_response, error_response
from base.countries import normalize_phone
from base.permissions import IsAdmin, is_operator
from servers.admin_access import breakglass
from servers.admin_access.bridge import ScopedPermission
from servers.admin_dashboard import login_guard
from base import ops_mfa
from servers.admin_audit.services import record_admin_action
from django.contrib.auth import get_user_model
from servers.auth_user.serializers import UserModelSerializer
from servers.auth_user.identity import InvalidEmail, normalize_email
from servers.auth_user.password_views import _validate_new_password
from servers.auth_user.session_revocation import revoke_after_credential_change
from rest_framework.pagination import PageNumberPagination
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

logger = logging.getLogger(__name__)

user_model = get_user_model()

@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('pii.view')])
def admin_list_users(request):
    """
    Admin view to list all users with pagination and filtering.
    Filters:
    - role: rider | driver | admin
    - is_active: true | false

    GLOBAL BY DESIGN, SO GATED ON pii.view INSTEAD OF SCOPE
    ------------------------------------------------------
    A rider belongs to no city — they travel — so there is nothing to scope
    this list by; see `admin_access.scoping.RIDERS_ARE_GLOBAL`. What it
    returns is names, phone numbers and email addresses for every account
    in both countries, so the gate is `pii.view`, which the Analyst role
    deliberately does not hold.

    Interim. Phase 1(e) masks these fields by default and logs each reveal,
    at which point this drops to `rider.read` and the masking carries the
    PII decision per field rather than per endpoint.
    """
    try:
        users = user_model.objects.all().order_by('-date_joined')

        role = request.query_params.get('role')
        if role:
            users = users.filter(role=role)

        is_active = request.query_params.get('is_active')
        if is_active is not None:
            active_bool = str(is_active).lower() == 'true'
            users = users.filter(is_active=active_bool)

        paginator = PageNumberPagination()
        paginator.page_size = 20
        paginator.page_size_query_param = 'page_size'
        paginator.max_page_size = 100
        
        result_page = paginator.paginate_queryset(users, request)
        serializer = UserModelSerializer(result_page, many=True)
        return success_response(paginator.get_paginated_response(serializer.data).data, status.HTTP_200_OK)

    except Exception as e:
        return error_response(
            code='INTERNAL_ERROR',
            message='An unexpected error occurred',
            field='general',
            issue=str(e),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


def _issue_recovery_codes_if_first(request, user):
    """The recovery codes, ONCE, on the first token an enrolled account gets.

    ISSUED WITH THE TOKEN, NOT AT THE MOMENT OF CONFIRMATION. The pending
    authenticator is confirmed by its first valid code, but two gates can
    still answer that same request with a 401: the break-glass reason and
    the forced password change. A set issued at confirmation and returned
    on a 401 would be a set the operator never saw -- and it would not be
    issued again, because the device is confirmed now. So the set is issued
    here, where the response is the one that carries the token, and the
    rule is "a confirmed authenticator with no recovery set yet" rather
    than "confirmed a moment ago". That rule also reaches the operators who
    confirmed at sign-in BEFORE this existed and hold no codes at all:
    their next sign-in hands them a set, once.

    A set whose codes have all been spent still exists, so it does not
    trigger this: that operator regenerates from the console, proving a
    live authenticator code, rather than being handed a new set by the
    act of signing in.

    Returns the fields to merge into the response: empty on every sign-in
    but that one. The audit row carries the count, never a code.
    """
    if not ops_mfa.mfa_enabled():
        return {}
    if not ops_mfa.totp_enrolled(user) or ops_mfa.has_recovery_device(user):
        return {}
    codes = ops_mfa.issue_recovery_codes(user)
    record_admin_action(
        request, action='ops_recovery_codes_issued', target_type='operator',
        target_id=user.pk,
        after={'recovery_codes_issued': len(codes), 'shown': 'once_at_sign_in'},
        reason='First token after the authenticator was confirmed.',
    )
    return {
        'recovery_codes': codes,
        'recovery_codes_notice': ops_mfa.RECOVERY_CODES_NOTICE,
        'recovery_codes_remaining': len(codes),
        'recovery_codes_low': False,
    }


@api_view(['POST'])
def admin_login(request):
    """Password sign-in for an operator, issuing a JWT pair.

    THREE DEFECTS THIS VIEW HAD
    ---------------------------
    **It was completely unthrottled.** No `throttle_classes`, and the project sets
    `DEFAULT_THROTTLE_RATES` without `DEFAULT_THROTTLE_CLASSES`, so nothing applied.
    The Django console form at `/login/` was measured at 12 failed logins in 7.8
    seconds before `login_guard` was added; this endpoint had exactly that exposure
    and no guard at all -- an unmetered password oracle for the accounts that
    approve KYC and release payouts. It now shares `login_guard` with the console,
    so the two operator sign-in surfaces cannot have different brute-force
    resistance.

    **Its authorization gate was the weakest of four.** It refused only when
    `role != 'admin' AND not is_staff AND not is_superuser` -- an "any of three"
    gate, so `is_staff` alone was enough. A rider carrying `is_staff` could mint an
    operator token here while `IsAdmin` refused them everywhere else. Now
    `is_operator`, which is the one definition.

    **It enumerated users.** An unknown phone returned `issue='User not found'` and
    a wrong password returned `issue='Incorrect password'` -- two distinguishable
    401s, which is a clean oracle for which numbers hold accounts. A third distinct
    403 (`AUTH_NOT_ADMIN`) then told an attacker which of those accounts are
    operators. All three are now one identical refusal.
    """
    try:
        # EMAIL OR PHONE, because an operator is a person with an email
        # address.
        #
        # This endpoint took a phone number and only a phone number, and the
        # console's own sign-in screen took a phone and a one-time code. So
        # the owner's operator account -- created with and identified by
        # admin@saaradhigo.com -- had no door it could walk through, and
        # "sign in as admin with your email and password" was not a feature
        # anybody could use.
        #
        # The address is resolved the same way every other email path
        # resolves one (trim, NFKC, lower-case, unique), so `A@B.com` and
        # `a@b.com` are the same operator here as everywhere else. Nothing
        # about the authorization below changes: the password, the operator
        # check, MFA and the lockout are identical whichever identifier was
        # used to find the account.
        raw_email = request.data.get('email')
        phone_number = request.data.get('phone_number')
        password = request.data.get('password')

        if not (raw_email or phone_number) or not password:
            return error_response(
                code='AUTH_MISSING_CREDENTIALS',
                message='An email address or phone number, and a password, are required',
                field='general',
                issue='Missing credentials',
                status=status.HTTP_400_BAD_REQUEST
            )

        email = ''
        if raw_email:
            try:
                email = normalize_email(raw_email)
            except InvalidEmail:
                # Shaped like the generic refusal rather than a validation
                # error: "that is not an address" and "no such operator"
                # must not be distinguishable here.
                email = ''

        if phone_number:
            # Format phone number to E.164 if it's not already.
            #
            # Was an inline +91 prepend, so a US operator could not sign in
            # here either. One helper, resolving against the country registry.
            phone_number = normalize_phone(str(phone_number).strip()) or str(
                phone_number).strip()

        # The lockout key. One namespace for both identifier kinds -- an
        # email and a phone can never collide as strings -- so a hundred
        # guesses against admin@saaradhigo.com cannot be continued against
        # the same operator's number with a fresh counter.
        identifier = email or phone_number

        # One refusal for every failure below: unknown identifier, wrong
        # password, correct password on a non-operator account, and locked
        # out. Anything that distinguishes them is an oracle.
        def _refuse():
            return error_response(
                code='AUTH_INVALID_CREDENTIALS',
                message='Invalid credentials',
                field='general',
                issue='Invalid credentials',
                status=status.HTTP_401_UNAUTHORIZED
            )

        if not identifier:
            return _refuse()

        if login_guard.is_locked(request, identifier):
            return _refuse()

        user = None
        if email:
            user = user_model.objects.filter(email__iexact=email).first()
        elif phone_number:
            user = user_model.objects.filter(phone_number=phone_number).first()

        if user is None:
            # Hash and discard, so an unknown identifier costs the same time
            # as a wrong password and the endpoint is not a timing oracle for
            # which operator accounts exist.
            user_model().set_password(password)
            login_guard.record_failure(request, identifier)
            return _refuse()

        if not user.check_password(password):
            login_guard.record_failure(request, identifier)
            return _refuse()

        # A CORRECT password on a non-operator account still counts as a failure.
        # Otherwise every rider and driver account is an unthrottled oracle for
        # password guessing -- the attacker simply aims at a non-operator.
        if not is_operator(user):
            login_guard.record_failure(request, identifier)
            return _refuse()

        # MFA at the point the credential is ISSUED.
        #
        # This is what stops MFA from being a login form. If the token were minted
        # first and the factor demanded later, or demanded only by the console, then
        # POSTing straight to /driver/admin/withdrawals/1/approve/ with this token
        # would skip it entirely. A token from this endpoint therefore represents a
        # session that has already presented two factors, and every downstream
        # authorization check can trust it without knowing about MFA at all.
        #
        # A PENDING DEVICE IS CONFIRMED HERE, AT FIRST SIGN-IN.
        #
        # `create_operator --enrol-mfa` and `enroll_operator_mfa` create the
        # TOTP device UNCONFIRMED: the secret has been printed in a shell,
        # but nobody has yet proved an authenticator holds it. Confirming
        # used to need a second shell run with a live code, which the
        # operator being enrolled cannot do themselves. So an account with
        # no confirmed factor and one pending device is asked for a code
        # like an enrolled one, and the first code that verifies confirms
        # the device -- the same proof `confirm_totp` always required, made
        # at the one moment the operator is certainly holding the app.
        # From then on the account is enrolled and MFA applies as usual.
        pending = (None if not ops_mfa.mfa_enabled() or ops_mfa.has_device(user)
                   else ops_mfa.pending_totp_device(user))
        # Set when a RECOVERY code signed this request in: the response then
        # says how many are left, and the audit row says one was spent.
        recovery_used = False
        if ops_mfa.mfa_required_for(user) or pending is not None:
            # str() first: JSON allows a numeric literal, and a client that
            # sends `"mfa_code": 123456` would otherwise 500 on .strip().
            code = str(request.data.get('mfa_code') or '').strip()
            # A LOST AUTHENTICATOR IS NOT A LOCKOUT. `recovery_code` is one
            # of the single-use codes issued when the authenticator was
            # confirmed (below) or regenerated from the console
            # (`mfa_views`). It is checked against the recovery set ONLY,
            # so the field says what it carries. A recovery code sent as
            # `mfa_code` still works -- the console did that before this
            # field existed -- and is counted the same way.
            recovery_code = str(request.data.get('recovery_code') or '').strip()
            if not ops_mfa.has_device(user) and pending is None:
                # Enforced environment, operator with nothing enrolled. Refused
                # rather than waved through, with the same generic message so this
                # does not report which operators lack a second factor.
                login_guard.record_failure(request, identifier)
                logger.warning('ops_api_login_refused_no_mfa_device user_id=%s',
                               user.id)
                return _refuse()
            if not code and not (recovery_code and pending is None):
                # A distinct, non-terminal answer: the caller has proved the first
                # factor and needs to be told to send the second. It deliberately
                # carries no hint about whether the account exists -- it is only
                # reachable AFTER a correct password for a real operator. A
                # pending authenticator can only be confirmed by its own
                # code, so a recovery code alone is answered the same way.
                return error_response(
                    code='AUTH_MFA_REQUIRED',
                    message='An authentication code is required',
                    field='mfa_code',
                    issue='Second factor required',
                    status=status.HTTP_401_UNAUTHORIZED,
                )
            if pending is not None:
                if not ops_mfa.confirm_totp(pending, code):
                    login_guard.record_failure(request, identifier)
                    return _refuse()
                # The recovery codes are issued when the TOKEN is minted,
                # not here: the break-glass reason and the forced password
                # change still stand between this point and a response the
                # operator sees, and a set returned on a 401 would be a set
                # nobody saw. See `_issue_recovery_codes_if_first` below.
                record_admin_action(
                    request, action='ops_mfa_enrolled', target_type='operator',
                    target_id=user.pk,
                    after={'method': 'totp', 'confirmed_at': 'first_sign_in',
                           'recovery_codes': 'issued_with_first_token'},
                    reason='Pending authenticator confirmed by its first '
                           'valid code at sign-in.',
                )
            elif recovery_code:
                if ops_mfa.verify_factor(user, recovery_code,
                                         kinds=('static',)) is None:
                    login_guard.record_failure(request, identifier)
                    return _refuse()
                recovery_used = True
            else:
                device = ops_mfa.verify_factor(user, code)
                if device is None:
                    login_guard.record_failure(request, identifier)
                    return _refuse()
                recovery_used = ops_mfa.is_recovery_device(device)

        # BREAK-GLASS: a reason, before the token.
        #
        # Checked here rather than earlier so it cannot be used to probe
        # which accounts are the emergency one -- it is only reachable
        # after a correct password, an operator check and MFA.
        if breakglass.is_break_glass(user):
            try:
                reason = breakglass.validate_reason(request.data.get('reason'))
            except breakglass.ReasonRequired as exc:
                # Non-terminal and distinct, like AUTH_MFA_REQUIRED: the
                # caller has proved everything else and needs to say why.
                return error_response(
                    code='AUTH_BREAK_GLASS_REASON_REQUIRED',
                    message=str(exc),
                    field='reason',
                    issue='A reason is required to use the emergency account',
                    status=status.HTTP_401_UNAUTHORIZED,
                )
            breakglass.announce_use(user, reason, request=request, channel='api')

        # A TEMPORARY PASSWORD IS REPLACED BEFORE ANY TOKEN EXISTS.
        #
        # `create_operator` issues a generated password through somebody
        # else's terminal, so until the operator has chosen their own it is
        # not a secret they alone hold. Last, after MFA and break-glass: the
        # change is made by somebody who has proved every factor, and no
        # token is minted on the temporary credential at all -- the console
        # cannot be reached with it, however briefly. The console's
        # `AUTH_PASSWORD_CHANGE_REQUIRED` step resends the same sign-in with
        # `new_password` added.
        if getattr(user, 'must_change_password', False):
            new_password = request.data.get('new_password')
            if not new_password:
                return error_response(
                    code='AUTH_PASSWORD_CHANGE_REQUIRED',
                    message='Choose a new password to finish signing in.',
                    field='new_password',
                    issue='Password change required',
                    status=status.HTTP_401_UNAUTHORIZED,
                )
            new_password = str(new_password)
            if new_password == str(password):
                return error_response(
                    code='AUTH_PASSWORD_WEAK',
                    message='The new password must be different from the '
                            'temporary one.',
                    field='new_password',
                    issue='New password equals the temporary password',
                    status=status.HTTP_400_BAD_REQUEST,
                )
            invalid = _validate_new_password(new_password, user)
            if invalid is not None:
                return invalid
            user.set_password(new_password)
            user.must_change_password = False
            user.save(update_fields=['password', 'must_change_password'])
            revoke_after_credential_change(
                user, request,
                reason='Temporary password replaced at first sign-in.')
            record_admin_action(
                request, action='ops_password_changed', target_type='operator',
                target_id=user.pk, after={'forced': True},
                reason='Temporary password replaced at first sign-in.',
            )

        login_guard.clear(request, identifier)
        record_admin_action(
            request, action='ops_api_login', target_type='operator',
            target_id=user.pk,
            after={
                'mfa': bool(ops_mfa.mfa_required_for(user)),
                'break_glass': breakglass.is_break_glass(user),
                'recovery_code_used': recovery_used,
            },
        )
        access_token = AccessToken.for_user(user)
        refresh_token = RefreshToken.for_user(user)
        user_serializer = UserModelSerializer(user)

        data = {
            'token': str(access_token),
            'refresh_token': str(refresh_token),
            'user': user_serializer.data,
        }
        if recovery_used:
            # Counted AFTER the code was consumed, so "remaining" is what is
            # left, and audited as a count: the audit trail never holds a
            # code, used or unused.
            remaining = ops_mfa.recovery_codes_remaining(user)
            data['recovery_codes_remaining'] = remaining
            data['recovery_codes_low'] = ops_mfa.recovery_codes_low(remaining)
            record_admin_action(
                request, action='ops_recovery_code_used', target_type='operator',
                target_id=user.pk,
                after={'recovery_codes_remaining': remaining},
                reason='Signed in with a single-use recovery code.',
            )
        else:
            data.update(_issue_recovery_codes_if_first(request, user))

        return success_response(data=data, status_code=status.HTTP_200_OK)

    except Exception as e:
        return error_response(
            code='INTERNAL_ERROR',
            message='An unexpected error occurred',
            field='general',
            issue=str(e),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
