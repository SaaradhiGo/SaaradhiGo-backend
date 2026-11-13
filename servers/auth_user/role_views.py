"""Ride with your SaaradhiGo account (13 Nov, owner: one person, one account, two roles).

A driver who wants to book a ride uses the account they already have: same name, same mobile,
no new sign-up. This adds the `Rider` row, which is what makes the account a rider
(`ProfileSerializer.get_roles`). The web then signs the session again with both roles.
"""

import logging

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.utils import error_response, success_response

logger = logging.getLogger(__name__)


def _roles(user):
    return [r for r in ('rider', 'driver') if user.role == r or hasattr(user, r)]


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def me_ride(request):
    """Add the rider role to the caller's account. Idempotent; answers `{roles, added}`."""
    from servers.rider.models import Rider

    user = request.user
    if user.role not in ('rider', 'driver'):
        return error_response('NOT_RIDER_OR_DRIVER', 'Staff accounts cannot book rides.',
                              'role', 'invalid', status.HTTP_403_FORBIDDEN)
    from .closure import is_closing
    if is_closing(user.pk):
        return error_response('ACCOUNT_CLOSING', 'This account is being closed.',
                              'account', 'invalid', status.HTTP_409_CONFLICT)
    _, added = Rider.objects.get_or_create(user_id=user)
    if added:
        user.refresh_from_db()
        logger.info('rider role added user=%s', user.pk)
    return success_response({'roles': _roles(user), 'added': added}, status.HTTP_200_OK)
