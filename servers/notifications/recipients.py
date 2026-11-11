"""Who a notification is for: country, city (the operator-scope anchor), contact, timezone.

The city follows the existing scope anchors (`admin_access.scoping`, `rider.scope`): a driver's
`home_city`; a rider's `home_city`, else the city of their most recent trip; else the country
node read from the phone's calling code. A recipient with no anchor at all is unplaced, and an
unplaced message is visible at Global scope only.
"""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from base.countries import COUNTRIES, country_of_phone
from servers.admin_access.models import COUNTRY, GeoNode
from servers.admin_access.masking import mask_email, mask_phone

#: Words in a message address field that are masked in a response by the field-name table.
CHANNEL_ADDRESS = {'sms': 'phone', 'whatsapp': 'phone', 'email': 'email', 'push': 'token',
                   'in_app': 'user'}


def _driver(user):
    try:
        return user.driver
    except Exception:  # noqa: BLE001 - RelatedObjectDoesNotExist
        return None


def _rider(user):
    try:
        return user.rider
    except Exception:  # noqa: BLE001
        return None


def country_of(user):
    """ISO country code of the recipient, or None."""
    driver = _driver(user)
    home = getattr(driver, 'home_city', None) if driver is not None else None
    if home is not None and home.country is not None:
        return home.country.code
    rider = _rider(user)
    home = getattr(rider, 'home_city', None) if rider is not None else None
    if home is not None and home.country is not None:
        return home.country.code
    found = country_of_phone(getattr(user, 'phone_number', ''))
    return found.code if found else None


def city_of(user):
    """The GeoNode the message is anchored at (see the module docstring), or None."""
    driver = _driver(user)
    if driver is not None and driver.home_city_id:
        return driver.home_city
    rider = _rider(user)
    if rider is not None and rider.home_city_id:
        return rider.home_city
    from servers.ride.models import Trip

    trip = (Trip.objects.filter(user_id=user.pk, zone__geo_node__isnull=False)
            .select_related('zone__geo_node').order_by('-requested_at').first())
    if trip is not None:
        return trip.zone.geo_node
    code = country_of(user)
    return GeoNode.objects.filter(kind=COUNTRY, code=code).first() if code else None


def timezone_of(user, country=None):
    """The recipient's local zone: their home city's active zone, else the country default."""
    from servers.pricing.models import ServiceZone

    node = city_of(user)
    if node is not None:
        zone = (ServiceZone.objects.filter(geo_node=node, is_active=True)
                .exclude(timezone_name='').first())
        if zone is not None:
            try:
                return ZoneInfo(zone.timezone_name)
            except ZoneInfoNotFoundError:
                pass
    market = COUNTRIES.get(country or country_of(user) or '')
    return ZoneInfo(market.default_timezone if market else 'UTC')


def push_token_of(user):
    return (getattr(user, 'fcm_token', '') or '').strip()


def address_for(user, channel):
    """The raw contact for a channel, read at send time and never stored on a message."""
    if channel in ('sms', 'whatsapp'):
        return (user.phone_number or '').strip()
    if channel == 'email':
        return (user.email or '').strip()
    if channel == 'push':
        return push_token_of(user)
    return str(user.pk)


def masked_for(user, channel):
    """What a delivery log may show: a masked phone or email, or the channel for push."""
    if channel in ('sms', 'whatsapp'):
        return mask_phone(user.phone_number or '')
    if channel == 'email':
        return mask_email(user.email) if user.email else ''
    if channel == 'push':
        return 'device token' if push_token_of(user) else ''
    return f'user {user.pk}'
