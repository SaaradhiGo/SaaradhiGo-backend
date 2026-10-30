"""Writes other modules make on a ticket."""

import logging

from django.db import IntegrityError

from .models import SupportMessage, SupportTicket

logger = logging.getLogger(__name__)


def post_system_message(ticket_id, body, *, key):
    """Add a 'system' message to a ticket once per `key`. Never raises:
    a missing ticket or a repeat is quietly nothing, because the money action
    that calls this must not fail over a note about itself."""
    scoped_key = f'system:{ticket_id}:{key}'
    try:
        if not SupportTicket.objects.filter(pk=ticket_id).exists():
            return None
        if SupportMessage.everything.filter(idempotency_key=scoped_key).exists():
            return None
        return SupportMessage.everything.create(
            ticket_id=ticket_id, author=None, author_role='system', body=body,
            idempotency_key=scoped_key)
    except IntegrityError:
        return None
    except Exception:  # noqa: BLE001
        logger.exception('support: system message failed for ticket %s', ticket_id)
        return None
