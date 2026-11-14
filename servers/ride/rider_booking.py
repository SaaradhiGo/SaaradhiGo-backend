"""What a rider's booking needs besides the fare (rider QA fixes, 11 Nov).

  * `PHONE_REQUIRED`: a rider who signed up by email proves a mobile number before the first
    booking (Srini's ruling); the socket refuses to book until then.
  * `street_address`: "Current location" is never stored as a pickup; the point is turned into a
    street address once, through the same cache the reverse-geocode proxy uses.
  * `payment_options`: cash everywhere; UPI and card where the pickup's city allows online payment
    (ONLINE_PAYMENT_ZONES) and the country has a configured payment gateway.
  * `search_timeout_seconds`: how long dispatch looks for a driver, set per city zone.
"""

import logging

from django.conf import settings

logger = logging.getLogger(__name__)

PHONE_REQUIRED = 'PHONE_VERIFICATION_REQUIRED'
PHONE_REQUIRED_MESSAGE = 'Add and verify your mobile number before your first ride.'
GENERIC_PICKUP_LABELS = {'', 'current location', 'my location', 'your location', 'pickup'}
CASH, UPI, CARD, NETBANKING = 'cash', 'upi', 'card', 'netbanking'
ONLINE_METHODS = (UPI, CARD, NETBANKING)
#: 13 Nov: what each market's gateway takes online. India: Razorpay (UPI, card, netbanking);
#: the USA: Stripe (card).
ONLINE_METHODS_BY_COUNTRY = {'IN': (UPI, CARD, NETBANKING), 'US': (CARD,)}
DEFAULT_SEARCH_TIMEOUT_SECONDS = 180


def phone_required(user):
    from servers.auth_user.phone_identity import has_verified_phone
    return getattr(user, 'role', None) == 'rider' and not has_verified_phone(user)


def street_address(lat, lng):
    """The first reverse-geocode result's address for a point, or '' when there is none."""
    from servers.redis_client import cache_map_data, get_cached_map_data
    from servers.ride.maps_utils import google_geocode
    try:
        lat, lng = round(float(lat), 5), round(float(lng), 5)
    except (TypeError, ValueError):
        return ''
    key = f'maps:revgeo:{lat},{lng}'
    data = get_cached_map_data(key)
    if not data:
        data = google_geocode(latlng=f'{lat},{lng}')
        if data:
            cache_map_data(key, data, ttl_seconds=7 * 24 * 60 * 60)
    try:
        return str((data or {}).get('results', [])[0].get('formatted_address') or '')[:255]
    except (IndexError, AttributeError, TypeError):
        return ''


def needs_street_address(label):
    return str(label or '').strip().lower() in GENERIC_PICKUP_LABELS


def _zone_chain(zone):
    seen = 0
    while zone is not None and seen < 8:
        yield zone
        zone = zone.parent
        seen += 1


def online_payment_zone(zone):
    allowed = {c.strip().upper() for c in getattr(settings, 'ONLINE_PAYMENT_ZONES', '').split(',') if c.strip()}
    return any(z.code.upper() in allowed for z in _zone_chain(zone))


def cash_allowed_in(zone):
    """False where the market forbids cash (14 Nov; Idaho Code 49-3712: payment only through the
    app). A point outside every zone keeps today's behaviour."""
    if zone is None:
        return True
    from servers.pricing.regulation import rules_for_zone

    return bool(rules_for_zone(zone)['cash_allowed'])


def cash_allowed_at(lat, lng):
    from servers.pricing.services import find_zone_for_point

    try:
        return cash_allowed_in(find_zone_for_point(lat, lng))
    except Exception:  # noqa: BLE001 - a bad point is refused elsewhere
        return True


#: The stable code a cash booking gets where cash is not allowed.
CASH_NOT_ALLOWED_HERE = 'CASH_NOT_ALLOWED_HERE'
CASH_NOT_ALLOWED_MESSAGE = 'Cash is not accepted here. Pay in the app by card, Apple Pay or Google Pay.'


def payment_options(user, lat, lng):
    """{"methods": [...], "default": ..., "online_unavailable_reason": ''}."""
    from servers.payments.payment_gateways.factory import get_payment_gateway_for_payments
    from servers.pricing.services import find_zone_for_point
    from servers.rider.models import cash_disabled_for

    zone = find_zone_for_point(lat, lng)
    methods = []
    reason = ''
    if zone is not None and online_payment_zone(zone):
        from servers.payments.card_hold import card_available
        if get_payment_gateway_for_payments(zone.country) or card_available(zone.country):
            methods = list(ONLINE_METHODS_BY_COUNTRY.get(zone.country, (CARD,)))
        else:
            reason = 'gateway_not_configured'
    if not cash_disabled_for(user) and cash_allowed_in(zone):
        methods.append(CASH)
    default = None
    if methods:
        from servers.ride.models import Trip
        last = (Trip.objects.filter(user_id=user, payment_method__in=methods)
                .order_by('-requested_at').values_list('payment_method', flat=True).first())
        default = last or (CASH if CASH in methods else methods[0])
    return {'methods': methods, 'default': default, 'online_unavailable_reason': reason}


def search_timeout_seconds(zone):
    """The city's driver-search time (console: Zones), else the deployment default (3 min)."""
    for z in _zone_chain(zone):
        if getattr(z, 'dispatch_timeout_seconds', None):
            return int(z.dispatch_timeout_seconds)
    return int(getattr(settings, 'TRIP_ACCEPT_TIMEOUT_SECONDS', DEFAULT_SEARCH_TIMEOUT_SECONDS))


def search_timeout_for_trip(trip):
    return search_timeout_seconds(getattr(trip, 'zone', None))
