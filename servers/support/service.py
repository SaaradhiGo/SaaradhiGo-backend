"""Writes other modules make on a ticket."""

import logging

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.utils import timezone

from .models import OperatorNotification, SupportMessage, SupportTicket

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


def post_internal_note(ticket_id, body, *, key, author=None):
    """An operator-only note on a ticket, once per `key`. Never raises."""
    scoped_key = f'internal:{ticket_id}:{key}'
    try:
        if not SupportTicket.objects.filter(pk=ticket_id).exists():
            return None
        if SupportMessage.everything.filter(idempotency_key=scoped_key).exists():
            return None
        return SupportMessage.everything.create(
            ticket_id=ticket_id, author=author, author_role='internal', body=body,
            idempotency_key=scoped_key)
    except IntegrityError:
        return None
    except Exception:  # noqa: BLE001
        logger.exception('support: internal note failed for ticket %s', ticket_id)
        return None


# ---------------------------------------------------------------------------
# A refund that was turned down goes back to the agent who asked
# ---------------------------------------------------------------------------

EXPIRY_LABEL = 'system:approval_expiry'
_OUTCOME_WORDS = {'rejected': 'rejected', 'cancelled': 'withdrawn', 'expired': 'expired'}


def return_refund_to_agent(request_row, outcome):
    """Approvals callback (registered for the refund actions).

    A refund approval linked to a ticket (`payload.ticket_id`) was rejected,
    cancelled or expired. The RIDER is told nothing: no message is written
    to the thread. Instead the ticket goes back to the agent who asked
    (assigned to the maker, IN_PROGRESS, `returned_at` / `returned_reason`),
    an INTERNAL note carries the approver's reason, the agent gets an inbox
    item and the move is audited. Runs inside the transaction that closed
    the request, so it is all there or none of it is.
    """
    from servers.admin_audit.context import audit_context
    from servers.admin_audit.services import record_admin_action

    from .sla import enter_status

    ticket_id = (request_row.payload or {}).get('ticket_id')
    if not ticket_id:
        return None
    User = get_user_model()
    ticket = SupportTicket.objects.select_for_update().filter(pk=ticket_id).first()
    if ticket is None:
        return None
    maker = User.objects.filter(pk=request_row.maker_id, is_active=True).first()
    checker = (User.objects.filter(pk=request_row.checker_id).first()
               if request_row.checker_id else None)
    word = _OUTCOME_WORDS.get(outcome, outcome)
    if outcome == 'expired':
        why = 'It expired before a checker acted.'
    else:
        why = (request_row.decision_reason or '').strip() or 'No reason was given.'
    amount = request_row.amount
    shown = f'{amount} {request_row.currency} (minor units)' if amount is not None else 'a refund'
    note = (f'Refund request #{request_row.id} for {shown} was {word}. {why} '
            f'The ticket is back with you.')
    now = timezone.now()
    before = {'status': ticket.status, 'assigned_to': ticket.assigned_to_id,
              'returned_at': ticket.returned_at.isoformat() if ticket.returned_at else None}
    key = f'internal:{ticket.id}:returned:{request_row.id}:{outcome}'
    if not SupportMessage.everything.filter(idempotency_key=key).exists():
        SupportMessage.everything.create(
            ticket=ticket, author=checker if outcome == 'rejected' else None,
            author_role='internal', body=note, idempotency_key=key)
    fields = ['returned_reason', 'returned_at', 'updated_at']
    ticket.returned_reason = why[:2000]
    ticket.returned_at = now
    fields += enter_status(ticket, 'IN_PROGRESS', now)
    fields += ['status']
    ticket.resolved_at = None
    fields.append('resolved_at')
    if maker is not None:
        ticket.assigned_to = maker
        fields.append('assigned_to')
    ticket.save(update_fields=list(dict.fromkeys(fields)))
    if maker is not None:
        OperatorNotification.objects.get_or_create(
            recipient=maker, kind=OperatorNotification.KIND_REFUND_RETURNED,
            ticket=ticket, approval_request_id=request_row.id,
            defaults={'text': f'Ticket #{ticket.id} came back: your refund request was '
                              f'{word}. {why}'})
    actor = checker if outcome == 'rejected' else None
    label = EXPIRY_LABEL if (actor is None and outcome == 'expired') else ''
    with audit_context(actor=actor, actor_label=label, reason=f'refund {word}'):
        record_admin_action(
            None, action='support_ticket_refund_returned', target_type='support_ticket',
            target_id=ticket.id, before=before,
            after={'status': ticket.status, 'assigned_to': ticket.assigned_to_id,
                   'returned_at': now.isoformat(), 'approval_request_id': request_row.id,
                   'outcome': outcome},
            reason=f'refund request #{request_row.id} {word}: {why}'[:2000])
    return ticket
