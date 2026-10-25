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


class GuestMapsThrottle(SimpleRateThrottle):
    """Cap place lookups by ANONYMOUS callers, keyed on client IP.

    The public homepage lets a visitor search for a pickup and a destination
    before signing in, which means `maps/geocode` and `maps/place-details`
    have to answer unauthenticated requests. Those calls cost money with the
    map provider, so an uncapped version of them is an open geocoding API
    with our billing attached.

    A SIGNED-IN RIDER IS NOT THROTTLED HERE. `get_cache_key` returns None for
    an authenticated request, which tells DRF to skip this throttle entirely,
    so the behaviour riders have today is unchanged. Only the guest path is
    bounded.

    The IP comes from `REMOTE_ADDR` via DRF's own `get_ident`, which already
    honours `NUM_PROXIES`. Behind a proxy that is the edge's view of the
    client, which is the best signal available and is good enough for a
    spend cap -- it is not an authentication control.
    """

    scope = 'guest_maps'

    def get_ident(self, request):
        """The client IP, taken as the FIRST entry of X-Forwarded-For.

        DRF's own `get_ident` joins the whole XFF chain when `NUM_PROXIES`
        is unset. Behind Railway's edge that chain carries a hop that
        changes between requests, so every call produced a different key and
        the throttle counted nothing -- measured at 65 anonymous requests in
        a row, all 200, against a 60/hour cap.

        The leftmost entry is the original client and is stable. It is also
        client-supplied and therefore spoofable, which is why this is not the
        only cap: `GuestMapsGlobalThrottle` bounds total anonymous spend
        regardless of what any one caller claims its address is.
        """
        xff = request.META.get('HTTP_X_FORWARDED_FOR')
        if xff:
            first = xff.split(',')[0].strip()
            if first:
                return first
        return request.META.get('REMOTE_ADDR', '')

    def get_cache_key(self, request, view):
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated:
            return None  # signed-in riders are not subject to this cap
        return self.cache_format % {
            'scope': self.scope,
            'ident': self.get_ident(request),
        }


class GuestMapsGlobalThrottle(SimpleRateThrottle):
    """A ceiling on ALL anonymous place lookups, across every caller.

    The per-IP cap is keyed on an address the client supplies, so a caller
    willing to rotate `X-Forwarded-For` can walk around it. This one cannot
    be walked around: every anonymous request shares a single bucket, so the
    most the public endpoints can cost in an hour is bounded no matter how
    the traffic is spread.

    Sized well above honest use -- a visitor typing two addresses makes a
    handful of calls -- and well below a bill worth worrying about. Signed-in
    riders bypass it, exactly as they bypass the per-IP cap.
    """

    scope = 'guest_maps_global'

    def get_cache_key(self, request, view):
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated:
            return None
        return self.cache_format % {'scope': self.scope, 'ident': 'all'}


class GuestFareThrottle(GuestMapsThrottle):
    """Cap fare quotes by ANONYMOUS callers, keyed on client IP.

    A rider is shown prices before being asked to sign in, so
    `/ride/estimate-fare/` has to answer unauthenticated requests. Each
    distinct trip costs one Google Distance Matrix call; the result is
    cached, so quoting four vehicle classes for the same trip costs one
    call, not four. An uncapped version is still an open distance API with
    our billing attached.

    Its own scope rather than sharing `guest_maps`, so that a visitor who
    has spent their place-search budget can still be quoted a price for the
    trip they already built, and so the two budgets can be tuned apart.

    Everything else -- the leftmost-XFF client key, and signed-in riders
    skipping the cap entirely -- is inherited.
    """

    scope = 'guest_fare'


class GuestFareGlobalThrottle(GuestMapsGlobalThrottle):
    """A ceiling on ALL anonymous fare quotes, across every caller.

    Same reasoning as the global maps ceiling: the per-IP key is an address
    the client supplies, so a caller willing to rotate it walks around the
    per-IP cap. This bucket is shared by every anonymous request and cannot
    be walked around, which is what actually bounds the bill.
    """

    scope = 'guest_fare_global'
