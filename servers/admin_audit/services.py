"""Helpers to record admin actions in a single line at the call site.

Usage from an admin view:

    from servers.admin_audit.services import record_admin_action
    record_admin_action(
        request,
        action='kyc_approved',
        target_type='driver',
        target_id=driver.id,
        before={'approved': prior_value},
        after={'approved': True},
        reason=request.data.get('reason', ''),
    )

Failures inside the audit write must NEVER fail the surrounding business
operation: the recorder logs and swallows exceptions so an audit-log
problem can't keep an admin from approving a KYC. Loss of a log row is
preferable to refusing the action.

NOT FOR ACCESS ROWS
-------------------
Assignments, roles, scopes and the authority flags on a user are audited
by the model layer itself (`access_audit`): every write to one appends a
row whether or not anybody calls this. A view that changes one of those
should NOT also call `record_admin_action` for the same change -- it gets
two rows for one fact. It should open an `audit_context` (or rely on the
middleware, which opens one per request) and let the structural row carry
its actor, reason and, if it wants a business-level name, its action.

WHO ACTED, WHEN THERE IS NO REQUEST
-----------------------------------
A command or a service called with `request=None` used to produce a row
with no actor at all. Now the active `audit_context` is consulted: a
command that says who ran it gets that person on every row it writes.
"""

import logging

from .context import current_context
from .models import AdminAuditLog

logger = logging.getLogger(__name__)


def _client_ip(request):
    if not request:
        return None
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR') or None


def _user_agent(request):
    if not request:
        return ''
    return (request.META.get('HTTP_USER_AGENT') or '')[:512]


def _actor_label(actor):
    if not actor:
        return ''
    return (
        getattr(actor, 'full_name', None)
        or getattr(actor, 'phone_number', None)
        or getattr(actor, 'username', None)
        or str(actor)
    )[:255]


def record_admin_action(
    request,
    *,
    action,
    target_type,
    target_id,
    before=None,
    after=None,
    reason='',
):
    """Persist an audit row. Returns the row or None on failure."""
    try:
        actor = getattr(request, 'user', None) if request else None
        if actor is not None and not getattr(actor, 'is_authenticated', False):
            actor = None
        ctx = current_context()
        label = ''
        if actor is None and ctx is not None:
            actor = ctx.resolve_actor()
            label = ctx.actor_label
            if request is None:
                request = ctx.request
        return AdminAuditLog.objects.create(
            actor=actor,
            actor_label=label or _actor_label(actor),
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else '',
            before=before or {},
            after=after or {},
            reason=(reason or '')[:10000],
            ip_address=_client_ip(request),
            user_agent=_user_agent(request),
        )
    except Exception as e:
        # Don't let an audit failure break the action being audited; just
        # log loudly so we notice the audit pipeline is broken.
        logger.error(
            f"AdminAuditLog write failed (action={action} target={target_type}#{target_id}): {e}"
        )
        return None
