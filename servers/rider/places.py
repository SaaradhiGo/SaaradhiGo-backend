"""Saved places: Home, Work and up to ten others (2026-11-09).

    GET    /rider/places/          the rider's places, Home and Work first
    POST   /rider/places/          {label, name?, address_text, latitude, longitude}
    PATCH  /rider/places/<id>/     any of those fields (rename, move the pin, new address)
    DELETE /rider/places/<id>/

The older /rider/locations/ routes stay as they were for app builds already installed.

A place outside every served zone is still saved: a rider may well set Home before the
service reaches it. Each place says whether it is inside (`in_service_area`) and carries
`notice` for the screens to show when it is not.
"""

import logging

from django.db import transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.service_area import is_inside_service_area
from base.utils import error_response, success_response

from .models import FavoritePlace
from .views import _coordinate

logger = logging.getLogger(__name__)

LABELS = ('home', 'work', 'other')
#: How many places of each label one rider may keep.
LIMITS = {'home': 1, 'work': 1, 'other': 10}
NAME_MAX = 40
ADDRESS_MAX = 512
OUTSIDE_NOTICE = 'Outside our service area for now'
_ORDER = {'home': 0, 'work': 1}


def place_json(place):
    inside = is_inside_service_area(place.latitude, place.longitude)
    return {
        'id': place.id,
        'label': place.label or 'other',
        'name': place.name or '',
        'address_text': place.address_text or '',
        'latitude': str(place.latitude),
        'longitude': str(place.longitude),
        'in_service_area': inside,
        'notice': None if inside else OUTSIDE_NOTICE,
    }


def _bad(field, issue, message=None):
    return error_response(code='VALIDATION_ERROR', message=message or f'{field.capitalize()} {issue}.',
                          field=field, issue=issue, status=status.HTTP_400_BAD_REQUEST)


def _clean(data, partial):
    """The validated fields from `data`, or an error response."""
    out = {}
    if 'label' in data or not partial:
        label = str(data.get('label') or '').strip().lower()
        if label not in LABELS:
            return None, _bad('label', 'must be home, work or other')
        out['label'] = label
    if 'name' in data:
        name = str(data.get('name') or '').strip()
        if len(name) > NAME_MAX:
            return None, _bad('name', f'must be at most {NAME_MAX} characters')
        out['name'] = name
    if 'address_text' in data or not partial:
        address = str(data.get('address_text') or '').strip()
        if not address:
            return None, _bad('address_text', 'is required', 'Address is required.')
        if len(address) > ADDRESS_MAX:
            return None, _bad('address_text', f'must be at most {ADDRESS_MAX} characters')
        out['address_text'] = address
    for field, limit in (('latitude', 90), ('longitude', 180)):
        if field in data or not partial:
            value, problem = _coordinate(data.get(field), limit)
            if problem:
                return None, _bad(field, problem)
            out[field] = value
    return out, None


def _limit_error(user, label, exclude_id=None):
    others = FavoritePlace.objects.filter(user_id=user)
    if exclude_id is not None:
        others = others.exclude(pk=exclude_id)
    if label == 'other':
        count = others.filter(label='other').count() + others.filter(label__isnull=True).count()
    else:
        count = others.filter(label=label).count()
    if count < LIMITS[label]:
        return None
    message = (f'You already have a {label.capitalize()}. Edit it instead.' if label != 'other'
               else f'You can save up to {LIMITS["other"]} other places.')
    return error_response(code='PLACE_LIMIT', message=message, field='label', issue='limit reached',
                          status=status.HTTP_409_CONFLICT)


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def places(request):
    if request.method == 'GET':
        rows = sorted(FavoritePlace.objects.filter(user_id=request.user).order_by('id'),
                      key=lambda p: _ORDER.get(p.label, 2))
        return success_response({'places': [place_json(p) for p in rows], 'limits': LIMITS},
                                status.HTTP_200_OK)

    fields, problem = _clean(request.data, partial=False)
    if problem:
        return problem
    if fields['label'] == 'other' and not fields.get('name'):
        return _bad('name', 'is required for an other place', 'Give this place a name.')
    if fields['label'] != 'other':
        fields['name'] = ''
    with transaction.atomic():
        # One rider's writes are serialised on their own row, so two quick taps cannot
        # both pass the limit check.
        type(request.user).objects.select_for_update().filter(pk=request.user.pk).first()
        refused = _limit_error(request.user, fields['label'])
        if refused:
            return refused
        place = FavoritePlace.objects.create(user_id=request.user, **fields)
    return success_response({'place': place_json(place)}, status.HTTP_201_CREATED)


@api_view(['PATCH', 'DELETE'])
@permission_classes([IsAuthenticated])
def place_detail(request, place_id):
    place = FavoritePlace.objects.filter(pk=place_id, user_id=request.user).first()
    if place is None:
        return error_response(code='NOT_FOUND', message='Saved place not found', field='place',
                              issue='not found', status=status.HTTP_404_NOT_FOUND)
    if request.method == 'DELETE':
        place.delete()
        return success_response({'deleted': place_id}, status.HTTP_200_OK)

    fields, problem = _clean(request.data, partial=True)
    if problem:
        return problem
    label = fields.get('label', place.label or 'other')
    name = fields.get('name', place.name)
    if label == 'other' and not name:
        return _bad('name', 'is required for an other place', 'Give this place a name.')
    if label != 'other':
        fields['name'] = ''
    with transaction.atomic():
        type(request.user).objects.select_for_update().filter(pk=request.user.pk).first()
        if label != (place.label or 'other'):
            refused = _limit_error(request.user, label, exclude_id=place.pk)
            if refused:
                return refused
        for key, value in fields.items():
            setattr(place, key, value)
        place.save()
    return success_response({'place': place_json(place)}, status.HTTP_200_OK)
