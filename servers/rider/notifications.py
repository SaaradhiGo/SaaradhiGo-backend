"""Notification dispatch with DPDP-aware opt-out checks.

The rest of the codebase still creates rows directly on the
Notification model for ride / payment / SOS events -- those are
*transactional* and never need an opt-in. But marketing and promo
notifications MUST go through `create_notification` so the user's
NotificationPreference is honoured. Same for any future
send_push_notification call that carries a marketing payload.

API:
    create_notification(user, title, message, category='ride_event', ...)

    Returns the Notification row, or None if the user has opted out of
    the given category. Callers should treat a None return as "skipped
    on purpose; do not retry."
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Categories that bypass the preference check entirely. A user cannot
# turn these off because they're functional / safety-critical.
LOCKED_ON_CATEGORIES = frozenset({'transactional', 'sos', 'payout'})


def _is_allowed(user, category: str) -> bool:
    if category in LOCKED_ON_CATEGORIES:
        return True
    from servers.rider.models import NotificationPreference
    try:
        prefs = NotificationPreference.objects.get(user_id=user)
    except NotificationPreference.DoesNotExist:
        # First-touch defaults; refer to model defaults via a fresh row
        # without saving so we don't race the get_or_create path.
        prefs = NotificationPreference(user_id=user)
    return prefs.is_enabled_for(category)


def create_notification(
    user, title: str, message: str, category: str = 'ride_event',
    trip=None, data: Optional[dict] = None,
):
    """Create a Notification row if the user has not opted out."""
    from servers.rider.models import Notification

    if not _is_allowed(user, category):
        logger.info(
            'notification suppressed (opt-out) user=%s category=%s title=%r',
            getattr(user, 'id', None), category, title,
        )
        return None

    return Notification.objects.create(
        user_id=user,
        title=title,
        message=message,
        notif_type=category if category != 'transactional' else 'ride_event',
        trip=trip,
        data=data or {},
    )


def is_marketing_allowed(user) -> bool:
    """Shortcut for promo + marketing FCM dispatchers."""
    return _is_allowed(user, 'marketing') and _is_allowed(user, 'promo')
