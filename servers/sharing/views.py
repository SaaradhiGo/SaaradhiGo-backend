"""Trusted contacts, the auto-share setting, trip share links and the public trip page data.

    GET  POST   /sharing/contacts/              {name, phone}
    DELETE      /sharing/contacts/<id>/
    GET  PATCH  /sharing/settings/              {auto_share}
    GET  POST  DELETE  /sharing/trips/<trip_id>/share/   (the rider of the trip)
    GET         /sharing/public/<token>/        no sign-in; services.public_trip
    POST        /sharing/sms/inbound/           provider webhook: STOP replies
"""

import hmac

from django.conf import settings
from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.throttling import AnonRateThrottle

from base.countries import country_of_phone, normalize_phone
from base.utils import error_response, success_response

from . import services, sms
from .models import SharingPreference, TripShare, TrustedContact

NAME_MAX = 60
PRIVACY_NOTE = 'Your contacts only see this trip, and only until 30 minutes after it ends.'


def _err(code, message, field, http=status.HTTP_400_BAD_REQUEST):
    return error_response(code=code, message=message, field=field, issue=message, status=http)


def contact_json(c):
    # The rider typed this number; show it back to them in full on their own list.
    return {'id': c.id, 'name': c.name, 'phone': c.phone,
            'opted_out': sms.opted_out(c.phone)}


def _market(user):
    from servers.notifications.recipients import country_of

    return country_of(user)


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def contacts(request):
    if request.method == 'GET':
        rows = services.contacts_of(request.user)
        return success_response({'contacts': [contact_json(c) for c in rows],
                                 'max': services.MAX_CONTACTS, 'note': PRIVACY_NOTE},
                                status.HTTP_200_OK)
    name = str(request.data.get('name') or '').strip()
    if not name:
        return _err('VALIDATION_ERROR', 'Name is required.', 'name')
    if len(name) > NAME_MAX:
        return _err('VALIDATION_ERROR', f'Name must be at most {NAME_MAX} characters.', 'name')
    market = _market(request.user)
    phone = normalize_phone(request.data.get('phone'), default_country=market)
    found = country_of_phone(phone) if phone else None
    if phone is None or (market and (found is None or found.code != market)):
        return _err('VALIDATION_ERROR', 'Enter a valid mobile number for your country.', 'phone')
    if phone == request.user.phone_number:
        return _err('VALIDATION_ERROR', 'Add someone other than yourself.', 'phone')
    with transaction.atomic():
        type(request.user).objects.select_for_update().filter(pk=request.user.pk).first()
        if TrustedContact.objects.filter(rider=request.user).count() >= services.MAX_CONTACTS:
            return _err('CONTACT_LIMIT', f'You can add up to {services.MAX_CONTACTS} trusted contacts.',
                        'contacts', status.HTTP_409_CONFLICT)
        try:
            with transaction.atomic():
                contact = TrustedContact.objects.create(rider=request.user, name=name, phone=phone)
        except IntegrityError:
            return _err('CONTACT_EXISTS', 'This number is already a trusted contact.', 'phone',
                        status.HTTP_409_CONFLICT)
    first = services.first_name(request.user.full_name, 'Someone')
    sms.send(request.user, contact, 'added',
             f'{first} added you as a trusted contact on {settings.PLATFORM_BRAND_NAME}. '
             f'Reply STOP to opt out.')
    return success_response({'contact': contact_json(contact)}, status.HTTP_201_CREATED)


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def contact_detail(request, contact_id):
    deleted, _ = TrustedContact.objects.filter(pk=contact_id, rider=request.user).delete()
    if not deleted:
        return _err('NOT_FOUND', 'Contact not found.', 'contact', status.HTTP_404_NOT_FOUND)
    return success_response({'deleted': contact_id}, status.HTTP_200_OK)


@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def sharing_settings(request):
    pref, _ = SharingPreference.objects.get_or_create(user=request.user)
    if request.method == 'PATCH':
        value = request.data.get('auto_share')
        if not isinstance(value, bool):
            return _err('VALIDATION_ERROR', 'auto_share must be true or false.', 'auto_share')
        pref.auto_share = value
        pref.save(update_fields=['auto_share', 'updated_at'])
    return success_response({'auto_share': pref.auto_share,
                             'contacts': TrustedContact.objects.filter(rider=request.user).count()},
                            status.HTTP_200_OK)


@api_view(['GET', 'POST', 'DELETE'])
@permission_classes([IsAuthenticated])
def trip_share(request, trip_id):
    from servers.ride.models import Trip

    trip = Trip.objects.select_related('status_id').filter(pk=trip_id, user_id=request.user).first()
    if trip is None:
        return _err('NOT_FOUND', 'Trip not found.', 'trip_id', status.HTTP_404_NOT_FOUND)
    if request.method == 'DELETE':
        services.stop_share(trip)
        return success_response({'active': False, 'url': None}, status.HTTP_200_OK)
    if request.method == 'POST':
        if services._status(trip) in services.ENDED or services._status(trip) == 'requested':
            return _err('TRIP_NOT_ACTIVE', 'You can share a trip once a driver is on the way.',
                        'trip_id', status.HTTP_409_CONFLICT)
        share, _ = services.start_share(trip, source='manual')
        return success_response(services.share_json(share), status.HTTP_200_OK)
    share = TripShare.objects.filter(trip=trip).first()
    if share is None:
        return success_response({'active': False, 'url': None}, status.HTTP_200_OK)
    return success_response(services.share_json(share), status.HTTP_200_OK)


class PublicTripThrottle(AnonRateThrottle):
    rate = '120/min'


@api_view(['GET'])
@permission_classes([AllowAny])
@throttle_classes([PublicTripThrottle])
def public_trip(request, token):
    share = (TripShare.objects
             .select_related('trip', 'trip__status_id', 'trip__user_id', 'trip__driver_id__user_id',
                             'trip__vehicle_id', 'trip__zone')
             .filter(token=token).first())
    # An unknown token reads exactly like an ended trip: nothing to learn by guessing.
    body = services.public_trip(share) if share else {'live': False, 'message': 'This trip has ended.'}
    response = success_response(body, status.HTTP_200_OK)
    response['X-Robots-Tag'] = 'noindex, nofollow'
    response['Cache-Control'] = 'no-store'
    return response


@api_view(['POST'])
@permission_classes([AllowAny])
def sms_inbound(request):
    expected = getattr(settings, 'SHARING_SMS_INBOUND_TOKEN', '')
    given = request.headers.get('X-Sms-Webhook-Token', '')
    if not expected or not hmac.compare_digest(expected, given):
        return _err('NOT_FOUND', 'Not found.', 'general', status.HTTP_404_NOT_FOUND)
    phone = normalize_phone(request.data.get('from'))
    if phone is None:
        return _err('VALIDATION_ERROR', 'from must be a phone number.', 'from')
    stopped = sms.record_inbound(phone, request.data.get('text'))
    return success_response({'opted_out': stopped}, status.HTTP_200_OK)
