"""The shared emergency account: loud rather than forbidden.

WHY IT EXISTS AT ALL
--------------------
Super Admins must be named individuals, so no shared mailbox can hold the
role. But an account nobody can sign into is its own outage: if the named
Super Admins are unreachable — travelling, hospitalised, or locked out by
the very incident being handled — somebody still has to be able to act.

So exactly one account is kept for that case, its credentials offline, and
it is expected never to be used. Since nobody owns it, every protection
here is about EVIDENCE rather than prevention:

  * sign-in refuses without a typed reason,
  * every Super Admin is notified at the moment it happens,
  * the reason goes into the append-only audit log,
  * and an ERROR-level log line makes it visible to alerting.

None of that stops a determined insider with the credentials. It does mean
a use cannot be quiet, which is the achievable goal.

WHAT IT DOES NOT DO
-------------------
The flag grants nothing. Authority still comes from an assignment, so the
account can be flagged before anybody decides what it may do — and it can
be flagged with NO assignment at all, in which case it can sign in and do
nothing, which is a reasonable resting state for a key under glass.
"""

import logging

from django.db import transaction

logger = logging.getLogger(__name__)

#: Shortest reason worth recording. Low enough not to obstruct somebody
#: working an incident at 3am, high enough that '.' does not pass.
MIN_REASON_LENGTH = 12


class ReasonRequired(Exception):
    """Raised when a break-glass sign-in arrives without a usable reason."""


def is_break_glass(user):
    return bool(getattr(user, 'is_break_glass', False))


def validate_reason(reason):
    """Return the cleaned reason, or raise `ReasonRequired`.

    Deliberately not a silent default. "No reason given" written by the
    system is worse than a refusal, because it looks like a record.
    """
    cleaned = (reason or '').strip()
    if len(cleaned) < MIN_REASON_LENGTH:
        raise ReasonRequired(
            f'A reason of at least {MIN_REASON_LENGTH} characters is '
            f'required to use the emergency account.'
        )
    return cleaned


def announce_use(user, reason, *, request=None, channel='api'):
    """Notify every Super Admin, log, and audit. Never raises.

    Called AFTER authentication has already succeeded, so a failure to
    deliver a notification must not turn a successful emergency sign-in
    into a failed one — the whole point of the account is that it works
    when other things do not. Delivery problems are logged and swallowed.

    Returns the number of Super Admins notified, for the caller to log.
    """
    from servers.admin_audit.services import record_admin_action

    from .services import active_super_admin_ids

    # Loud in the logs first: it is the one step that cannot fail.
    logger.error(
        'BREAK-GLASS SIGN-IN channel=%s user_id=%s email=%s reason=%r',
        channel, getattr(user, 'id', None), getattr(user, 'email', ''), reason,
    )

    notified = 0
    try:
        notified = _notify_super_admins(user, reason, channel)
    except Exception:  # noqa: BLE001 - never block the sign-in
        logger.exception('break_glass_notification_failed user_id=%s', user.id)

    try:
        record_admin_action(
            request, action='break_glass_login', target_type='operator',
            target_id=user.pk,
            after={
                'reason': reason,
                'channel': channel,
                'super_admins_notified': notified,
            },
        )
    except Exception:  # noqa: BLE001 - the ERROR log above is the fallback
        logger.exception('break_glass_audit_failed user_id=%s', user.id)

    return notified


def _notify_super_admins(user, reason, channel):
    from django.contrib.auth import get_user_model

    from servers.rider.models import Notification

    from .services import active_super_admin_ids

    recipient_ids = [i for i in active_super_admin_ids() if i != user.pk]
    if not recipient_ids:
        # Worth its own line: it means the alert reached nobody, which is
        # the state the Super Admin floor exists to prevent.
        logger.error(
            'break_glass_alert_reached_nobody: no active Super Admin to '
            'notify. Appoint named Super Admins.'
        )
        return 0

    who = getattr(user, 'email', '') or getattr(user, 'phone_number', '')
    recipients = get_user_model().objects.filter(id__in=recipient_ids)

    with transaction.atomic():
        Notification.objects.bulk_create([
            Notification(
                user_id=recipient,
                title='Emergency account used',
                message=(
                    f'The break-glass account {who} signed in via {channel}. '
                    f'Reason given: {reason}'
                ),
                notif_type='system',
                data={
                    'kind': 'break_glass_login',
                    'account': who,
                    'channel': channel,
                    'reason': reason,
                },
            )
            for recipient in recipients
        ])

    return len(recipient_ids)
