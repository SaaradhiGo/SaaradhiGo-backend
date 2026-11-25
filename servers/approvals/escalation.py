"""25 Nov (owner): an approval request waiting over 3 days escalates to Super Admin.

One alert per request (`raise_alert` keeps one open alert per source), written to the audit log the
first time, and closed when the request is decided, sent back, cancelled or expires. The
dashboard's Needs action already lists it for Super Admin from day 3 (command_centre/workspace).
"""

from datetime import timedelta

from django.utils import timezone

from .models import ESCALATE_AFTER_HOURS, REQUESTED, ApprovalRequest

SOURCE = 'approval_request'


def sweep(now=None):
    """Raise the alert for every request waiting past the line; clear it for closed ones.
    Returns (raised, cleared)."""
    from servers.admin_audit.services import record_admin_action
    from servers.approvals.describe import describe
    from servers.command_centre.models import APPROVAL_ESCALATED, WARNING, OperatorAlert
    from servers.command_centre.services import clear_alerts, raise_alert

    from base import refs

    def plural(n, one):
        return f"{n} {one if n == 1 else one + 's'}"

    now = now or timezone.now()
    line = now - timedelta(hours=ESCALATE_AFTER_HOURS)
    raised = 0
    for row in ApprovalRequest.objects.filter(status=REQUESTED, requested_at__lt=line).select_related('scope_node'):
        first = not OperatorAlert.objects.filter(source_type=SOURCE, source_id=str(row.pk)).exists()
        days = (now - row.requested_at).days
        ref = refs.approval_ref(row)
        raise_alert(APPROVAL_ESCALATED, severity=WARNING,
                    title=f'{ref} waiting {plural(days, "day")}: {describe(row, row.action_key)}'[:200],
                    payload={'request_id': row.pk, 'ref': ref, 'days': days},
                    source_type=SOURCE, source_id=row.pk,
                    city=row.scope_node if getattr(row.scope_node, 'kind', '') == 'city' else None)
        if first:
            raised += 1
            record_admin_action(None, action='approval_escalated', target_type='approval_request',
                                target_id=str(row.pk), after={'ref': ref, 'waiting_days': days,
                                                              'escalated_to': 'Super Admin'},
                                reason=f'Waiting over {plural(ESCALATE_AFTER_HOURS // 24, "day")}')
    cleared = 0
    open_ids = OperatorAlert.objects.filter(kind=APPROVAL_ESCALATED, source_type=SOURCE,
                                            acknowledged_at__isnull=True).values_list('source_id', flat=True)
    closed = ApprovalRequest.objects.filter(pk__in=[int(i) for i in open_ids if str(i).isdigit()]).exclude(status=REQUESTED)
    for row in closed:
        cleared += clear_alerts(SOURCE, row.pk, note=f'The request is {row.get_status_display().lower()}.')
    return raised, cleared
