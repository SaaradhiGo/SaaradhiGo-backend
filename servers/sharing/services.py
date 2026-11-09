"""Trusted contacts, trip share links and what a contact may see (2026-11-09).

PRIVACY OF THE PUBLIC PAGE (/t/<token>)
    It shows the rider's FIRST name, the pickup and drop AREAS (never the first, most exact
    part of an address), the driver's first name, the vehicle and plate, the driver's live
    position, the trip status and the arrival time. It never shows a phone number, the fare
    or the rider's full name. `public_trip` is the only function that builds it, and
    tests/test_sharing.py asserts the payload holds no phone digits and no fare.
"""

import re
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import SharingPreference, TripShare, TrustedContact

MAX_CONTACTS = 5
#: A link dies this long after the trip ends.
LINGER = timedelta(minutes=30)
ENDED = ('completed', 'cancelled')


def first_name(full_name, fallback=''):
    parts = (full_name or '').strip().split()
    return parts[0] if parts else fallback


_COUNTRY_WORDS = {'india', 'united states', 'usa', 'us', 'united states of america'}


def area_of(address):
    """The landmark/area part of an address, never its most exact part.

    "Flat 302, Sai Residency, Madhapur, Hyderabad, Telangana 500081, India" -> "Madhapur, Hyderabad".
    A trailing postcode is cut, then parts holding a digit (door, plot and road numbers) and the
    country are dropped; of what is left, the area and city are kept (the two before the state,
    when three or more remain).
    """
    parts = [re.sub(r'\s*\d{5,6}(-\d{4})?$', '', p.strip()) for p in (address or '').split(',')]
    parts = [p for p in parts if p]
    parts = [p for p in parts if not re.search(r'\d', p) and p.lower() not in _COUNTRY_WORDS]
    if len(parts) >= 3:
        parts = parts[-3:-1]
    return ', '.join(parts[-2:])


def share_base_url():
    return (getattr(settings, 'TRIP_SHARE_BASE_URL', '') or settings.FRONTEND_URL).rstrip('/')


def share_url(share):
    return f'{share_base_url()}/t/{share.token}'


def _ended_at(trip):
    return trip.completed_at or trip.cancelled_at


def _status(trip):
    return getattr(trip.status_id, 'status_code', None)


def share_is_live(share, now=None):
    if share is None or not share.active:
        return False
    trip = share.trip
    if _status(trip) in ENDED:
        ended = _ended_at(trip)
        return ended is not None and (now or timezone.now()) < ended + LINGER
    return True


def expires_at(share):
    ended = _ended_at(share.trip) if _status(share.trip) in ENDED else None
    return (ended + LINGER).isoformat() if ended else None


@transaction.atomic
def start_share(trip, source='manual'):
    """The trip's live share, created or reissued. An active one is returned as it is."""
    share = TripShare.objects.select_for_update().filter(trip=trip).first()
    if share is not None and share.active:
        return share, False
    token = secrets.token_urlsafe(32)
    if share is None:
        share = TripShare.objects.create(trip=trip, token=token, source=source)
    else:
        share.token, share.active, share.source, share.stopped_at = token, True, source, None
        share.save(update_fields=['token', 'active', 'source', 'stopped_at'])
    return share, True


def stop_share(trip):
    return TripShare.objects.filter(trip=trip, active=True).update(
        active=False, stopped_at=timezone.now())


def share_json(share):
    live = share_is_live(share)
    return {'active': live, 'url': share_url(share) if live else None,
            'expires_at': expires_at(share) if live else None, 'source': share.source}


def auto_share_on(user):
    pref = SharingPreference.objects.filter(user=user).first()
    return bool(pref and pref.auto_share)


def contacts_of(user):
    return list(TrustedContact.objects.filter(rider=user))


def on_trip_started(trip_id):
    """Auto-share: the rider's setting on, and contacts to share with -> one SMS each."""
    from servers.ride.models import Trip

    from . import sms

    trip = Trip.objects.select_related('user_id', 'status_id').filter(pk=trip_id).first()
    if trip is None or not auto_share_on(trip.user_id):
        return 0
    contacts = contacts_of(trip.user_id)
    if not contacts:
        return 0
    share, _ = start_share(trip, source='auto')
    name = first_name(trip.user_id.full_name, 'Your contact')
    brand = settings.PLATFORM_BRAND_NAME
    text = f'{name} is on a {brand} trip. Follow it live: {share_url(share)}'
    return sum(1 for c in contacts if sms.send(trip.user_id, c, 'trip_share', text, trip_id=trip.id))


def on_sos(trip, user):
    """The rider pressed SOS on a trip: every trusted contact gets the link."""
    from . import sms

    if trip is None or trip.user_id_id != user.id:
        return 0
    contacts = contacts_of(user)
    if not contacts:
        return 0
    share, _ = start_share(trip, source='sos')
    name = first_name(user.full_name, 'Your contact')
    brand = settings.PLATFORM_BRAND_NAME
    text = (f'{name} pressed SOS during a {brand} trip. Track the trip: {share_url(share)}. '
            f'If you think they are in danger, call the local emergency number.')
    return sum(1 for c in contacts if sms.send(user, c, 'sos', text, trip_id=trip.id))


def public_trip(share):
    """What the public page shows. See the module docstring for what it must never show."""
    from servers.ride import eta

    if not share_is_live(share):
        return {'live': False, 'message': 'This trip has ended.'}
    trip = share.trip
    driver = trip.driver_id
    vehicle = trip.vehicle_id
    status = _status(trip)
    position = None
    est = None
    if status not in ENDED and driver is not None:
        position = eta.driver_position(driver.pk)
        est = eta.compute(trip, driver_at=position) if position else None
    return {
        'live': True,
        'status': status,
        'rider_first_name': first_name(trip.user_id.full_name, 'Your contact'),
        'pickup_area': area_of(trip.pickup_address),
        'destination_area': area_of(trip.destination_address),
        'driver_first_name': first_name(driver.user_id.full_name, 'Driver') if driver else None,
        'vehicle': ({'brand': vehicle.brand or '', 'model': vehicle.model or '',
                     'color': vehicle.color or '', 'plate': vehicle.vehicle_number or ''}
                    if vehicle else None),
        'position': {'lat': round(position[0], 5), 'lng': round(position[1], 5)} if position else None,
        'arrive_at': est['arrive_at'] if est else None,
        'eta_min': (est['drop_eta_min'] if est else None),
        'expires_at': expires_at(share),
    }
