"""Opt-in and opt-out, per user x channel x category.

DEFAULTS (when the user has no row)
  transactional  push, in_app, sms, email     ON
  transactional  whatsapp                      OFF until an explicit opt-in (Meta policy)
  promotional    every channel                 OFF until opted in
In-app TRANSACTIONAL is always on and cannot be switched off by anybody; in-app promotional is
an ordinary promotional opt-in.

STOP. An SMS STOP received through a (verified) webhook opts the number out of ALL SMS,
transactional included, as the carriers do. A permanent SES bounce or a complaint opts the
address out of ALL email. Both are written by the system actor and audited.

A user's own choice always wins over a default; an admin may only opt a user OUT, never in:
consent has to come from the person.
"""

import logging

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from servers.admin_audit.context import audit_context
from servers.admin_audit.services import record_admin_action

from .models import CATEGORIES, CHANNELS, IN_APP, SMS, TRANSACTIONAL, WHATSAPP, EMAIL, NotificationPreference

logger = logging.getLogger(__name__)

DEFAULT_ON = {
    (TRANSACTIONAL, 'push'), (TRANSACTIONAL, IN_APP), (TRANSACTIONAL, SMS), (TRANSACTIONAL, EMAIL),
}


def default_for(channel, category):
    return (category, channel) in DEFAULT_ON


def locked_on(channel, category):
    return channel == IN_APP and category == TRANSACTIONAL


def is_opted_in(user_id, channel, category):
    """`(allowed, reason)`. `reason` explains a refusal for the delivery log."""
    if locked_on(channel, category):
        return True, ''
    row = NotificationPreference.objects.filter(
        user_id=user_id, channel=channel, category=category).first()
    if row is not None:
        if row.opted_in:
            return True, ''
        return False, f'opted out of {channel} {category} ({row.source})'
    if default_for(channel, category):
        return True, ''
    if channel == WHATSAPP:
        return False, 'no explicit WhatsApp opt-in'
    return False, f'not opted in to {channel} {category}'


def effective(user_id):
    """Every (channel, category) as the dispatcher would read it: {channel: {category: bool}}."""
    rows = {(p.channel, p.category): p for p in NotificationPreference.objects.filter(user_id=user_id)}
    out = {}
    for channel in CHANNELS:
        out[channel] = {}
        for category in CATEGORIES:
            row = rows.get((channel, category))
            if locked_on(channel, category):
                value = True
            elif row is not None:
                value = row.opted_in
            else:
                value = default_for(channel, category)
            out[channel][category] = value
    return out


def set_preference(user, channel, category, opted_in, source):
    """Write one preference. Returns `(row, changed)`."""
    if channel not in CHANNELS or category not in CATEGORIES:
        raise ValueError('unknown channel or category')
    if locked_on(channel, category) and not opted_in:
        raise ValueError('In-app transactional notifications cannot be switched off.')
    row, created = NotificationPreference.objects.get_or_create(
        user=user, channel=channel, category=category,
        defaults={'opted_in': opted_in, 'source': source, 'updated_at': timezone.now()})
    if created:
        return row, True
    if row.opted_in == opted_in:
        return row, False
    row.opted_in = opted_in
    row.source = source
    row.updated_at = timezone.now()
    row.save(update_fields=['opted_in', 'source', 'updated_at'])
    return row, True


def _system_audit(action, user, source, detail):
    label = f'system:{source}'
    with audit_context(actor=None, actor_label=label, reason=detail):
        record_admin_action(
            None, action=action, target_type='user_notification_preferences', target_id=user.pk,
            before={}, after={'source': source, 'detail': detail}, reason=detail)


def opt_out_channel(user, channel, source, action, detail):
    """Opt `user` out of `channel` for every category, audited as the system actor."""
    changed = False
    with transaction.atomic():
        for category in CATEGORIES:
            if locked_on(channel, category):
                continue
            _, did = set_preference(user, channel, category, False, source)
            changed = changed or did
        if changed:
            _system_audit(action, user, source, detail)
    return changed


def users_for_phone(phone):
    """Accounts whose number is `phone` (E.164, or digits with the country code)."""
    digits = ''.join(ch for ch in str(phone or '') if ch.isdigit())
    if len(digits) < 8:
        return []
    User = get_user_model()
    return list(User.objects.filter(phone_number__in=[f'+{digits}', digits]))


def users_for_message(message_row):
    User = get_user_model()
    return list(User.objects.filter(pk=message_row.recipient_user_id))
