"""Self-service account creation with an email address and a password.

  POST /auth/email/register/   public: create a rider or driver account

WHY THIS DID NOT EXIST
----------------------
Every account in this system was created one of two ways: by the phone+OTP
path, which sets an unusable password, or by an operator through
`/auth/admin/invite/`. Email+password existed for *signing in* and for
*resetting*, so an address could be used to log in only once somebody had
already created the account some other way.

That is why the public website had no registration: there was nothing to
call. This is the missing half.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
**It does not sign you in.** The account is created with the password the
person chose and an UNVERIFIED address, and `email_login` refuses an
unverified address. Verification is what proves the person owns the mailbox;
issuing a session before that would make the verification step decorative.

**It does not say whether an address or a number is already registered.**
Both the created and the already-taken case return the same message. An
endpoint that answers "that email is taken" is a membership oracle for every
address an attacker cares to try, and for a ride-hailing service that leaks
who holds an account. `forgot_password` already takes this line; this matches
it.

**It cannot create an operator.** `role` is checked against the same
SELF_SERVICE_ROLES the OTP path uses, so a self-declared `admin` is refused
with HTTP 400 — the same privilege-escalation guard, in the one other place
an account can now come into existence.

WHY A PHONE NUMBER IS STILL REQUIRED
------------------------------------
`phone_number` is the model's USERNAME_FIELD and is unique and non-null.
Beyond the schema, a ride-hailing account without a number is not usable: the
driver has to be able to reach the rider at the pickup. So registration takes
both, and the result is one account reachable two ways — OTP or password —
rather than a second, parallel identity.

THE NUMBER IS PROVED BEFORE THE ACCOUNT EXISTS (AUDIT 27.78, H4)
----------------------------------------------------------------
Because the number is the unique identity, taking it on trust let anyone
register a stranger's number and lock the stranger out of signing up. The
request must carry `phone_proof`, issued by `/auth/otp/verify/` after a
correct SMS code for this number and role (`phone_proof.py`), and the proof
is spent here.
"""

import logging

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny

from base.countries import normalize_phone
from base.utils import error_response, success_response
from servers.admin_dashboard import login_guard

from .identity import InvalidEmail, normalize_email
from .password_views import _send_verification_for
from .profiles import ensure_role_profile

logger = logging.getLogger(__name__)

user_model = get_user_model()

#: The only roles anybody may give themselves. Mirrors `views.SELF_SERVICE_ROLES`.
SELF_SERVICE_ROLES = ('rider', 'driver')

#: One answer for "created" and for "already registered". See the module note.
GENERIC_ACCEPTED = (
    'Check your email. If we could create the account, a verification link is '
    'on its way; if the address or number was already registered, nothing '
    'changed.'
)

#: The same, for a deployment that cannot send mail at all. See
#: `_verification_is_deliverable` for why this exists and why it is safe.
GENERIC_ACCEPTED_NO_MAIL = (
    'If we could create the account, it is ready to sign in; if the address '
    'or number was already registered, nothing changed.'
)


#: What the person reads when the phone proof is refused, per reason.
_PROOF_MESSAGES = {
    'AUTH_PHONE_PROOF_REQUIRED': 'Verify your mobile number with the code we text you, then try again.',
    'AUTH_PHONE_PROOF_INVALID': 'That verification is not for this mobile number. Verify the number again.',
    'AUTH_PHONE_PROOF_EXPIRED': 'Your mobile verification has expired. Verify the number again.',
    'AUTH_PHONE_PROOF_USED': 'That verification has already been used. Verify the number again.',
}

def _verification_is_deliverable():
    """See `base.auth_email.verification_is_deliverable`.

    Kept as a module-level name because the tests patch it here, and because
    the registration path and the sign-in path must answer this question
    identically -- which is why the rule itself lives in one place.
    """
    from base.auth_email import verification_is_deliverable

    return verification_is_deliverable()


def _register_rider_by_email(request, data):
    """Name, email and password -> a working rider account and a session (201).

    The ruling trades two old rules for a simpler sign-up: a taken address is said so (with Log in
    and Forgot password on the form), and the account works before the address is confirmed. The
    confirmation link is still sent; the lockout still counts every attempt per address.
    """
    from .email_auth_views import _issue_tokens
    from .phone_identity import placeholder_phone

    full_name = (data.get('full_name') or '').strip()
    password = data.get('password') or ''
    if not full_name:
        return error_response(code='AUTH_VALIDATION_ERROR', message='Tell us what to call you.',
                              field='full_name', issue='Name required',
                              status=status.HTTP_400_BAD_REQUEST)
    try:
        email = normalize_email(data.get('email'))
    except InvalidEmail as exc:
        email = None
        message = str(exc) or 'Enter a valid email address.'
    else:
        message = 'Enter a valid email address.'
    if not email:
        return error_response(code='AUTH_INVALID_EMAIL', message=message, field='email',
                              issue='Invalid email', status=status.HTTP_400_BAD_REQUEST)
    if len(password) < 10:
        return error_response(code='AUTH_WEAK_PASSWORD', message='Use at least 10 characters.',
                              field='password', issue='Password too short',
                              status=status.HTTP_400_BAD_REQUEST)
    try:
        validate_password(password, user=user_model(email=email, full_name=full_name))
    except DjangoValidationError as exc:
        return error_response(code='AUTH_WEAK_PASSWORD', message=' '.join(exc.messages),
                              field='password', issue='Password rejected',
                              status=status.HTTP_400_BAD_REQUEST)
    if login_guard.is_locked(request, email):
        return error_response(code='AUTH_RATE_LIMITED',
                              message='Too many attempts. Try again in a few minutes.',
                              field='email', issue='Locked', status=status.HTTP_429_TOO_MANY_REQUESTS)
    if user_model.objects.filter(email__iexact=email).exists():
        login_guard.record_failure(request, email)
        return error_response(code='AUTH_EMAIL_IN_USE',
                              message='This email already has an account.', field='email',
                              issue='Address belongs to an existing account',
                              status=status.HTTP_409_CONFLICT)
    try:
        with transaction.atomic():
            user = user_model(phone_number=placeholder_phone(), username='', email=email,
                              role='rider', full_name=full_name, is_active=True)
            user.username = user.phone_number
            user.set_password(password)
            user.email_verified_at = None
            user.save()
            ensure_role_profile(user)
    except IntegrityError:
        return error_response(code='AUTH_EMAIL_IN_USE',
                              message='This email already has an account.', field='email',
                              issue='Lost a race with another sign-up',
                              status=status.HTTP_409_CONFLICT)
    # 12 Nov: a rider's address is never confirmed on its behalf, even where mail cannot be sent
    # (QA): the rider signs in unconfirmed anyway, and sees "Confirm your email" until the link is
    # opened. `_send_verification_for` logs `email_verify_sent ... delivered=` either way.
    sent = _send_verification_for(user)
    user.last_login = timezone.now()
    user.save(update_fields=['last_login'])
    logger.info('register_ok user_id=%s role=rider email_only=1', user.pk)
    return success_response(data={**_issue_tokens(user), 'verification_required': False,
                                  'confirmation_sent': bool(sent)},
                            status_code=status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([AllowAny])
def register_with_email(request):
    """Create a rider or driver account from an email and a password.

    Expected:
        {"full_name", "phone_number", "email", "password",
         "confirm_password", "role": "rider"|"driver",
         "phone_proof": from POST /auth/otp/verify/ for this number and role}
    """
    data = request.data
    raw_email = data.get('email')
    raw_phone = data.get('phone_number')
    password = data.get('password') or ''
    confirm = data.get('confirm_password') or ''
    full_name = (data.get('full_name') or '').strip()
    role = (data.get('role') or 'rider').strip().lower()

    if role not in SELF_SERVICE_ROLES:
        # The same refusal the OTP path gives. Authority is never taken from
        # what a client asks to be.
        logger.warning('register_refused reason=privileged_role role=%s', role)
        return error_response(
            code='AUTH_INVALID_ROLE',
            message=f'Role must be one of {", ".join(SELF_SERVICE_ROLES)}.',
            field='role',
            issue='Invalid role specified',
            status=status.HTTP_400_BAD_REQUEST,
        )

    # RIDERS SIGN UP BY EMAIL ONLY (11 Nov, Srini's ruling). The number is asked once, at the
    # first booking, and proved there; drivers keep the proved-number sign-up below.
    if role == 'rider' and not data.get('phone_number'):
        return _register_rider_by_email(request, data)

    # A driver's name follows the driver-name rule from the first request (AUDIT 27.76): sign-up
    # used to store a name that profile completion then refused.
    if role == 'driver' and full_name:
        from servers.driver.serializers import is_valid_driver_name

        if not is_valid_driver_name(full_name):
            return error_response(
                code='AUTH_VALIDATION_ERROR',
                message='Enter your full name.',
                field='full_name',
                issue='Letters, spaces and . \' - only, at least 2 characters',
                status=status.HTTP_400_BAD_REQUEST,
            )

    try:
        email = normalize_email(raw_email)
    except InvalidEmail as exc:
        return error_response(
            code='AUTH_INVALID_EMAIL',
            message=str(exc) or 'Enter a valid email address.',
            field='email',
            issue='Invalid email',
            status=status.HTTP_400_BAD_REQUEST,
        )

    # No implicit country (module 15): a bare national number is read in the country the client
    # names, else the sole live country; with two live countries it needs its country code.
    from servers.platform_ops.markets import default_phone_country
    phone = (normalize_phone(raw_phone,
                             default_country=request.data.get('country') or default_phone_country())
             if raw_phone else None)
    if not phone:
        return error_response(
            code='AUTH_INVALID_PHONE',
            message='Enter your mobile number in international format, like '
                    '+919876543210.',
            field='phone_number',
            issue='Invalid phone number',
            status=status.HTTP_400_BAD_REQUEST,
        )

    if password != confirm:
        return error_response(
            code='AUTH_PASSWORD_MISMATCH',
            message='Both passwords must match.',
            field='confirm_password',
            issue='Passwords do not match',
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Django's configured validators: length, commonness, all-numeric, and
    # similarity to the values the person just typed.
    try:
        validate_password(
            password,
            user=user_model(email=email, phone_number=phone,
                            full_name=full_name or ''),
        )
    except DjangoValidationError as exc:
        return error_response(
            code='AUTH_WEAK_PASSWORD',
            message=' '.join(exc.messages),
            field='password',
            issue='Password rejected',
            status=status.HTTP_400_BAD_REQUEST,
        )

    # THE NUMBER MUST BE PROVED FIRST (AUDIT 27.78, H4). The number is the
    # account's unique identity, so registering it on trust let anyone take a
    # stranger's number -- the stranger then could not sign up at all. The
    # proof comes from `/auth/otp/` + `/auth/otp/verify/` and is bound to this
    # number and role; it is spent here, whatever happens next, so one SMS
    # code buys at most one registration attempt.
    from . import phone_proof

    def _same_phone(proved, submitted):
        return proved == submitted or normalize_phone(
            proved, default_country=request.data.get('country') or default_phone_country(),
        ) == submitted

    proof_error = phone_proof.redeem(
        data.get('phone_proof'), phone, role, same_phone=_same_phone,
    )
    if proof_error is not None:
        logger.warning('register_refused reason=phone_proof code=%s', proof_error)
        return error_response(
            code=proof_error,
            message=_PROOF_MESSAGES.get(proof_error, _PROOF_MESSAGES[phone_proof.INVALID]),
            field='phone_proof',
            issue='Verify your mobile number with the SMS code first',
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Rate limit on the identity, with the same guard the login views use, so
    # this cannot be driven as an account-creation or enumeration engine.
    if login_guard.is_locked(request, email):
        logger.warning('register_locked')
        return success_response(
            data={'message': GENERIC_ACCEPTED},
            status_code=status.HTTP_202_ACCEPTED,
        )

    taken = (
        user_model.objects.filter(email__iexact=email).exists()
        or user_model.objects.filter(phone_number=phone).exists()
    )
    if taken:
        # Identical response to the success case. Recorded as a failure so a
        # script walking a list of addresses still hits the lockout.
        login_guard.record_failure(request, email)
        logger.info('register_noop reason=already_registered')
        return success_response(
            data={'message': GENERIC_ACCEPTED},
            status_code=status.HTTP_202_ACCEPTED,
        )

    try:
        with transaction.atomic():
            user = user_model.objects.create_user(
                phone_number=phone,
                username=phone,
                email=email,
                role=role,
                full_name=full_name or '',
            )
            user.set_password(password)
            # Unverified on purpose: `email_login` refuses until the address
            # is proven, which is what makes the verification step real.
            user.email_verified_at = None
            user.save(update_fields=['password', 'email_verified_at'])

            # THE PROFILE ROW IS PART OF THE ACCOUNT, not an extra.
            #
            # This branch used to read `if role == 'rider'` and create a
            # Rider -- and nothing at all for a driver. Every driver who
            # signed up through "Apply to drive" on the website therefore
            # got an account with no `Driver` row, and `request.user.driver`
            # in servers/driver/views.py raised, so every driver endpoint
            # answered HTTP 500 for them. They could sign in and then do
            # nothing. One helper now, shared with the OTP path and the
            # invite path, so the role always gets what the role needs.
            ensure_role_profile(user)
    except IntegrityError:
        # Lost a race against a concurrent registration for the same
        # identity. Same answer as "taken"; nothing is disclosed.
        logger.info('register_noop reason=race')
        return success_response(
            data={'message': GENERIC_ACCEPTED},
            status_code=status.HTTP_202_ACCEPTED,
        )

    logger.info('register_ok user_id=%s role=%s', user.pk, role)

    deliverable = _verification_is_deliverable()

    if deliverable:
        # Through the one outbound gate, so an unconfigured or allowlisted
        # environment refuses to send rather than mailing a stranger. A
        # blocked send is not an error for the caller: the account exists
        # either way, and the operator can verify it another way.
        _send_verification_for(user)
    else:
        # No mail backend, and not production. Requiring a click in an email
        # that cannot be sent would leave this account permanently unusable.
        user.email_verified_at = timezone.now()
        user.save(update_fields=['email_verified_at'])
        logger.info(
            'register_autoverified user_id=%s reason=mail_not_deliverable', user.pk,
        )

    return success_response(
        data={
            'message': GENERIC_ACCEPTED if deliverable else GENERIC_ACCEPTED_NO_MAIL,
            # A PROPERTY OF THE DEPLOYMENT, NOT OF THIS ADDRESS. It is the
            # same for every caller, so it tells nobody whether a particular
            # address was already registered -- which is the one thing these
            # responses are careful not to reveal.
            'verification_required': deliverable,
        },
        status_code=status.HTTP_201_CREATED,
    )
