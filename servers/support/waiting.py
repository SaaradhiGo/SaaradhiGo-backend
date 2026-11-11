"""The hourly sweep of tickets waiting on the user.

PER-COUNTRY, NEVER DEFAULTED
----------------------------
`SupportCountryConfig` holds `waiting_reminder_days` and
`waiting_autoclose_days`. A ticket whose country has no row is skipped:
no reminder, no auto-close. Nothing here assumes a country.

WHAT IT DOES
------------
* after `waiting_reminder_days` in WAITING_USER: ONE neutral, user-visible
  system message (`reminder_sent_at` and the message's idempotency key both
  make a second run a no-op);
* after `waiting_autoclose_days`: status CLOSED, `resolved_at`, a system
  message, and an audit row whose actor is the system label below.

A safety ticket is never touched: it never pauses and never auto-closes.
The wait is measured from `waiting_since`, so a user who answered and was
asked again starts a fresh count.
"""

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from servers.admin_audit.context import audit_context
from servers.admin_audit.services import record_admin_action

from .models import SupportCountryConfig, SupportTicket
from .service import post_system_message
from .sla import enter_status

logger = logging.getLogger(__name__)

SYSTEM_LABEL = 'system:support_waiting_sweep'

REMINDER_TEXT = (
    "We're still waiting to hear from you about this request. "
    "Reply here when you can and we'll pick it back up.")
AUTOCLOSE_TEXT = (
    "We haven't heard back, so we've closed this request. "
    "If you still need help, reply here or open a new one.")


def _snapshot(ticket):
    return {'status': ticket.status, 'resolved_at': ticket.resolved_at.isoformat()
            if ticket.resolved_at else None, 'reminder_sent_at': ticket.reminder_sent_at.isoformat()
            if ticket.reminder_sent_at else None}


def _remind(ticket, now):
    key = f'reminder:{ticket.waiting_since.isoformat()}'
    with transaction.atomic():
        locked = SupportTicket.objects.select_for_update().get(pk=ticket.pk)
        if (locked.status != 'WAITING_USER' or locked.reminder_sent_at is not None
                or locked.is_safety):
            return False
        before = _snapshot(locked)
        locked.reminder_sent_at = now
        locked.save(update_fields=['reminder_sent_at', 'updated_at'])
        post_system_message(locked.id, REMINDER_TEXT, key=key)
        with audit_context(actor=None, actor_label=SYSTEM_LABEL,
                           reason='waiting-on-user reminder'):
            record_admin_action(
                None, action='support_ticket_reminded', target_type='support_ticket',
                target_id=locked.id, before=before, after=_snapshot(locked),
                reason='waiting-on-user reminder')
    return True


def _autoclose(ticket, now):
    with transaction.atomic():
        locked = SupportTicket.objects.select_for_update().get(pk=ticket.pk)
        if locked.status != 'WAITING_USER' or locked.is_safety:
            return False
        before = _snapshot(locked)
        fields = enter_status(locked, 'CLOSED', now)
        locked.resolved_at = now
        locked.save(update_fields=['status', 'resolved_at', 'updated_at', *fields])
        post_system_message(locked.id, AUTOCLOSE_TEXT, key='autoclose')
        with audit_context(actor=None, actor_label=SYSTEM_LABEL,
                           reason='waiting-on-user auto-close'):
            record_admin_action(
                None, action='support_ticket_autoclosed', target_type='support_ticket',
                target_id=locked.id, before=before, after=_snapshot(locked),
                reason='waiting-on-user auto-close')
    return True


def sweep_waiting(now=None):
    """Remind and auto-close. Idempotent. Returns {'reminded': n, 'closed': n}."""
    now = now or timezone.now()
    reminded = closed = 0
    for config in SupportCountryConfig.objects.all():
        waiting = SupportTicket.objects.filter(
            country=config.country, status='WAITING_USER',
            waiting_since__isnull=False, safety_flag=False,
        ).exclude(issue_type='safety')
        close_before = now - timedelta(days=config.waiting_autoclose_days)
        remind_before = now - timedelta(days=config.waiting_reminder_days)
        for ticket in waiting.filter(waiting_since__lte=close_before):
            try:
                closed += bool(_autoclose(ticket, now))
            except Exception:  # noqa: BLE001 - one bad row must not stop the sweep
                logger.exception('support: auto-close failed for ticket %s', ticket.pk)
        for ticket in waiting.filter(waiting_since__lte=remind_before,
                                     reminder_sent_at__isnull=True):
            try:
                reminded += bool(_remind(ticket, now))
            except Exception:  # noqa: BLE001
                logger.exception('support: reminder failed for ticket %s', ticket.pk)
    return {'reminded': reminded, 'closed': closed}
