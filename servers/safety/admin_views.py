"""Safety module (operator console, module 6): the /safety/admin/ API.

  GET  incidents/                          queue + counts            safety.read | safety.respond, scoped
  POST incidents/                          open one by hand          safety.respond
  GET  incidents/<id>/                     the case file             safety.read | safety.respond
  POST incidents/<id>/assign|note|status|link|unlink|close/
  POST incidents/<id>/suspend-driver/      the EXISTING audited suspension
  POST incidents/<id>/block-rider/         the EXISTING audited block
  POST incidents/<id>/refer-police/        records the referral, no vendor
  GET  incidents/<id>/assignees/           responders who can be assigned
  POST tickets/<id>/escalate/              support.reply: ticket -> incident
  GET  signals/    POST signals/<id>/dismiss/   (GET: read | respond; dismiss: respond)
  GET  config/     POST config/<country>/  (GET: read | respond; edit: safety.config)

READ-ONLY ACCESS (owner ruling, AUDIT 27.40)
--------------------------------------------
`safety.read` ("View the Safety queue and incidents": City Ops Manager,
Safety Officer, Super Admin) opens the four GETs above, within the SAME scope
rule as `safety.respond` (out of scope is 404). Every write, the assignee
list and the SOS contact reveal (`sos.views.reveal_sos_contact`) stay
`safety.respond` only: a read-only caller gets 403 from each, the detail
payload carries `actions` all false and `allowed_actions == []`, and every
person stays masked (there is no reveal for them to ask for).

RULES THIS FILE KEEPS
---------------------
* ONE scope rule (`scope.incidents_visible_to`) for the queue, the page,
  every write, the assignee list and the escalation answer. Out of scope is
  404 and in scope without the capability at the incident is 403.
* Contact details are masked by the renderer by field name; the only real
  numbers come from the SOS reveal (active incidents only) and the generic
  reveal. Free text is masked on the way out (`mask_free_text`).
* Every write is audited with before and after (`record_admin_action`,
  target `safety_incident`) and idempotent: a repeat changes nothing and
  writes no second audit row.
* Account actions call the EXISTING services (`operator_suspend`,
  `operator_block`); nothing here touches money, fares or earnings.
"""

import logging
from datetime import datetime, time, timedelta, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Case, Count, IntegerField, Min, Q, Value, When
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.countries import COUNTRIES
from base.utils import error_response, success_response
from servers.admin_access.bridge import AnyOfScopedPermission, ScopedPermission, granted
from servers.admin_access.masking import mask_phone
from servers.admin_access.scoping import ScopeNotHeld, requested_node, scoped, scoped_or_none
from servers.admin_audit.services import record_admin_action
from servers.ride.admin_trips import BadInput, _date, _idempotency_key, _int, _iso, _text
from servers.support.text import mask_free_text

from . import config as cfg
from . import services
from .models import (
    CATEGORIES, CLOSED, LINK_DRIVER, LINK_KINDS, LINK_RIDER, LINK_SOS, LINK_TICKET, LINK_TRIP,
    LIVE_STATUSES, OUTCOMES, SEVERITIES, SEVERITY_RANK, SOURCES, SRC_MANUAL, STATUSES,
    U_NOTE, U_STATUS, SafetyCountryConfig, SafetyIncident, SafetyIncidentLink, SafetyIncidentUpdate,
    SafetySignal,
)
from .scope import (
    eligible_responder, incident_may, incidents_visible_to, signals_visible_to,
)

logger = logging.getLogger(__name__)

User = get_user_model()

TARGET = 'safety_incident'
SIGNAL_TARGET = 'safety_signal'
CAPABILITY = 'safety.respond'
READ_CAPABILITY = 'safety.read'
#: The door of the four GETs: may look. Writes keep `ScopedPermission(CAPABILITY)`.
READ_GATE = AnyOfScopedPermission(READ_CAPABILITY, CAPABILITY)

CATEGORY_CODES = {c for c, _ in CATEGORIES}
SEVERITY_CODES = {c for c, _ in SEVERITIES}
SOURCE_CODES = {c for c, _ in SOURCES}
OUTCOME_CODES = {c for c, _ in OUTCOMES}
LINK_CODES = {c for c, _ in LINK_KINDS}
MAX_NOTE = 5000

INCIDENT_QUERYSET = SafetyIncident.objects.select_related(
    'anchor_node', 'assigned_to', 'opened_by', 'trip', 'trip__zone', 'trip__zone__geo_node')


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

def _not_found(incident_id):
    return error_response(code='NOT_FOUND', message='Incident not found.', field='incident_id',
                          issue=f'No incident {incident_id}', status=status.HTTP_404_NOT_FOUND)


def _denied(incident_id):
    return error_response(
        code='PERMISSION_DENIED',
        message='You do not hold safety.respond where this incident sits.',
        field='incident_id', issue=CAPABILITY, status=status.HTTP_403_FORBIDDEN)


def _closed(incident):
    return error_response(code='INCIDENT_CLOSED', message='This incident is closed.',
                          field='status', issue=f'Incident {incident.id} is closed',
                          status=status.HTTP_409_CONFLICT)


def _conflict(code, message, field='general'):
    return error_response(code=code, message=message, field=field, issue=message,
                          status=status.HTTP_409_CONFLICT)


def _scope_refused(exc):
    return error_response(code='SCOPE_NOT_HELD', message=str(exc), field='scope', issue=str(exc),
                          status=status.HTTP_403_FORBIDDEN)


def _can_respond(user, incident):
    return incident_may(user, CAPABILITY, incident)


def _incident_for(request, incident_id, writable=False, read=False):
    """`(incident, None)` or `(None, error response)`.

    Not in scope (or no such incident) is NOT_FOUND, identically; in scope
    without `safety.respond` at the incident is 403 (with `read=True`,
    `safety.read` is enough). `writable` also refuses a closed incident.
    """
    incident = incidents_visible_to(
        request.user, None, INCIDENT_QUERYSET).filter(pk=incident_id).first()
    if incident is None:
        return None, _not_found(incident_id)
    may = _can_respond(request.user, incident) or (
        read and incident_may(request.user, READ_CAPABILITY, incident))
    if not may:
        return None, _denied(incident_id)
    if writable and incident.status == CLOSED:
        return None, _closed(incident)
    return incident, None


def _locked(incident_id):
    # A plain get(): the scope filter joins nullable columns, and Postgres
    # refuses FOR UPDATE on the nullable side of an outer join. The scope
    # answer was already given above; the window can only end in a refusal.
    return SafetyIncident.objects.select_for_update().get(pk=incident_id)


def _snapshot(incident):
    return {
        'status': incident.status, 'severity': incident.severity,
        'category': incident.category, 'assigned_to': incident.assigned_to_id,
        'outcome': incident.outcome,
    }


def _audit(request, action, incident, before, after, reason=''):
    record_admin_action(request, action=action, target_type=TARGET, target_id=incident.id,
                        before=before, after=after, reason=reason)


def _actor(user):
    if user is None:
        return None
    return {'id': user.id, 'label': services.actor_label(user)}


def _limit_offset(request):
    try:
        limit = min(max(int(request.query_params.get('limit', 50)), 1), 200)
        offset = max(int(request.query_params.get('offset', 0)), 0)
    except (TypeError, ValueError):
        limit, offset = 50, 0
    return limit, offset


# ---------------------------------------------------------------------------
# Emergency numbers and timers: from config, never a fallback
# ---------------------------------------------------------------------------

def emergency_block(country):
    config = cfg.config_for(country) if country else None
    if config is None or not config.emergency_numbers:
        return {'country': country or None, 'configured': False, 'numbers': []}
    return {'country': country, 'configured': True, 'numbers': list(config.emergency_numbers)}


def timers_for(countries):
    out = {}
    for code in sorted(countries):
        config = cfg.config_for(code)
        out[code] = {
            'configured': config is not None,
            'sos_ack_minutes': config.sos_ack_minutes if config else None,
            'incident_assign_minutes': config.incident_assign_minutes if config else None,
        }
    return out


def countries_in_scope(user):
    from servers.admin_access.access import scope_paths
    from servers.rider.scope import unrestricted

    if unrestricted(user):
        return sorted(COUNTRIES)
    paths = scope_paths(user) or []
    found = set()
    for path in paths:
        for part in path.split('/'):
            if part in COUNTRIES:
                found.add(part)
    return sorted(found)


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

ACTIVE_SOS = ('open', 'acknowledged')


def _annotate(queryset):
    active = Q(sos_events__status__in=ACTIVE_SOS)
    unack = Q(sos_events__status='open')
    return queryset.annotate(
        active_sos=Count('sos_events', filter=active, distinct=True),
        unack_sos=Count('sos_events', filter=unack, distinct=True),
        oldest_unack=Min('sos_events__created_at', filter=unack),
        oldest_active=Min('sos_events__created_at', filter=active),
        sos_first=Case(When(active_sos__gt=0, then=Value(0)), default=Value(1),
                       output_field=IntegerField()),
        sev_rank=Case(*[When(severity=s, then=Value(r)) for s, r in SEVERITY_RANK.items()],
                      default=Value(9), output_field=IntegerField()),
    )


def _place(incident):
    node = incident.anchor_node
    return {'source': incident.anchor_source,
            'node': node.code if node is not None else None,
            'node_name': node.name if node is not None else None}


def _unacknowledged_ids(incidents):
    """`{incident id: [open SOS ids]}` for a page of incidents: one query."""
    from servers.sos.models import SOSEvent

    out = {}
    rows = (SOSEvent.objects.filter(incident_id__in=[i.id for i in incidents], status='open')
            .order_by('created_at').values_list('incident_id', 'id'))
    for incident_id, sos_id in rows:
        out.setdefault(incident_id, []).append(sos_id)
    return out


def _row(incident, unacknowledged=None):
    return {
        'id': incident.id,
        'title': incident.title,
        'category': incident.category,
        'severity': incident.severity,
        'status': incident.status,
        'source': incident.source,
        'country': incident.country or None,
        'anchor': _place(incident),
        'trip_id': incident.trip_id,
        'assigned_to': incident.assigned_to_id,
        'assignee': _actor(incident.assigned_to),
        'opened_at': _iso(incident.opened_at),
        'closed_at': _iso(incident.closed_at),
        'outcome': incident.outcome or None,
        'police_referred_at': _iso(incident.police_referred_at),
        'sos': {
            'active': getattr(incident, 'active_sos', None),
            'unacknowledged': getattr(incident, 'unack_sos', None),
            'oldest_unacknowledged_at': _iso(getattr(incident, 'oldest_unack', None)),
            'oldest_active_at': _iso(getattr(incident, 'oldest_active', None)),
            'unacknowledged_ids': (unacknowledged or {}).get(incident.id, []),
        },
        'escalated': incident.assign_escalated_at is not None,
    }


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

def _csv_filter(params, name, allowed):
    raw = (params.get(name) or '').strip()
    if not raw:
        return None
    values = [v.strip() for v in raw.split(',') if v.strip()]
    bad = [v for v in values if v not in allowed]
    if bad:
        raise BadInput('INVALID_FILTER', f'{name} must be one of: {", ".join(sorted(allowed))}.', name)
    return values


def _day_bounds(day, end=False):
    # UTC: a neutral day boundary. Nothing here assumes a country's clock.
    start = datetime.combine(day + timedelta(days=1 if end else 0), time.min, tzinfo=dt_timezone.utc)
    return start


def filter_incidents(request, queryset, now):
    params = request.query_params
    statuses = _csv_filter(params, 'status', {s for s, _ in STATUSES})
    if statuses:
        queryset = queryset.filter(status__in=statuses)
    severities = _csv_filter(params, 'severity', SEVERITY_CODES)
    if severities:
        queryset = queryset.filter(severity__in=severities)
    categories = _csv_filter(params, 'category', CATEGORY_CODES)
    if categories:
        queryset = queryset.filter(category__in=categories)
    sources = _csv_filter(params, 'source', SOURCE_CODES)
    if sources:
        queryset = queryset.filter(source__in=sources)

    assigned = (params.get('assigned') or '').strip().lower()
    if assigned == 'me':
        queryset = queryset.filter(assigned_to=request.user)
    elif assigned == 'none':
        queryset = queryset.filter(assigned_to__isnull=True)
    elif assigned:
        queryset = queryset.filter(assigned_to_id=_int(assigned, 'assigned'))

    country = (params.get('country') or '').strip().upper()
    if country:
        queryset = queryset.filter(country=country)

    date_from, date_to = _date(params, 'date_from'), _date(params, 'date_to')
    if date_from:
        queryset = queryset.filter(opened_at__gte=_day_bounds(date_from))
    if date_to:
        queryset = queryset.filter(opened_at__lt=_day_bounds(date_to, end=True))

    open_sos = (params.get('has_open_sos') or '').strip().lower()
    if open_sos in ('1', 'true', 'yes'):
        queryset = queryset.filter(active_sos__gt=0)
    elif open_sos in ('0', 'false', 'no'):
        queryset = queryset.filter(active_sos=0)

    text = (params.get('q') or '').strip().lstrip('#')
    if text:
        cond = Q(title__icontains=text)
        if text.isdigit() and len(text) <= 9:
            cond |= Q(id=int(text)) | Q(trip_id=int(text))
        queryset = queryset.filter(cond)

    sort = (params.get('sort') or 'priority').strip()
    if sort == 'recent':
        return queryset.order_by('-opened_at', '-id')
    if sort == 'closed':
        return queryset.order_by('-closed_at', '-id')
    if sort != 'priority':
        raise BadInput('INVALID_SORT', 'sort must be priority, recent or closed.', 'sort')
    # SOS first, then severity, then the oldest: the next one to answer.
    return queryset.order_by('sos_first', 'sev_rank', 'opened_at', 'id')


def queue_counts(user, node):
    base = incidents_visible_to(user, node, SafetyIncident.objects.all())
    live = ~Q(status=CLOSED)
    counts = base.aggregate(
        open=Count('id', filter=Q(status='open'), distinct=True),
        investigating=Count('id', filter=Q(status='investigating'), distinct=True),
        action_taken=Count('id', filter=Q(status='action_taken'), distinct=True),
        closed=Count('id', filter=Q(status=CLOSED), distinct=True),
        mine=Count('id', filter=live & Q(assigned_to=user), distinct=True),
        unassigned=Count('id', filter=live & Q(assigned_to__isnull=True), distinct=True),
        unassigned_critical=Count(
            'id', filter=live & Q(assigned_to__isnull=True, severity='critical'), distinct=True),
    )
    sos = base.filter(sos_events__status__in=ACTIVE_SOS)
    counts['active_sos'] = sos.values('id').distinct().count()
    counts['unacknowledged_sos'] = base.filter(
        sos_events__status='open').values('id').distinct().count()
    counts['signals'] = signals_visible_to(
        user, node, SafetySignal.objects.filter(dismissed_at__isnull=True)).count()
    return counts


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated, READ_GATE])
def safety_incidents(request):
    """GET: the queue (SOS first, then severity, then the oldest) with counts.
    POST: open an incident by hand (`safety.respond` only; the door admits
    read-only callers for the GET, so the write asks again)."""
    if request.method == 'POST':
        if not granted(request.user, CAPABILITY)[0]:
            return error_response(
                code='PERMISSION_DENIED', message='You do not hold safety.respond.',
                field='general', issue=CAPABILITY, status=status.HTTP_403_FORBIDDEN)
        return _create_incident(request)
    now = timezone.now()
    try:
        node = requested_node(request)
    except ScopeNotHeld as exc:
        return _scope_refused(exc)
    try:
        queryset = filter_incidents(
            request, incidents_visible_to(request.user, node, _annotate(INCIDENT_QUERYSET)), now)
    except BadInput as exc:
        return exc.response()
    limit, offset = _limit_offset(request)
    total = queryset.count()
    rows = list(queryset[offset:offset + limit])
    countries = set(countries_in_scope(request.user)) | {r.country for r in rows if r.country}
    unack = _unacknowledged_ids(rows)
    return success_response({
        'count': total, 'limit': limit, 'offset': offset,
        'counts': queue_counts(request.user, node),
        'timers': timers_for(countries),
        'results': [_row(r, unack) for r in rows],
    }, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------

def _update(update):
    return {
        'id': update.id, 'kind': update.kind,
        'actor_id': update.actor_id, 'actor_label': update.actor_label,
        'from_status': update.from_status or None, 'to_status': update.to_status or None,
        'note': mask_free_text(update.note), 'data': update.data,
        'created_at': _iso(update.created_at),
    }


def _linked_people(user, incident):
    """The linked drivers, riders, trips and tickets, with whether the caller
    may open each one and (people) what an account action would do."""
    from servers.driver.admin_drivers import active_suspension_q
    from servers.driver.models import Driver
    from servers.ride.models import Trip
    from servers.rider.models import Rider, RiderBlock
    from servers.rider.scope import riders_visible_to
    from servers.support.models import SupportTicket
    from servers.support.scope import tickets_visible_to

    links = list(incident.links.all())
    ids = {kind: [link.target_id for link in links if link.kind == kind] for kind, _ in LINK_KINDS}

    out = {'drivers': [], 'riders': [], 'trips': [], 'tickets': []}

    drivers = list(Driver.objects.filter(id__in=ids[LINK_DRIVER])
                   .select_related('user_id', 'home_city'))
    visible = set(scoped(user, Driver.objects.filter(id__in=ids[LINK_DRIVER]))
                  .values_list('id', flat=True))
    suspended = set(Driver.objects.filter(id__in=ids[LINK_DRIVER]).filter(active_suspension_q())
                    .values_list('id', flat=True))
    for d in drivers:
        can = d.id in visible and granted(user, 'driver.write', d.home_city)[0]
        out['drivers'].append({
            'id': d.id, 'driver_name': d.user_id.full_name or '',
            'in_scope': d.id in visible, 'suspended': d.id in suspended,
            'can_suspend': bool(can) and d.id not in suspended,
            'page': {'type': 'driver', 'id': d.id},
        })

    riders = list(Rider.objects.filter(user_id_id__in=ids[LINK_RIDER]).select_related('user_id'))
    visible_riders = set(riders_visible_to(
        user, None, Rider.objects.filter(user_id_id__in=ids[LINK_RIDER]))
        .values_list('user_id_id', flat=True))
    blocked = set(RiderBlock.objects.filter(rider__user_id_id__in=ids[LINK_RIDER],
                                            lifted_at__isnull=True)
                  .values_list('rider__user_id_id', flat=True))
    from servers.rider.scope import rider_may

    for r in riders:
        uid = r.user_id_id
        can = uid in visible_riders and rider_may(user, 'rider.block', r)
        out['riders'].append({
            'id': uid, 'rider_name': r.user_id.full_name or '',
            'in_scope': uid in visible_riders, 'blocked': uid in blocked,
            'can_block': bool(can) and uid not in blocked,
            'page': {'type': 'rider', 'id': uid},
        })

    visible_trips = set(scoped(user, Trip.objects.filter(id__in=ids[LINK_TRIP]))
                        .values_list('id', flat=True))
    for t in Trip.objects.filter(id__in=ids[LINK_TRIP]).select_related('status_id'):
        out['trips'].append({
            'id': t.id, 'status': t.status_id.status_code if t.status_id else None,
            'requested_at': _iso(t.requested_at), 'in_scope': t.id in visible_trips,
            'primary': t.id == incident.trip_id, 'page': {'type': 'trip', 'id': t.id},
        })

    visible_tickets = set(tickets_visible_to(
        user, None, SupportTicket.objects.filter(id__in=ids[LINK_TICKET]))
        .values_list('id', flat=True))
    for tk in SupportTicket.objects.filter(id__in=ids[LINK_TICKET]):
        out['tickets'].append({
            'id': tk.id, 'issue_type': tk.issue_type, 'status': tk.status,
            'in_scope': tk.id in visible_tickets, 'page': {'type': 'ticket', 'id': tk.id},
        })
    return out


def _sos_rows(incident):
    return [{
        'id': e.id, 'status': e.status, 'event_type': e.event_type,
        'initiated_by': e.initiated_by, 'user_label': e.user_label,
        'user_phone': mask_phone(getattr(e.user, 'phone_number', None)),
        'created_at': _iso(e.created_at), 'escalated_at': _iso(e.escalated_at),
        'active': e.status in ACTIVE_SOS,
    } for e in incident.sos_events.select_related('user').order_by('created_at')]


def _signal_row(signal):
    return {
        'id': signal.id, 'kind': signal.kind, 'trip_id': signal.trip_id,
        'country': signal.country or None,
        'anchor': {'node': signal.anchor_node.code if signal.anchor_node_id else None,
                   'node_name': signal.anchor_node.name if signal.anchor_node_id else None},
        'measured': signal.measured, 'threshold_version': signal.threshold_version,
        'incident_id': signal.incident_id, 'created_at': _iso(signal.created_at),
        'updated_at': _iso(signal.updated_at), 'dismissed_at': _iso(signal.dismissed_at),
        'dismissed_by': signal.dismissed_by_id, 'dismiss_reason': mask_free_text(signal.dismiss_reason),
    }


def allowed_actions(user, incident, people, blockers):
    live = incident.status != CLOSED
    return {
        'assign': live, 'note': live, 'status': live, 'link': live, 'unlink': live,
        'refer_police': live, 'close': live and not blockers,
        'close_blockers': blockers,
        'suspend_driver': live and any(d['can_suspend'] for d in people['drivers']),
        'block_rider': live and any(r['can_block'] for r in people['riders']),
        'reveal_sos': live and any(s['active'] for s in _sos_rows(incident)),
        'edit_config': granted(user, 'safety.config')[0],
    }


READ_ONLY_ACTIONS = {
    'assign': False, 'note': False, 'status': False, 'link': False, 'unlink': False,
    'refer_police': False, 'close': False, 'close_blockers': [], 'suspend_driver': False,
    'block_rider': False, 'reveal_sos': False, 'edit_config': False,
}


def incident_payload(request, incident):
    annotated = _annotate(INCIDENT_QUERYSET).filter(pk=incident.pk).first() or incident
    people = _linked_people(request.user, incident)
    blockers = services.close_blockers(incident)
    read_only = not _can_respond(request.user, incident)
    if read_only:
        # Nothing an account action could do is offered to a caller who may
        # only look: the people stay (masked) but cannot be acted on.
        for d in people['drivers']:
            d['can_suspend'] = False
        for r in people['riders']:
            r['can_block'] = False
    actions = dict(READ_ONLY_ACTIONS) if read_only else allowed_actions(
        request.user, incident, people, blockers)
    return {
        'read_only': read_only,
        # The actions this caller may take now; empty when read-only.
        'allowed_actions': [k for k, v in actions.items() if v is True],
        **_row(annotated, _unacknowledged_ids([incident])),
        'closing_summary': mask_free_text(incident.closing_summary) or None,
        'police_reference': incident.police_reference or None,
        'timeline': [_update(u) for u in incident.updates.all()],
        'people': people,
        'sos_events': _sos_rows(incident),
        'signals': [_signal_row(s) for s in incident.signals.select_related('anchor_node')],
        'emergency': emergency_block(incident.country),
        'actions': actions,
        'vocabulary': {
            'categories': [c for c, _ in CATEGORIES], 'severities': [s for s, _ in SEVERITIES],
            'statuses': [s for s, _ in STATUSES], 'outcomes': [o for o, _ in OUTCOMES],
        },
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated, READ_GATE])
def safety_incident_detail(request, incident_id):
    """GET /safety/admin/incidents/<id>/  The case file (read-only for safety.read)."""
    incident, refusal = _incident_for(request, incident_id, read=True)
    if refusal:
        return refusal
    return success_response(incident_payload(request, incident), status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Manual incident
# ---------------------------------------------------------------------------

def _target(request, kind, raw):
    """Resolve a link target the CALLER may see, or raise BadInput."""
    from servers.driver.models import Driver
    from servers.ride.models import Trip
    from servers.rider.models import Rider
    from servers.rider.scope import riders_visible_to
    from servers.sos.models import SOSEvent
    from servers.support.models import SupportTicket
    from servers.support.scope import tickets_visible_to

    target_id = _int(raw, f'{kind}_id')
    user = request.user
    if kind == LINK_DRIVER:
        found = scoped_or_none(user, Driver.objects.select_related('user_id', 'home_city'),
                               id=target_id)
    elif kind == LINK_RIDER:
        found = riders_visible_to(user, None, Rider.objects.select_related('user_id')).filter(
            user_id_id=target_id).first()
    elif kind == LINK_TRIP:
        found = scoped_or_none(user, Trip.objects.select_related(
            'zone', 'zone__geo_node', 'user_id', 'driver_id'), id=target_id)
    elif kind == LINK_TICKET:
        found = tickets_visible_to(user, None, SupportTicket.objects.select_related(
            'user_id')).filter(id=target_id).first()
    elif kind == LINK_SOS:
        found = scoped_or_none(user, SOSEvent.objects.select_related(
            'user', 'trip', 'trip__zone', 'trip__zone__geo_node', 'anchor_node'), id=target_id)
    else:
        raise BadInput('INVALID_KIND', f'kind must be one of: {", ".join(sorted(LINK_CODES))}.', 'kind')
    if found is None:
        raise BadInput('TARGET_NOT_FOUND', f'No {kind} {target_id} in your scope.', f'{kind}_id')
    return found, target_id


def _create_incident(request):
    data = request.data
    try:
        category = _text(data, 'category', required=True, limit=20)
        severity = _text(data, 'severity', required=True, limit=10)
        title = _text(data, 'title', required=True, limit=200)
        note = _text(data, 'note', limit=MAX_NOTE)
        if category not in CATEGORY_CODES:
            raise BadInput('INVALID_CATEGORY', f'category must be one of {sorted(CATEGORY_CODES)}.',
                           'category')
        if severity not in SEVERITY_CODES:
            raise BadInput('INVALID_SEVERITY', f'severity must be one of {sorted(SEVERITY_CODES)}.',
                           'severity')
        trip = driver = rider = ticket = sos = None
        if data.get('trip_id'):
            trip, _ = _target(request, LINK_TRIP, data.get('trip_id'))
        if data.get('driver_id'):
            driver, _ = _target(request, LINK_DRIVER, data.get('driver_id'))
        if data.get('rider_id'):
            rider, _ = _target(request, LINK_RIDER, data.get('rider_id'))
        if data.get('ticket_id'):
            ticket, _ = _target(request, LINK_TICKET, data.get('ticket_id'))
        if data.get('sos_id'):
            sos, _ = _target(request, LINK_SOS, data.get('sos_id'))
    except BadInput as exc:
        return exc.response()

    if sos is not None and sos.incident_id:
        return _conflict('SOS_HAS_INCIDENT', f'SOS {sos.id} already belongs to incident {sos.incident_id}.',
                         'sos_id')
    with transaction.atomic():
        incident, created = services.open_incident(
            source=SRC_MANUAL, category=category, severity=severity, title=title, trip=trip,
            sos=sos, driver=driver, rider_user=rider.user_id if rider is not None else None,
            ticket=ticket, actor=request.user, note=note)
        if not created:
            services.add_update(incident, U_NOTE, actor=request.user,
                                note=note or f'Opened by hand: {title}',
                                data={'joined_by_manual': True})
        if sos is not None:
            type(sos).objects.filter(pk=sos.pk).update(incident=incident)
            services.link_added(incident, LINK_SOS, sos.id, request.user)
        if created:
            _audit(request, 'safety_incident_opened', incident, {},
                   {**_snapshot(incident), 'source': SRC_MANUAL, 'trip': incident.trip_id},
                   reason=title)
    incident = _incident_for(request, incident.id)[0] or incident
    return success_response(
        {**_row(incident), 'created': created},
        status.HTTP_201_CREATED if created else status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def _assignee_target(data):
    if data.get('me') is True:
        return 'me'
    if data.get('none') is True:
        return 'none'
    value = data.get('assigned_to')
    if value in (None, '') or str(value).strip().lower() == 'none':
        if 'assigned_to' in data:
            return 'none'
        raise BadInput('MISSING_FIELDS', 'assigned_to is required.', 'assigned_to')
    if str(value).strip().lower() == 'me':
        return 'me'
    return _int(value, 'assigned_to')


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission(CAPABILITY)])
def safety_assign(request, incident_id):
    """POST incidents/<id>/assign/  {assigned_to: <id> | "me" | "none"}

    Only an operator who holds safety.respond where the incident sits may be
    assigned."""
    incident, refusal = _incident_for(request, incident_id, writable=True)
    if refusal:
        return refusal
    try:
        target = _assignee_target(request.data)
    except BadInput as exc:
        return exc.response()
    if target == 'none':
        assignee = None
    else:
        assignee = request.user if target == 'me' else User.objects.filter(id=target).first()
        if not eligible_responder(assignee, incident):
            return error_response(
                code='ASSIGNEE_NOT_ELIGIBLE',
                message='That person cannot be assigned: they must be an active operator '
                        'who can respond to safety incidents where this one sits.',
                field='assigned_to', issue=f'User {target}', status=status.HTTP_400_BAD_REQUEST)
    with transaction.atomic():
        locked = _locked(incident.id)
        if locked.status == CLOSED:
            return _closed(locked)
        if locked.assigned_to_id == (assignee.id if assignee else None):
            return success_response({'incident_id': locked.id, 'changed': False,
                                     **_snapshot(locked)}, status.HTTP_200_OK)
        before = _snapshot(locked)
        services.assign(locked, assignee, actor=request.user)
        _audit(request, 'safety_incident_assigned', locked, before, _snapshot(locked))
    return success_response({'incident_id': locked.id, 'changed': True, **_snapshot(locked)},
                            status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission(CAPABILITY)])
def safety_assignees(request, incident_id):
    """GET incidents/<id>/assignees/  Operators who may be assigned."""
    incident, refusal = _incident_for(request, incident_id)
    if refusal:
        return refusal
    people = []
    for candidate in User.objects.filter(is_active=True, is_staff=True).order_by('id')[:500]:
        if eligible_responder(candidate, incident):
            people.append({'id': candidate.id, 'label': services.actor_label(candidate),
                           'me': candidate.id == request.user.id,
                           'assigned': candidate.id == incident.assigned_to_id})
    return success_response({'incident_id': incident.id, 'results': people}, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission(CAPABILITY)])
def safety_note(request, incident_id):
    """POST incidents/<id>/note/  {body, idempotency_key?}"""
    incident, refusal = _incident_for(request, incident_id, writable=True)
    if refusal:
        return refusal
    try:
        body = _text(request.data, 'body', required=True, limit=MAX_NOTE)
        key = _idempotency_key(request, required=False)
    except BadInput as exc:
        return exc.response()
    if key:
        existing = SafetyIncidentUpdate.objects.filter(incident=incident, idempotency_key=key).first()
        if existing is not None:
            return success_response({**_update(existing), 'created': False}, status.HTTP_200_OK)
    try:
        with transaction.atomic():
            update = services.add_update(incident, U_NOTE, actor=request.user, note=body,
                                         idempotency_key=key)
            _audit(request, 'safety_incident_noted', incident, {},
                   {'update_id': update.id, 'body_chars': len(body)}, reason='note')
    except IntegrityError:
        existing = SafetyIncidentUpdate.objects.filter(incident=incident, idempotency_key=key).first()
        if existing is None:
            raise
        return success_response({**_update(existing), 'created': False}, status.HTTP_200_OK)
    return success_response({**_update(update), 'created': True}, status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission(CAPABILITY)])
def safety_set_status(request, incident_id):
    """POST incidents/<id>/status/  {status, note?}  Closing has its own endpoint."""
    incident, refusal = _incident_for(request, incident_id, writable=True)
    if refusal:
        return refusal
    try:
        wanted = _text(request.data, 'status', required=True, limit=14)
        note = _text(request.data, 'note', limit=MAX_NOTE)
    except BadInput as exc:
        return exc.response()
    if wanted == CLOSED:
        return error_response(code='USE_CLOSE', message='Close an incident with a summary and an outcome.',
                              field='status', issue='close endpoint', status=status.HTTP_400_BAD_REQUEST)
    if wanted not in LIVE_STATUSES:
        return error_response(code='INVALID_STATUS',
                              message=f'status must be one of: {", ".join(LIVE_STATUSES)}.',
                              field='status', issue=f'Got {wanted!r}', status=status.HTTP_400_BAD_REQUEST)
    with transaction.atomic():
        locked = _locked(incident.id)
        if locked.status == CLOSED:
            return _closed(locked)
        if locked.status == wanted:
            return success_response({'incident_id': locked.id, 'changed': False,
                                     **_snapshot(locked)}, status.HTTP_200_OK)
        before = _snapshot(locked)
        previous = locked.status
        locked.status = wanted
        locked.save(update_fields=['status', 'updated_at'])
        services.add_update(locked, U_STATUS, actor=request.user, note=note,
                            from_status=previous, to_status=wanted)
        _audit(request, 'safety_incident_status_changed', locked, before, _snapshot(locked), reason=note)
    return success_response({'incident_id': locked.id, 'changed': True, **_snapshot(locked)},
                            status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission(CAPABILITY)])
def safety_link(request, incident_id):
    """POST incidents/<id>/link/  {kind, id}  A target the caller can see."""
    incident, refusal = _incident_for(request, incident_id, writable=True)
    if refusal:
        return refusal
    try:
        kind = _text(request.data, 'kind', required=True, limit=8)
        _found, target_id = _target(request, kind, request.data.get('id'))
    except BadInput as exc:
        return exc.response()
    if kind == LINK_SOS and _found.incident_id and _found.incident_id != incident.id:
        return _conflict('SOS_HAS_INCIDENT',
                         f'SOS {target_id} already belongs to incident {_found.incident_id}.', 'id')
    with transaction.atomic():
        locked = _locked(incident.id)
        if locked.status == CLOSED:
            return _closed(locked)
        link, created = services.link_added(locked, kind, target_id, request.user)
        if kind == LINK_SOS:
            type(_found).objects.filter(pk=_found.pk).update(incident=locked)
        if created:
            _audit(request, 'safety_incident_linked', locked, {},
                   {'kind': kind, 'target_id': target_id}, reason='link')
    return success_response({'incident_id': incident.id, 'kind': kind, 'id': target_id,
                             'changed': created}, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission(CAPABILITY)])
def safety_unlink(request, incident_id):
    """POST incidents/<id>/unlink/  {kind, id}  An SOS and the primary trip stay."""
    incident, refusal = _incident_for(request, incident_id, writable=True)
    if refusal:
        return refusal
    try:
        kind = _text(request.data, 'kind', required=True, limit=8)
        target_id = _int(request.data.get('id'), 'id')
        if kind not in LINK_CODES:
            raise BadInput('INVALID_KIND', 'Unknown kind.', 'kind')
    except BadInput as exc:
        return exc.response()
    allowed, why = services.unlink_allowed(kind, incident, target_id)
    if not allowed:
        return _conflict('UNLINK_REFUSED', why, 'kind')
    with transaction.atomic():
        locked = _locked(incident.id)
        if locked.status == CLOSED:
            return _closed(locked)
        removed = services.link_removed(locked, kind, target_id, request.user)
        if removed:
            _audit(request, 'safety_incident_unlinked', locked, {'kind': kind, 'target_id': target_id},
                   {}, reason='unlink')
    return success_response({'incident_id': incident.id, 'kind': kind, 'id': target_id,
                             'changed': removed}, status.HTTP_200_OK)


# --- account actions: the existing audited services ------------------------

def _linked_ids(incident, kind):
    return set(incident.links.filter(kind=kind).values_list('target_id', flat=True))


def _after_action(request, incident, summary, action_name, data):
    """Timeline + audit for an account action taken from an incident."""
    with transaction.atomic():
        locked = _locked(incident.id)
        before = _snapshot(locked)
        services.add_update(locked, 'action', actor=request.user, note=summary,
                            data={'action': action_name, **data})
        if locked.status in ('open', 'investigating'):
            previous = locked.status
            locked.status = 'action_taken'
            locked.save(update_fields=['status', 'updated_at'])
            services.add_update(locked, U_STATUS, actor=request.user, from_status=previous,
                                to_status='action_taken', note=summary)
        _audit(request, f'safety_incident_{action_name}', locked, before, _snapshot(locked),
               reason=summary)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission(CAPABILITY)])
def safety_suspend_driver(request, incident_id):
    """POST incidents/<id>/suspend-driver/  {driver_id, reason_code, note}

    Suspends a driver LINKED to this incident through the existing audited
    suspension (`driver.write` at the driver's city)."""
    from servers.driver.admin_drivers import (
        _driver_for_write, _suspend_params, operator_suspend,
    )

    incident, refusal = _incident_for(request, incident_id, writable=True)
    if refusal:
        return refusal
    try:
        driver_id = _int(request.data.get('driver_id'), 'driver_id')
        reason_code, note = _suspend_params(request.data)
    except BadInput as exc:
        return exc.response()
    if driver_id not in _linked_ids(incident, LINK_DRIVER):
        return _conflict('NOT_LINKED', 'That driver is not linked to this incident.', 'driver_id')
    driver, refusal = _driver_for_write(request, driver_id, 'driver.write')
    if refusal:
        return refusal
    outcome, row = operator_suspend(driver.id, reason_code=reason_code, note=note,
                                    actor=request.user, http_request=request)
    if outcome == 'suspended':
        _after_action(request, incident, f'Driver {driver.id} suspended ({reason_code})',
                      'driver_suspended', {'driver_id': driver.id, 'suspension_id': row.id,
                                           'reason_code': reason_code})
    return success_response(
        {'incident_id': incident.id, 'driver_id': driver.id, 'suspension_id': row.id,
         'reason_code': row.reason_code, 'created': outcome == 'suspended'},
        status.HTTP_201_CREATED if outcome == 'suspended' else status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission(CAPABILITY)])
def safety_block_rider(request, incident_id):
    """POST incidents/<id>/block-rider/  {rider_id, reason_code, note}

    Blocks a rider LINKED to this incident through the existing audited block
    (`rider.block` at the rider's anchors). A blocked rider can still raise an
    SOS: the block stops booking and nothing else."""
    from servers.rider.admin_riders import _block_params, _rider_for, operator_block

    incident, refusal = _incident_for(request, incident_id, writable=True)
    if refusal:
        return refusal
    try:
        rider_id = _int(request.data.get('rider_id'), 'rider_id')
        reason_code, note = _block_params(request.data)
    except BadInput as exc:
        return exc.response()
    if rider_id not in _linked_ids(incident, LINK_RIDER):
        return _conflict('NOT_LINKED', 'That rider is not linked to this incident.', 'rider_id')
    rider, _node, refusal = _rider_for(request, rider_id, 'rider.block')
    if refusal:
        return refusal
    outcome, row = operator_block(rider.id, reason_code=reason_code, note=note,
                                  actor=request.user, http_request=request)
    if outcome == 'blocked':
        _after_action(request, incident, f'Rider {rider_id} blocked ({reason_code})',
                      'rider_blocked', {'rider_id': rider_id, 'block_id': row.id,
                                        'reason_code': reason_code})
    return success_response(
        {'incident_id': incident.id, 'rider_id': rider_id, 'block_id': row.id,
         'reason_code': row.reason_code, 'created': outcome == 'blocked'},
        status.HTTP_201_CREATED if outcome == 'blocked' else status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission(CAPABILITY)])
def safety_refer_police(request, incident_id):
    """POST incidents/<id>/refer-police/  {reference?, note?}

    Records that the operator referred the incident to the police: a timeline
    entry and an audit row, through the no-vendor provider. Shows the
    incident country's numbers; a country with none configured says so."""
    incident, refusal = _incident_for(request, incident_id, writable=True)
    if refusal:
        return refusal
    try:
        reference = _text(request.data, 'reference', limit=120)
        note = _text(request.data, 'note', limit=MAX_NOTE)
    except BadInput as exc:
        return exc.response()
    with transaction.atomic():
        locked = _locked(incident.id)
        if locked.status == CLOSED:
            return _closed(locked)
        if locked.police_referred_at is not None:
            return success_response({'incident_id': locked.id, 'changed': False,
                                     'police_referred_at': _iso(locked.police_referred_at),
                                     'emergency': emergency_block(locked.country)},
                                    status.HTTP_200_OK)
        before = _snapshot(locked)
        services.refer_to_police(locked, actor=request.user, reference=reference, note=note)
        _audit(request, 'safety_incident_police_referral', locked, before,
               {**_snapshot(locked), 'outcome_suggested': 'police_referral',
                'reference': reference}, reason=note or 'referred to police')
    return success_response({'incident_id': locked.id, 'changed': True,
                             'police_referred_at': _iso(locked.police_referred_at),
                             'emergency': emergency_block(locked.country)}, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission(CAPABILITY)])
def safety_close(request, incident_id):
    """POST incidents/<id>/close/  {summary, outcome}  Both required.

    Refused while a linked SOS is still active: the alarm is resolved or
    marked false first, so closing the case can never hide a live one."""
    incident, refusal = _incident_for(request, incident_id)
    if refusal:
        return refusal
    try:
        summary = _text(request.data, 'summary', required=True, limit=MAX_NOTE)
        outcome = _text(request.data, 'outcome', required=True, limit=20)
        if outcome not in OUTCOME_CODES:
            raise BadInput('INVALID_OUTCOME', f'outcome must be one of {sorted(OUTCOME_CODES)}.',
                           'outcome')
    except BadInput as exc:
        return exc.response()
    with transaction.atomic():
        locked = _locked(incident.id)
        if locked.status == CLOSED:
            if locked.closing_summary == summary and locked.outcome == outcome:
                return success_response({'incident_id': locked.id, 'changed': False,
                                         **_snapshot(locked)}, status.HTTP_200_OK)
            return _closed(locked)
        blockers = services.close_blockers(locked)
        if blockers:
            return error_response(
                code=blockers[0]['code'], message=blockers[0]['message'], field='status',
                issue=str(blockers[0]['sos_ids']), status=status.HTTP_409_CONFLICT)
        before = _snapshot(locked)
        services.close_incident(locked, actor=request.user, summary=summary, outcome=outcome)
        _audit(request, 'safety_incident_closed', locked, before,
               {**_snapshot(locked), 'closed_at': _iso(locked.closed_at)}, reason=summary)
    return success_response({'incident_id': locked.id, 'changed': True, **_snapshot(locked)},
                            status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Ticket escalation (support.reply holders)
# ---------------------------------------------------------------------------

@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('support.reply')])
def safety_escalate_ticket(request, ticket_id):
    """POST tickets/<id>/escalate/  {note?}  A safety ticket becomes an incident.

    Gated on support.reply (a support agent escalates; safety.respond holders
    work the incident). The ticket must be in the caller's scope under the one
    ticket rule and be a safety concern. Idempotent: a ticket already linked to
    an open incident answers with it."""
    from servers.support.models import SupportMessage, SupportTicket
    from servers.support.scope import ticket_may, tickets_visible_to

    ticket = tickets_visible_to(request.user, None, SupportTicket.objects.select_related(
        'user_id', 'trip_id', 'trip_id__zone', 'trip_id__zone__geo_node')).filter(
        pk=ticket_id).first()
    if ticket is None:
        return error_response(code='NOT_FOUND', message='Ticket not found.', field='ticket_id',
                              issue=f'No ticket {ticket_id}', status=status.HTTP_404_NOT_FOUND)
    if not ticket_may(request.user, 'support.reply', ticket):
        return error_response(code='PERMISSION_DENIED',
                              message="You do not hold support.reply for this ticket's place.",
                              field='ticket_id', issue='support.reply',
                              status=status.HTTP_403_FORBIDDEN)
    if not ticket.is_safety:
        return _conflict('NOT_A_SAFETY_TICKET', 'Only a safety concern can be escalated.', 'ticket_id')
    try:
        note = _text(request.data, 'note', limit=MAX_NOTE)
    except BadInput as exc:
        return exc.response()

    already = SafetyIncidentLink.objects.filter(
        kind=LINK_TICKET, target_id=ticket.id).exclude(incident__status=CLOSED).first()
    if already is not None:
        incident, created = already.incident, False
    else:
        with transaction.atomic():
            incident, created = services.open_from_ticket(
                ticket, actor=request.user, note=note or f'Escalated from support ticket {ticket.id}')
            if created:
                _audit(request, 'safety_incident_opened', incident, {},
                       {**_snapshot(incident), 'source': 'support_ticket', 'ticket_id': ticket.id},
                       reason=note or 'escalated from a support ticket')
            SupportMessage.everything.create(
                ticket=ticket, author=request.user, author_role='internal',
                body=f'Escalated to safety incident #{incident.id}.')
            record_admin_action(
                request, action='support_ticket_escalated_to_safety', target_type='support_ticket',
                target_id=ticket.id, after={'incident_id': incident.id, 'created': created},
                reason=note or 'escalated to safety')
    return success_response({
        'ticket_id': ticket.id, 'incident_id': incident.id, 'created': created,
        'can_open': incident_may(request.user, CAPABILITY, incident),
    }, status.HTTP_201_CREATED if created else status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([IsAuthenticated, READ_GATE])
def safety_signals(request):
    """GET signals/?state=open|dismissed|all&kind=&country=  The review queue."""
    try:
        node = requested_node(request)
    except ScopeNotHeld as exc:
        return _scope_refused(exc)
    state = (request.query_params.get('state') or 'open').strip()
    if state not in ('open', 'dismissed', 'all'):
        return error_response(code='INVALID_FILTER', message='state must be open, dismissed or all.',
                              field='state', issue=state, status=status.HTTP_400_BAD_REQUEST)
    queryset = signals_visible_to(request.user, node, SafetySignal.objects.select_related(
        'anchor_node'))
    if state == 'open':
        queryset = queryset.filter(dismissed_at__isnull=True)
    elif state == 'dismissed':
        queryset = queryset.filter(dismissed_at__isnull=False)
    kind = (request.query_params.get('kind') or '').strip()
    if kind:
        queryset = queryset.filter(kind=kind)
    country = (request.query_params.get('country') or '').strip().upper()
    if country:
        queryset = queryset.filter(country=country)
    limit, offset = _limit_offset(request)
    total = queryset.count()
    return success_response({
        'count': total, 'limit': limit, 'offset': offset,
        'results': [_signal_row(s) for s in queryset.order_by('-updated_at', '-id')[offset:offset + limit]],
    }, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission(CAPABILITY)])
def safety_dismiss_signal(request, signal_id):
    """POST signals/<id>/dismiss/  {reason}  Required. Idempotent."""
    signal = signals_visible_to(request.user, None, SafetySignal.objects.select_related(
        'anchor_node')).filter(pk=signal_id).first()
    if signal is None:
        return error_response(code='NOT_FOUND', message='Signal not found.', field='signal_id',
                              issue=f'No signal {signal_id}', status=status.HTTP_404_NOT_FOUND)
    if not granted(request.user, CAPABILITY, signal.anchor_node)[0]:
        return error_response(code='PERMISSION_DENIED', message='You do not hold safety.respond here.',
                              field='signal_id', issue=CAPABILITY, status=status.HTTP_403_FORBIDDEN)
    try:
        reason = _text(request.data, 'reason', required=True, limit=2000)
    except BadInput as exc:
        return exc.response()
    with transaction.atomic():
        locked = SafetySignal.objects.select_for_update().get(pk=signal.pk)
        if locked.dismissed_at is not None:
            return success_response({'signal_id': locked.id, 'changed': False}, status.HTTP_200_OK)
        locked.dismissed_at, locked.dismissed_by, locked.dismiss_reason = (
            timezone.now(), request.user, reason)
        locked.save(update_fields=['dismissed_at', 'dismissed_by', 'dismiss_reason'])
        record_admin_action(
            request, action='safety_signal_dismissed', target_type=SIGNAL_TARGET,
            target_id=locked.id, before={'dismissed': False},
            after={'dismissed': True, 'kind': locked.kind, 'trip_id': locked.trip_id}, reason=reason)
        if locked.incident_id:
            services.add_update(locked.incident, U_NOTE, actor=request.user,
                                note=f'{locked.get_kind_display()} signal dismissed: {reason}',
                                data={'signal_id': locked.id})
    return success_response({'signal_id': locked.id, 'changed': True}, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _config_row(code, user):
    active = cfg.config_for(code)
    return {
        'country': code, 'name': COUNTRIES[code].name,
        'configured': active is not None,
        'active': active.as_dict() if active is not None else None,
        'registry_numbers': cfg.registry_numbers(code),
        'history': [c.as_dict() for c in cfg.history(code, limit=10)],
    }


@api_view(['GET'])
@permission_classes([IsAuthenticated, READ_GATE])
def safety_config(request):
    """GET config/  Per-country timers, thresholds and numbers, with history.

    Readable by safety.read and safety.respond; `can_edit` says whether the caller holds
    safety.config. A country with no config says so: there is no default."""
    return success_response({
        'can_edit': granted(request.user, 'safety.config')[0],
        'fields': list(SafetyCountryConfig.NUMBER_FIELDS),
        'limits': {k: list(v) for k, v in cfg.LIMITS.items()},
        'results': [_config_row(code, request.user) for code in countries_in_scope(request.user)],
    }, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('safety.config')])
def safety_config_update(request, country):
    """POST config/<country>/  {<field>..., emergency_numbers?, reason}

    A new VERSION of the country's config; fields left out keep their value,
    an explicit null turns that rule off. Super Admin by default."""
    code = (country or '').upper()
    if code not in COUNTRIES:
        return error_response(code='NOT_FOUND', message='Unknown country.', field='country',
                              issue=country, status=status.HTTP_404_NOT_FOUND)
    try:
        row, changed = cfg.change_config(
            request.user, code, request.data, reason=str(request.data.get('reason') or ''),
            request=request)
    except cfg.ConfigError as exc:
        return error_response(code=exc.code, message=exc.message, field=exc.field,
                              issue=exc.message, status=status.HTTP_400_BAD_REQUEST)
    return success_response({'changed': changed, **_config_row(code, request.user)},
                            status.HTTP_200_OK)
