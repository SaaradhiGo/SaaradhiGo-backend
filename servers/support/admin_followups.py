"""Support follow-ups (AUDIT 27.36): the SLA dashboard, the operator inbox,
macro history and restore, and the per-country support config.

All under /support/admin/. The rules of admin_support.py hold here too:
scope through `tickets_visible_to`, 404 out of scope / 403 without the
capability, every write audited and idempotent.
"""

import logging
import statistics
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.countries import COUNTRIES
from base.utils import error_response, success_response
from servers.admin_access.bridge import ScopedPermission, granted
from servers.admin_access.models import COUNTRY, GeoNode
from servers.admin_access.scoping import ScopeNotHeld, requested_node
from servers.admin_audit.services import record_admin_action
from servers.ride.admin_trips import BadInput, _date, _iso, _staff

from . import macros as macro_lib
from .admin_support import (
    OPEN_STATUSES, PRIORITIES, TICKET_QUERYSET, _macro_dict, _macro_scope_ok,
    _scope_refused,
)
from .models import (
    OperatorNotification, SupportCountryConfig, SupportMacro, SupportMacroVersion,
    SupportTicket,
)
from .scope import ticket_may, ticket_node, ticket_place, ticket_rider, tickets_visible_to
from .sla import is_paused, sla_state

logger = logging.getLogger(__name__)

User = get_user_model()
MACRO_TARGET = 'support_macro'
INBOX_TARGET = 'operator_notification'
MAX_RANGE_DAYS = 92
MAX_TICKETS = 20000
DEFAULT_RANGE_DAYS = 7


# ---------------------------------------------------------------------------
# SLA dashboard
# ---------------------------------------------------------------------------

def _ticket_tz(ticket):
    """The ticket's own day: its trip zone's time zone, else its country's."""
    zone = ticket.trip_id.zone if ticket.trip_id_id else None
    name = (getattr(zone, 'timezone', '') or '') if zone is not None else ''
    if not name:
        country = COUNTRIES.get(ticket.country)
        name = country.default_timezone if country else timezone.get_current_timezone_name()
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - a bad zone name must not break a dashboard
        return timezone.get_current_timezone()


def _local_date(ticket):
    return ticket.created_at.astimezone(_ticket_tz(ticket)).date()


def _pct(met, total):
    return None if not total else round(100.0 * met / total, 1)


class _Bucket:
    __slots__ = ('opened', 'fr_met', 'fr_total', 'res_met', 'res_total', 'frt', 'breaching',
                 'paused')

    def __init__(self):
        self.opened = self.fr_met = self.fr_total = self.res_met = self.res_total = 0
        self.breaching = self.paused = 0
        self.frt = []

    def add_period(self, ticket, now):
        self.opened += 1
        due, done = ticket.first_response_due_at, ticket.first_responded_at
        if done is not None:
            self.fr_total += 1
            self.fr_met += 1 if (due is None or done <= due) else 0
            self.frt.append((done - ticket.created_at).total_seconds() / 60.0)
        elif due is not None and due < now:
            self.fr_total += 1
        closed = ticket.status == 'CLOSED' and ticket.resolved_at is not None
        rdue = ticket.resolution_due_at
        if closed:
            self.res_total += 1
            self.res_met += 1 if (rdue is None or ticket.resolved_at <= rdue) else 0
        elif rdue is not None and rdue < now and not is_paused(ticket) \
                and ticket.status in OPEN_STATUSES:
            self.res_total += 1

    def add_current(self, ticket, now):
        state = sla_state(ticket, now)
        if state['breached']:
            self.breaching += 1
        if state['paused']:
            self.paused += 1

    def as_dict(self):
        return {
            'opened': self.opened,
            'first_response_met_pct': _pct(self.fr_met, self.fr_total),
            'resolution_met_pct': _pct(self.res_met, self.res_total),
            'median_first_response_minutes': (
                round(statistics.median(self.frt), 1) if self.frt else None),
            'currently_breaching': self.breaching,
            'paused': self.paused,
        }


def _allowed_for_dashboard(user, tickets):
    """Tickets where the caller holds support.dashboard (cached by anchor)."""
    cache, kept = {}, []
    for ticket in tickets:
        rider = ticket_rider(ticket)
        if rider is not None:
            key = ('r', rider.id)
        else:
            node = ticket_node(ticket)
            key = ('n', node.id) if node is not None else ('x',)
        if key not in cache:
            cache[key] = ticket_may(user, 'support.dashboard', ticket)
        if cache[key]:
            kept.append(ticket)
    return kept


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('support.dashboard')])
def admin_sla_dashboard(request):
    """GET /support/admin/sla/dashboard/?date_from=&date_to=&scope=

    Per city and per priority (and per agent), for tickets OPENED in the date
    range, each cut at its own city's midnight: tickets opened, first
    response met %, resolution met %, median first-response minutes. The
    two "right now" columns (`currently_breaching`, `paused`) look at every
    open ticket in scope, not only those opened in the range. Scoped by
    `tickets_visible_to` and gated on support.dashboard at each ticket.
    """
    now = timezone.now()
    try:
        node = requested_node(request)
    except ScopeNotHeld as exc:
        return _scope_refused(exc)
    try:
        date_to = _date(request.query_params, 'date_to') or now.date()
        date_from = _date(request.query_params, 'date_from') or (
            date_to - timedelta(days=DEFAULT_RANGE_DAYS - 1))
        if date_from > date_to:
            raise BadInput('INVALID_DATE', 'date_from is after date_to.', 'date_from')
        if (date_to - date_from).days + 1 > MAX_RANGE_DAYS:
            raise BadInput('INVALID_DATE',
                           f'The range is at most {MAX_RANGE_DAYS} days.', 'date_to')
    except BadInput as exc:
        return exc.response()

    visible = tickets_visible_to(request.user, node, TICKET_QUERYSET)
    window = visible.filter(
        created_at__gte=datetime.combine(date_from - timedelta(days=1), time.min,
                                         tzinfo=ZoneInfo('UTC')),
        created_at__lt=datetime.combine(date_to + timedelta(days=2), time.min,
                                        tzinfo=ZoneInfo('UTC')))
    period = [t for t in _allowed_for_dashboard(request.user, list(window[:MAX_TICKETS + 1]))
              if date_from <= _local_date(t) <= date_to]
    truncated = len(period) > MAX_TICKETS
    period = period[:MAX_TICKETS]
    open_now = _allowed_for_dashboard(
        request.user, list(visible.filter(status__in=OPEN_STATUSES)[:MAX_TICKETS]))

    cities, city_priority, agents, total = {}, {}, {}, _Bucket()
    city_labels, agent_labels = {}, {}

    def city_key(ticket):
        place = ticket_place(ticket)
        if place is None:
            return ('', 'Unplaced', ticket.country or '')
        return (place['code'], place['name'], place.get('country') or ticket.country or '')

    def agent_key(ticket):
        who = ticket.assigned_to
        return who.id if who else None, _staff(who)

    def bucket(table, key):
        if key not in table:
            table[key] = _Bucket()
        return table[key]

    for ticket in period:
        ck = city_key(ticket)
        city_labels[ck[0]] = ck
        aid, staff = agent_key(ticket)
        agent_labels[aid] = staff
        for target in (bucket(cities, ck[0]), bucket(city_priority, (ck[0], ticket.priority)),
                       bucket(agents, aid), total):
            target.add_period(ticket, now)
    for ticket in open_now:
        ck = city_key(ticket)
        city_labels[ck[0]] = ck
        aid, staff = agent_key(ticket)
        agent_labels[aid] = staff
        for target in (bucket(cities, ck[0]), bucket(city_priority, (ck[0], ticket.priority)),
                       bucket(agents, aid), total):
            target.add_current(ticket, now)

    def city_row(code):
        _code, name, country = city_labels[code]
        return {'city': {'code': code or None, 'name': name, 'country': country or None}}

    priority_rank = {name: i for i, name in enumerate(PRIORITIES)}
    return success_response({
        'date_from': date_from.isoformat(), 'date_to': date_to.isoformat(),
        'as_of': _iso(now), 'truncated': truncated,
        'totals': total.as_dict(),
        'cities': [{**city_row(code), **cities[code].as_dict()}
                   for code in sorted(cities, key=lambda c: city_labels[c][1])],
        'rows': [{**city_row(code), 'priority': priority, **b.as_dict()}
                 for (code, priority), b in sorted(
                     city_priority.items(),
                     key=lambda kv: (city_labels[kv[0][0]][1], priority_rank.get(kv[0][1], 9)))],
        'agents': [{'agent': agent_labels[aid], 'unassigned': aid is None, **b.as_dict()}
                   for aid, b in sorted(
                       agents.items(),
                       key=lambda kv: ((agent_labels[kv[0]] or {}).get('label') or '~'))],
    }, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Operator inbox
# ---------------------------------------------------------------------------

def _inbox_row(item):
    return {
        'id': item.id, 'kind': item.kind, 'ticket_id': item.ticket_id, 'text': item.text,
        'created_at': _iso(item.created_at), 'read_at': _iso(item.read_at),
        'read': item.read_at is not None,
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('support.read')])
def admin_inbox(request):
    """GET /support/admin/inbox/  The caller's own console notifications.

    `?unread=1` leaves read ones out. `unread` is the badge number."""
    mine = OperatorNotification.objects.filter(recipient=request.user)
    unread = mine.filter(read_at__isnull=True).count()
    rows = mine.filter(read_at__isnull=True) if request.query_params.get('unread') in (
        '1', 'true') else mine
    return success_response({
        'unread': unread,
        'results': [_inbox_row(i) for i in rows.order_by('-created_at', '-id')[:100]],
    }, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('support.read')])
def admin_inbox_read(request):
    """POST /support/admin/inbox/read/  {ids: [..]} or {all: true}

    Marks the caller's own items read. A repeat changes and audits nothing."""
    mine = OperatorNotification.objects.filter(recipient=request.user, read_at__isnull=True)
    if request.data.get('all') is True:
        pass
    else:
        ids = request.data.get('ids')
        if (not isinstance(ids, list) or not ids
                or any(isinstance(i, bool) or not isinstance(i, int) for i in ids)):
            return BadInput('INVALID_IDS', 'ids must be a non-empty list of integers.',
                            'ids').response()
        mine = mine.filter(id__in=ids)
    with transaction.atomic():
        marked = list(mine.select_for_update().values_list('id', flat=True))
        if marked:
            OperatorNotification.objects.filter(id__in=marked).update(read_at=timezone.now())
            record_admin_action(
                request, action='support_inbox_read', target_type=INBOX_TARGET,
                target_id=request.user.id, after={'ids': marked},
                reason='inbox items marked read')
    unread = OperatorNotification.objects.filter(
        recipient=request.user, read_at__isnull=True).count()
    return success_response({'marked': len(marked), 'unread': unread}, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Macro versions
# ---------------------------------------------------------------------------

VERSIONED = ('title', 'body', 'locale', 'issue_types')


def write_macro_version(macro, user, *, restored_from=None):
    """Append the macro's current content as the next version. Returns it."""
    last = (SupportMacroVersion.objects.filter(macro=macro)
            .order_by('-version').values_list('version', flat=True).first()) or 0
    return SupportMacroVersion.objects.create(
        macro=macro, version=last + 1, title=macro.title, body=macro.body,
        locale=macro.locale, issue_types=list(macro.issue_types or []),
        edited_by=user, restored_from=restored_from)


def current_version(macro):
    return (SupportMacroVersion.objects.filter(macro=macro)
            .order_by('-version').values_list('version', flat=True).first()) or 0


def _version_dict(v):
    return {
        'version': v.version, 'title': v.title, 'body': v.body, 'locale': v.locale,
        'issue_types': list(v.issue_types or []), 'edited_by': _staff(v.edited_by),
        'created_at': _iso(v.created_at), 'restored_from': v.restored_from,
    }


def _macro_or_404(macro_id):
    macro = SupportMacro.objects.filter(id=macro_id).first()
    if macro is None:
        return None, error_response(code='NOT_FOUND', message='Macro not found.',
                                    field='macro_id', issue=f'Macro {macro_id}',
                                    status=status.HTTP_404_NOT_FOUND)
    return macro, None


def _macro_denied():
    return error_response(
        code='PERMISSION_DENIED', message='You do not hold support.macro for that country.',
        field='macro_id', issue='support.macro', status=status.HTTP_403_FORBIDDEN)


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('support.macro')])
def admin_macro_versions(request, macro_id):
    """GET /support/admin/macros/<id>/versions/  Newest first."""
    macro, refusal = _macro_or_404(macro_id)
    if refusal:
        return refusal
    if not _macro_scope_ok(request.user, macro.country, 'support.macro'):
        return _macro_denied()
    versions = macro.versions.select_related('edited_by').order_by('-version')
    return success_response({
        'macro': _macro_dict(macro),
        'results': [_version_dict(v) for v in versions],
    }, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('support.macro')])
def admin_macro_restore(request, macro_id):
    """POST /support/admin/macros/<id>/restore/  {version}

    Restoring is a NEW version carrying an old one's content (history is
    never rewritten), audited with before / after. Restoring what the macro
    already says changes and writes nothing."""
    try:
        wanted = request.data.get('version')
        if isinstance(wanted, bool) or not isinstance(wanted, int):
            raise BadInput('INVALID_VERSION', 'version must be an integer.', 'version')
    except BadInput as exc:
        return exc.response()
    with transaction.atomic():
        macro = SupportMacro.objects.select_for_update().filter(id=macro_id).first()
        if macro is None:
            return _macro_or_404(macro_id)[1]
        if not _macro_scope_ok(request.user, macro.country, 'support.macro'):
            return _macro_denied()
        old = macro.versions.filter(version=wanted).first()
        if old is None:
            return error_response(
                code='NOT_FOUND', message='That version does not exist.', field='version',
                issue=f'Version {wanted}', status=status.HTTP_404_NOT_FOUND)
        try:
            macro_lib.validate_body(old.body)
        except macro_lib.PlaceholderError as exc:
            return BadInput('UNKNOWN_PLACEHOLDER', str(exc), 'body').response()
        before = {**_macro_dict(macro), 'version': current_version(macro)}
        same = all(getattr(macro, f) == (list(old.issue_types or []) if f == 'issue_types'
                                         else getattr(old, f)) for f in VERSIONED)
        if same:
            return success_response({**_macro_dict(macro), 'version': before['version'],
                                     'changed': False}, status.HTTP_200_OK)
        macro.title, macro.body, macro.locale = old.title, old.body, old.locale
        macro.issue_types = list(old.issue_types or [])
        macro.save()
        new = write_macro_version(macro, request.user, restored_from=old.version)
        after = {**_macro_dict(macro), 'version': new.version}
        record_admin_action(
            request, action='support_macro_restored', target_type=MACRO_TARGET,
            target_id=macro.id, before=before, after={**after, 'restored_from': old.version},
            reason=f'restored version {old.version} as version {new.version}')
    return success_response({**after, 'changed': True}, status.HTTP_200_OK)
