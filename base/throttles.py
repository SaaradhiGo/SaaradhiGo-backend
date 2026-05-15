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
