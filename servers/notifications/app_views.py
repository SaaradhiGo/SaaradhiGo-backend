"""The rider / driver app's notification preferences.

  GET /api/v1/notifications/preferences/   the caller's effective opt-in per channel x category
  PUT /api/v1/notifications/preferences/   {"preferences": [{"channel", "category", "opted_in"}]}
                                           or a single {"channel", "category", "opted_in"}

The in-app notification LIST stays where it is (`/api/v1/rider/notifications/`). The app UI that
calls this comes later (listed in the module 11 report). Consent is the person's: this is the
only place a promotional or WhatsApp opt-IN can be recorded, and it is written with source
`app` and a timestamp. In-app transactional cannot be switched off.
"""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.utils import error_response, success_response

from . import preferences
from .models import CATEGORIES, CHANNELS, NotificationPreference


def _payload(user):
    rows = NotificationPreference.objects.filter(user=user).order_by('channel', 'category')
    return {
        'effective': preferences.effective(user.pk),
        'locked': [{'channel': 'in_app', 'category': 'transactional'}],
        'rows': [{'channel': r.channel, 'category': r.category, 'opted_in': r.opted_in,
                  'source': r.source, 'updated_at': r.updated_at.isoformat()} for r in rows],
    }


@api_view(['GET', 'PUT'])
@permission_classes([IsAuthenticated])
def my_preferences(request):
    user = request.user
    if getattr(user, 'role', '') not in ('rider', 'driver'):
        return error_response(
            code='FORBIDDEN', message='Only riders and drivers have notification preferences.',
            field='role', issue='Not a rider or driver account',
            status=status.HTTP_403_FORBIDDEN)
    if request.method == 'GET':
        return success_response(_payload(user), status.HTTP_200_OK)

    data = request.data if isinstance(request.data, dict) else {}
    items = data.get('preferences')
    if items is None and 'channel' in data:
        items = [data]
    if not isinstance(items, list) or not items:
        return error_response(
            code='INVALID_FIELD', message='Send a list of preferences.', field='preferences',
            issue='preferences is required', status=status.HTTP_400_BAD_REQUEST)
    for item in items:
        if (not isinstance(item, dict) or item.get('channel') not in CHANNELS
                or item.get('category') not in CATEGORIES
                or not isinstance(item.get('opted_in'), bool)):
            return error_response(
                code='INVALID_FIELD',
                message='Each preference needs a channel, a category and opted_in true or false.',
                field='preferences', issue='Malformed preference',
                status=status.HTTP_400_BAD_REQUEST)
        if preferences.locked_on(item['channel'], item['category']) and not item['opted_in']:
            return error_response(
                code='NOT_ALLOWED',
                message='In-app transactional notifications cannot be switched off.',
                field='preferences', issue='Locked on', status=status.HTTP_400_BAD_REQUEST)
    for item in items:
        preferences.set_preference(user, item['channel'], item['category'], item['opted_in'],
                                   source='app')
    return success_response(_payload(user), status.HTTP_200_OK)
