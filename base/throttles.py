"""Custom DRF throttle classes keyed on the phone_number in the request body.

Used by the OTP-issue and OTP-verify endpoints so brute-forcing is bounded
per target identity, not just per source IP (an attacker can rotate IPs).

Test phones configured via settings.TEST_PHONE_NUMBERS bypass throttling
so QA can iterate without waiting for cool-downs. Bypassing only those
specific listed numbers is safe — they're operator-controlled.
"""

from django.conf import settings
from rest_framework.throttling import SimpleRateThrottle


class _PhoneNumberRateThrottle(SimpleRateThrottle):
    """Base: key the throttle on the phone_number from the request body.

    If the request omits phone_number, the request is not throttled — the
    view's own validation will reject it anyway. Phones listed in
    settings.TEST_PHONE_NUMBERS also skip throttling.
    """

    scope = ''  # set by subclasses

    def get_cache_key(self, request, view):
        phone = None
        try:
            phone = request.data.get('phone_number')
        except Exception:
            phone = None
        if not phone:
            return None
        # Test phones bypass throttling so QA can iterate freely.
        if phone in getattr(settings, 'TEST_PHONE_NUMBERS', {}):
            return None
        return self.cache_format % {'scope': self.scope, 'ident': str(phone)}


class OtpRequestThrottle(_PhoneNumberRateThrottle):
    """Sustained cap on /auth/otp/ — e.g. 5 OTP issues per hour per phone."""
    scope = 'otp_request'


class OtpRequestBurstThrottle(_PhoneNumberRateThrottle):
    """Anti-spam burst cap on /auth/otp/ — e.g. 1 per 30 seconds per phone."""
    scope = 'otp_request_burst'


class OtpVerifyThrottle(_PhoneNumberRateThrottle):
    """Cap on /auth/login/ — bounds OTP brute-force across multiple OTP
    issues (the per-OTP attempt counter only catches attempts against a
    single issued OTP)."""
    scope = 'otp_verify'


class _EmailRateThrottle(SimpleRateThrottle):
    """Key the throttle on the email address in the request body.

    The sibling of `_PhoneNumberRateThrottle`, for the same reason: an
    attacker can rotate source IPs, so a per-IP cap alone does not bound
    attempts against one identity.

    Unlike the phone throttles there is NO test-account bypass. A test phone
    bypass is safe because the numbers are operator-controlled and the OTP is
    fixed; a password endpoint with a bypass list would be a way to hammer
    whichever accounts are on that list.

    A request with no address is not throttled here — the view refuses it
    anyway — but it is still covered by the per-IP lockout in
    `admin_dashboard.login_guard`.
    """

    scope = ''

    def get_cache_key(self, request, view):
        try:
            email = request.data.get('email')
        except Exception:  # noqa: BLE001
            email = None
        if not email:
            return None
        return self.cache_format % {
            'scope': self.scope,
            'ident': str(email).strip().lower(),
        }


class PasswordResetThrottle(_EmailRateThrottle):
    """Cap on the reset and verification mail endpoints.

    These SEND EMAIL, so an uncapped endpoint is both an enumeration oracle
    and a way to use the platform to spam a third party's inbox.
    """

    scope = 'password_reset'


class EmailLoginThrottle(_EmailRateThrottle):
    """Sustained cap on email+password attempts per address.

    Complements `login_guard`, which is the hard lockout. This is the gentler
    outer bound, and it keeps working when the lockout fails open during a
    cache outage... with the honest caveat that DRF throttles use the same
    cache, so a total cache outage disables both. Stated rather than implied.
    """

    scope = 'email_login'
