"""Multi-factor authentication for operator accounts.

WHY OPERATORS AND NOT EVERYONE
------------------------------
An operator account approves driver KYC, releases payouts and can read every
rider's trip history and every driver's live position. A rider account can book a
ride. Those do not warrant the same friction, and forcing a second factor on ten
thousand riders to protect four operators would be paid for by the riders.

So: MFA guards the operator surface, and riders and drivers are untouched.

NO CRYPTOGRAPHY IS IMPLEMENTED HERE
-----------------------------------
`django-otp` does all of it: TOTP per RFC 6238 (`otp_totp.TOTPDevice`) and
single-use static recovery codes (`otp_static.StaticDevice`). This module decides
*policy* -- who must present a factor, when, and what happens when they cannot.
It never touches a secret, generates a code, or compares one itself; verification
goes through the library's `device.verify_token`, which handles the time window and,
importantly, refuses a token it has already accepted.

THE THREE THINGS THAT MAKE MFA REAL RATHER THAN DECORATIVE
----------------------------------------------------------
1. **It cannot be skipped by calling the API directly.** A console that asks for a
   code while `/api/v1/driver/admin/withdrawals/1/approve/` does not is a login
   form, not a control. So MFA is enforced where the *credential is issued*: the
   console session is not treated as verified until a factor is presented, and the
   API's operator token is not minted at all without one.

2. **An operator cannot authenticate through the rider/driver SMS path.**
   `/api/v1/auth/login/` exchanges an SMS OTP for a token with no password and no
   second factor. If an operator could use it, every control below would be
   bypassable by anyone who could receive that operator's SMS -- which is one
   SIM-swap. Operators sign in with password plus TOTP, and only that.

3. **Production has no off switch.** `OPS_MFA_ENFORCED` defaults to True in
   production and there is no variable that turns it off there, matching the
   existing boot guards: a guard that can be satisfied by setting a flag is a
   comment. Outside production it is enforced for any account that has enrolled,
   so QA can rehearse both the enrolled and the not-yet-enrolled path.

LOCKOUT, WHICH IS THE REAL RISK IN A SMALL PILOT
------------------------------------------------
With one or two operators, a lost phone is an outage of the whole operations
function. Two mitigations, both deliberate:

  * enrolment issues single-use static recovery codes, which are presented once and
    are the operator's responsibility thereafter;
  * `manage.py reset_operator_mfa` removes an account's devices, and it requires
    shell access to the deployment. That is the correct bar for break-glass: it
    cannot be reached from the internet, and it is audited.
"""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

# Recovery codes issued at enrolment. Ten is the common default; enough that an
# operator can lose a few without being locked out, few enough to write down.
RECOVERY_CODE_COUNT = 10


def _totp_model():
    from django_otp.plugins.otp_totp.models import TOTPDevice
    return TOTPDevice


def _static_models():
    from django_otp.plugins.otp_static.models import StaticDevice, StaticToken
    return StaticDevice, StaticToken


def mfa_enforced() -> bool:
    """Is a second factor mandatory for operators in this environment?

    Production: always, with no override. Elsewhere: whatever the setting says,
    defaulting to off so a fresh QA deployment can bootstrap an operator and enrol
    them rather than locking itself out before anyone has a device.
    """
    if getattr(settings, 'IS_PRODUCTION', False):
        return True
    return bool(getattr(settings, 'OPS_MFA_ENFORCED', False))


def devices_for(user):
    """Every confirmed factor on this account: TOTP first, then recovery codes."""
    if user is None or not getattr(user, 'pk', None):
        return []
    TOTPDevice = _totp_model()
    StaticDevice, _ = _static_models()
    out = list(TOTPDevice.objects.filter(user=user, confirmed=True))
    out += list(StaticDevice.objects.filter(user=user, confirmed=True))
    return out


def has_device(user) -> bool:
    """Has this account enrolled a second factor?"""
    return bool(devices_for(user))


def mfa_required_for(user) -> bool:
    """Must THIS account present a second factor to sign in?

    Enforced environments: yes, for every operator, whether or not they have
    enrolled -- an operator without a device cannot sign in, which is what makes
    enrolment happen rather than being deferred forever.

    Other environments: only if they have enrolled, so adding MFA does not lock
    QA out of a console nobody has a device for yet.
    """
    from base.permissions import is_operator

    if not is_operator(user):
        return False
    return True if mfa_enforced() else has_device(user)


def verify_code(user, code) -> bool:
    """Check a submitted code against this account's factors.

    Tries each device in turn and returns on the first that accepts. A TOTP device
    refuses a token it has already accepted (django-otp tracks the last used
    counter), so a code captured in transit cannot be replayed. A static token is
    consumed when used.

    Never raises: an infrastructure failure here must read as "not verified", not
    as an exception that a caller might mistake for a pass.
    """
    if not code:
        return False
    code = str(code).strip().replace(' ', '')
    for device in devices_for(user):
        try:
            if device.verify_token(code):
                logger.info(
                    'ops_mfa_verified user_id=%s device=%s',
                    getattr(user, 'id', None), type(device).__name__,
                )
                return True
        except Exception as exc:  # noqa: BLE001
            logger.error(
                'ops_mfa_device_error user_id=%s device=%s detail=%s',
                getattr(user, 'id', None), type(device).__name__, exc,
            )
    logger.warning('ops_mfa_rejected user_id=%s', getattr(user, 'id', None))
    return False


def session_is_verified(request) -> bool:
    """Has THIS session presented a factor?

    `OTPMiddleware` sets `request.user.is_verified()` from `request.session`, so
    this is a property of the session rather than of the account. That distinction
    is the whole point: an account having a device does not mean the browser
    holding this cookie has ever proved it.
    """
    user = getattr(request, 'user', None)
    checker = getattr(user, 'is_verified', None)
    return bool(checker()) if callable(checker) else False


def mark_session_verified(request, user=None):
    """Record on the session that a factor was presented.

    Delegates to django-otp's `otp_login`, which stores the device id on the
    session so `is_verified()` answers True for subsequent requests in the same
    session and no longer.
    """
    from django_otp import login as otp_login

    user = user or getattr(request, 'user', None)
    device = next(iter(devices_for(user)), None)
    if device is None:
        return False
    try:
        otp_login(request, device)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error('ops_mfa_session_mark_failed user_id=%s detail=%s',
                     getattr(user, 'id', None), exc)
        return False


def console_access_allowed(request) -> bool:
    """May this request reach an operator console page?

    Authority AND session assurance, which are different questions:
    `is_operator` says the account is allowed to operate; this says the session in
    front of us has proved who it belongs to.
    """
    from base.permissions import is_operator

    user = getattr(request, 'user', None)
    if not is_operator(user):
        return False
    if not mfa_required_for(user):
        return True
    return session_is_verified(request)


def enroll_totp(user, issuer=None):
    """Create an unconfirmed TOTP device and return (device, provisioning_uri).

    The URI contains the shared secret. It is a credential: it goes to the operator
    once, and it is never logged, never stored in an audit row, and never returned
    by an HTTP endpoint.
    """
    TOTPDevice = _totp_model()
    issuer = issuer or getattr(settings, 'PLATFORM_BRAND_NAME', 'SaaradhiGo')
    device = TOTPDevice.objects.create(user=user, name='operator-totp',
                                       confirmed=False)
    uri = device.config_url
    if 'issuer=' not in uri:
        uri = f'{uri}&issuer={issuer}'
    return device, uri


def confirm_totp(device, code) -> bool:
    """Confirm enrolment by proving the operator's authenticator is in sync.

    Without this step an operator could be enrolled against a secret they never
    successfully scanned, and would discover it at the moment they were locked out.
    """
    try:
        if device.verify_token(str(code).strip()):
            device.confirmed = True
            device.save(update_fields=['confirmed'])
            return True
    except Exception as exc:  # noqa: BLE001
        logger.error('ops_mfa_confirm_error device=%s detail=%s', device.pk, exc)
    return False


def issue_recovery_codes(user, count=RECOVERY_CODE_COUNT):
    """Replace this account's recovery codes and return the new ones.

    Returned once, to be handed to the operator. Replacing rather than appending,
    so a leaked old set stops working the moment a new set is issued.
    """
    StaticDevice, StaticToken = _static_models()
    StaticDevice.objects.filter(user=user).delete()
    device = StaticDevice.objects.create(user=user, name='operator-recovery',
                                         confirmed=True)
    codes = []
    for _ in range(count):
        token = StaticToken.random_token()
        StaticToken.objects.create(device=device, token=token)
        codes.append(token)
    return codes


def remove_devices(user) -> int:
    """Break-glass: drop every factor on this account. Returns how many."""
    TOTPDevice = _totp_model()
    StaticDevice, _ = _static_models()
    removed = 0
    removed += TOTPDevice.objects.filter(user=user).delete()[0] or 0
    removed += StaticDevice.objects.filter(user=user).delete()[0] or 0
    return removed
