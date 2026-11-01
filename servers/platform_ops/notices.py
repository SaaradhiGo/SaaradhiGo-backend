"""Maintenance notices: a scoped, time-boxed message for one audience.

Scope: global, a country or a city. Audience: rider app, driver app or console. A notice shows
between `starts_at` (inclusive) and `ends_at` (exclusive) unless it was cancelled. Plain text,
at most 280 characters, severity `info` or `warning`.

NOT A PAUSE. A notice never refuses a booking. "Bookings paused" is a CITY PAUSE
(`markets.propose_status`), which has a checker; a notice has none (capability
`platform.settings`, audited), because putting up a message moves no money and stops nothing.
"""

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from servers.admin_access.access import scope_paths
from servers.admin_access.bridge import granted
from servers.admin_access.models import COUNTRY, CITY, GLOBAL, GeoNode
from servers.admin_audit.services import record_admin_action

from .errors import PlatformError
from .models import AUDIENCE_CHOICES, NOTICE_MESSAGE_MAX, SEVERITY_CHOICES, MaintenanceNotice

AUDIENCES = [a for a, _ in AUDIENCE_CHOICES]
SEVERITIES = [s for s, _ in SEVERITY_CHOICES]
AUDIT_CREATED, AUDIT_CHANGED, AUDIT_CANCELLED = (
    'maintenance_notice_created', 'maintenance_notice_changed', 'maintenance_notice_cancelled')
TARGET = 'maintenance_notice'
MAX_ACTIVE_WINDOW_DAYS = 90


def _label(user):
    return (getattr(user, 'full_name', '') or getattr(user, 'email', '') or f'staff #{user.pk}')[:255]


def notice_dict(n):
    return {
        'id': n.id, 'scope': n.scope_node.code if n.scope_node_id else None,
        'scope_name': n.scope_node.name if n.scope_node_id else 'Everywhere',
        'audience': n.audience, 'starts_at': n.starts_at.isoformat(),
        'ends_at': n.ends_at.isoformat(), 'message': n.message, 'severity': n.severity,
        'state': n.state, 'created_by': n.created_by_label or None,
        'created_at': n.created_at.isoformat(),
    }


def _clean(scope_code, audience, starts_at, ends_at, message, severity):
    if audience not in AUDIENCES:
        raise PlatformError('audience must be rider, driver or console.', code='INVALID_AUDIENCE',
                            field='audience')
    if severity not in SEVERITIES:
        raise PlatformError('severity must be info or warning.', code='INVALID_SEVERITY',
                            field='severity')
    message = str(message or '').strip()
    if not message or len(message) > NOTICE_MESSAGE_MAX:
        raise PlatformError(f'A message of 1 to {NOTICE_MESSAGE_MAX} characters is required.',
                            code='INVALID_MESSAGE', field='message')
    if '<' in message or '>' in message:
        raise PlatformError('A notice is plain text: no markup.', code='INVALID_MESSAGE',
                            field='message')
    if starts_at is None or ends_at is None or ends_at <= starts_at:
        raise PlatformError('The end must be after the start.', code='INVALID_WINDOW',
                            field='ends_at')
    if (ends_at - starts_at).days > MAX_ACTIVE_WINDOW_DAYS:
        raise PlatformError(f'A notice runs at most {MAX_ACTIVE_WINDOW_DAYS} days.',
                            code='INVALID_WINDOW', field='ends_at')
    node = None
    if scope_code:
        node = GeoNode.objects.filter(code=str(scope_code)).first()
        if node is None or node.kind not in (COUNTRY, CITY):
            raise PlatformError('A notice is global, or for a country or a city.',
                                code='INVALID_SCOPE', field='scope')
    return node, message


def _check_scope(actor, node):
    target = node or GeoNode.objects.filter(kind=GLOBAL).first()
    if not granted(actor, 'platform.settings', target)[0]:
        raise PlatformError('Posting a notice needs platform.settings here.',
                            code='SCOPE_NOT_HELD', field='scope', status=403)


def create(actor, *, scope, audience, starts_at, ends_at, message, severity='info',
           reason='', http_request=None):
    node, message = _clean(scope, audience, starts_at, ends_at, message, severity)
    _check_scope(actor, node)
    with transaction.atomic():
        row = MaintenanceNotice.objects.create(
            scope_node=node, audience=audience, starts_at=starts_at, ends_at=ends_at,
            message=message, severity=severity, created_by_id=actor.pk,
            created_by_label=_label(actor))
        record_admin_action(http_request, action=AUDIT_CREATED, target_type=TARGET,
                            target_id=row.pk, before={}, after=notice_dict(row),
                            reason=reason or 'notice posted')
    return row


def update(actor, row, *, starts_at=None, ends_at=None, message=None, severity=None,
           reason='', http_request=None):
    if row.state in ('ended', 'cancelled'):
        raise PlatformError(f'This notice is {row.state}.', code='NOT_EDITABLE', field='state',
                            status=409)
    _check_scope(actor, row.scope_node)
    node, message = _clean(row.scope_node.code if row.scope_node_id else None, row.audience,
                           starts_at or row.starts_at, ends_at or row.ends_at,
                           message if message is not None else row.message,
                           severity or row.severity)
    with transaction.atomic():
        before = notice_dict(row)
        row.starts_at, row.ends_at = starts_at or row.starts_at, ends_at or row.ends_at
        row.message, row.severity = message, severity or row.severity
        row.save()
        record_admin_action(http_request, action=AUDIT_CHANGED, target_type=TARGET,
                            target_id=row.pk, before=before, after=notice_dict(row),
                            reason=reason or 'notice changed')
    return row


def cancel(actor, row, *, reason='', http_request=None):
    if row.cancelled_at is not None:
        return row
    _check_scope(actor, row.scope_node)
    with transaction.atomic():
        before = notice_dict(row)
        row.cancelled_at = timezone.now()
        row.save()
        record_admin_action(http_request, action=AUDIT_CANCELLED, target_type=TARGET,
                            target_id=row.pk, before=before, after=notice_dict(row),
                            reason=reason or 'notice cancelled')
    return row


def _scope_q(nodes):
    """Notices that apply at these nodes: global, or at a node or any of its ancestors."""
    q = Q(scope_node__isnull=True)
    for node in nodes:
        if node is None:
            continue
        ancestors = [n for n in GeoNode.objects.filter(kind__in=[COUNTRY, CITY])
                     if node.path.startswith(n.path)]
        q |= Q(scope_node__in=ancestors)
    return q


def active_for(audience, node=None, when=None):
    """Active notices an app user in `node` (a city or country, or None) should see."""
    when = when or timezone.now()
    qs = MaintenanceNotice.objects.filter(
        audience=audience, cancelled_at__isnull=True, starts_at__lte=when, ends_at__gt=when)
    return list(qs.filter(_scope_q([node])).select_related('scope_node'))


def public_dict(n):
    return {'id': n.id, 'message': n.message, 'severity': n.severity,
            'audience': n.audience, 'scope': n.scope_node.code if n.scope_node_id else None,
            'starts_at': n.starts_at.isoformat(), 'ends_at': n.ends_at.isoformat()}


def console_banner(user, when=None):
    """Active console notices relevant to this operator: global, or covering/under their scope."""
    when = when or timezone.now()
    qs = (MaintenanceNotice.objects
          .filter(audience='console', cancelled_at__isnull=True, starts_at__lte=when,
                  ends_at__gt=when).select_related('scope_node'))
    paths = scope_paths(user)
    out = []
    for n in qs:
        if n.scope_node_id is None or paths is None:
            out.append(n)
            continue
        if any(n.scope_node.path.startswith(p) or p.startswith(n.scope_node.path)
               for p in paths):
            out.append(n)
    return [public_dict(n) for n in out]
