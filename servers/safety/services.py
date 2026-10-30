"""Opening, joining and working incidents. No HTTP in here.

THE INVARIANTS
--------------
* Every SOS has a case: `attach_sos` creates the incident for its trip or
  joins the open one (at most one non-closed incident per trip, enforced by a
  partial unique index, so two SOS racing on one trip cannot make two).
* Every change appends a `SafetyIncidentUpdate`; nothing is edited in place
  on the timeline.
* Nothing here moves money or changes what a rider pays or a driver earns.
  The only account actions (suspend a driver, block a rider) are the existing
  audited services, called from the views.
"""

import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import (
    ANCHOR_DRIVER_HOME, ANCHOR_RIDER, ANCHOR_RIDER_COUNTRY, ANCHOR_SOS, ANCHOR_TRIP,
    ANCHOR_UNANCHORED, CLOSED, LINK_DRIVER, LINK_RIDER, LINK_SOS, LINK_TICKET, LINK_TRIP,
    LIVE_STATUSES, OPEN, SEVERITY_RANK, SRC_SIGNAL, SRC_SOS, SRC_TICKET, U_ASSIGNED,
    U_CLOSED, U_LINK, U_NOTE, U_OPENED, U_POLICE, U_SOS, U_STATUS, U_UNLINK,
    SafetyIncident, SafetyIncidentLink, SafetyIncidentUpdate,
)

logger = logging.getLogger(__name__)

SYSTEM = 'system'

#: SOS event type -> (incident category, severity). A panic button is always at
#: least high: somebody pressed it.
SOS_MAPPING = {
    'panic': ('other', 'critical'),
    'medical': ('medical', 'critical'),
    'accident': ('accident', 'critical'),
    'harassment': ('harassment', 'high'),
    'vehicle_breakdown': ('other', 'medium'),
    'other': ('other', 'high'),
}


def actor_label(user):
    if user is None:
        return SYSTEM
    return (getattr(user, 'full_name', None) or getattr(user, 'email', None)
            or getattr(user, 'phone_number', None) or str(user))[:255]


# ---------------------------------------------------------------------------
# Where it is
# ---------------------------------------------------------------------------

def country_of_node(node):
    from servers.support.sla import country_of_node as _country

    return _country(node)


def trip_node(trip):
    zone = getattr(trip, 'zone', None) if trip is not None else None
    return getattr(zone, 'geo_node', None) if zone is not None else None


def _rider_anchor(rider_user):
    """`(node, source)` for a rider with no trip, by the rider rule."""
    from servers.rider.scope import anchor_nodes

    rider = getattr(rider_user, 'rider', None)
    if rider is None:
        return None, ANCHOR_UNANCHORED
    nodes = anchor_nodes(rider)
    if not nodes:
        return None, ANCHOR_UNANCHORED
    # Prefer a city-level anchor (a trip city, then the home city) over the
    # country the phone belongs to; the nodes come back unordered, so take the
    # deepest path, which is the most specific place.
    nodes = sorted(nodes, key=lambda n: (-len(n.path), n.code))
    best = nodes[0]
    return best, (ANCHOR_RIDER_COUNTRY if best.kind == 'country' else ANCHOR_RIDER)


def resolve_incident_anchor(*, trip=None, sos=None, driver=None, rider_user=None):
    """`(GeoNode or None, anchor_source)`, in the ruled order: the trip's city,
    else the SOS anchor, else a driver's home city, else the rider rule."""
    node = trip_node(trip)
    if node is not None:
        return node, ANCHOR_TRIP
    if sos is not None:
        if sos.anchor_node_id:
            return sos.anchor_node, ANCHOR_SOS
        # The SOS could not be placed, so neither can its case: an emergency
        # nobody can place must reach every responder (the SOS rule), and the
        # rider/driver fall-backs below would hide it from other cities.
        return None, ANCHOR_UNANCHORED
    if driver is not None and driver.home_city_id:
        return driver.home_city, ANCHOR_DRIVER_HOME
    if rider_user is not None:
        return _rider_anchor(rider_user)
    return None, ANCHOR_UNANCHORED


def country_for(node, *, trip=None, user=None):
    """ISO country: the anchor's, else the trip zone's, else the person's phone."""
    from base.countries import COUNTRIES, country_of_phone

    code = country_of_node(node) if node is not None else ''
    if code:
        return code
    zone = getattr(trip, 'zone', None) if trip is not None else None
    if zone is not None and (zone.country or '').upper() in COUNTRIES:
        return zone.country.upper()
    phone = getattr(user, 'phone_number', None) if user is not None else None
    guess = country_of_phone(phone) if phone else None
    return guess.code if guess is not None else ''


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------

def add_update(incident, kind, *, actor=None, label=None, note='', data=None,
               from_status='', to_status='', idempotency_key=''):
    return SafetyIncidentUpdate.objects.create(
        incident=incident, kind=kind, actor=actor,
        actor_label=label if label is not None else actor_label(actor),
        note=(note or '')[:10000], data=data or {},
        from_status=from_status, to_status=to_status,
        idempotency_key=(idempotency_key or '')[:120],
    )


def _add_link(incident, kind, target_id, actor=None):
    link, created = SafetyIncidentLink.objects.get_or_create(
        incident=incident, kind=kind, target_id=target_id,
        defaults={'linked_by': actor})
    return link, created


def _link_people(incident, *, trip=None, user=None, actor=None):
    """Link the trip, its rider and its driver, and a raiser, as known."""
    if trip is not None:
        _add_link(incident, LINK_TRIP, trip.id, actor)
        _add_link(incident, LINK_RIDER, trip.user_id_id, actor)
        if trip.driver_id_id:
            _add_link(incident, LINK_DRIVER, trip.driver_id_id, actor)
    if user is not None:
        driver = getattr(user, 'driver', None)
        if driver is not None:
            _add_link(incident, LINK_DRIVER, driver.id, actor)
        elif getattr(user, 'rider', None) is not None:
            _add_link(incident, LINK_RIDER, user.id, actor)


# ---------------------------------------------------------------------------
# Opening
# ---------------------------------------------------------------------------

def open_incident_for_trip(trip):
    if trip is None:
        return None
    return SafetyIncident.objects.filter(trip=trip).exclude(status=CLOSED).first()


def _raise_severity(incident, severity, *, reason):
    if SEVERITY_RANK[severity] < SEVERITY_RANK[incident.severity]:
        before = incident.severity
        incident.severity = severity
        incident.save(update_fields=['severity', 'updated_at'])
        add_update(incident, U_NOTE, label=SYSTEM, note=reason,
                   data={'severity_from': before, 'severity_to': severity})


def open_incident(*, source, category, severity, title, trip=None, sos=None, driver=None,
                  rider_user=None, ticket=None, actor=None, note=''):
    """Create the incident, or join the open one for `trip`.

    Returns `(incident, created)`. Safe to call twice for the same trip: the
    second call joins. A caller that wants to record WHY it joined adds its
    own timeline entry.
    """
    existing = open_incident_for_trip(trip)
    if existing is not None:
        _raise_severity(existing, severity, reason=f'Severity raised by {source}.')
        return existing, False

    node, anchor_source = resolve_incident_anchor(
        trip=trip, sos=sos, driver=driver, rider_user=rider_user)
    person = rider_user or (driver.user_id if driver is not None else None) \
        or (ticket.user_id if ticket is not None else None)
    country = country_for(node, trip=trip, user=person)
    try:
        with transaction.atomic():
            incident = SafetyIncident.objects.create(
                source=source, category=category, severity=severity, title=title[:200],
                trip=trip, anchor_node=node, anchor_source=anchor_source, country=country,
                opened_by=actor)
    except IntegrityError:
        # Lost a race on the one-open-incident-per-trip index: join the winner.
        winner = open_incident_for_trip(trip)
        if winner is None:
            raise
        _raise_severity(winner, severity, reason=f'Severity raised by {source}.')
        return winner, False

    add_update(incident, U_OPENED, actor=actor, label=actor_label(actor) if actor else SYSTEM,
               note=note, to_status=OPEN,
               data={'source': source, 'category': category, 'severity': severity})
    _link_people(incident, trip=trip, actor=actor)
    if driver is not None:
        _add_link(incident, LINK_DRIVER, driver.id, actor)
    if rider_user is not None and getattr(rider_user, 'rider', None) is not None:
        _add_link(incident, LINK_RIDER, rider_user.id, actor)
    if ticket is not None:
        _add_link(incident, LINK_TICKET, ticket.id, actor)
    return incident, True


def attach_sos(event):
    """Give a freshly written SOS its case: create the incident or join the
    trip's open one, and link the SOS."""
    category, severity = SOS_MAPPING.get(event.event_type, ('other', 'high'))
    raiser = event.user
    driver = getattr(raiser, 'driver', None) if raiser is not None else None
    rider_user = raiser if (raiser is not None and getattr(raiser, 'rider', None) is not None) else None
    title = f'SOS ({event.event_type})' + (f' on trip {event.trip_id}' if event.trip_id else '')

    with transaction.atomic():
        incident, created = open_incident(
            source=SRC_SOS, category=category, severity=severity, title=title,
            trip=event.trip, sos=event, driver=driver, rider_user=rider_user)
        type(event).objects.filter(pk=event.pk).update(incident=incident)
        event.incident = incident
        _add_link(incident, LINK_SOS, event.id)
        _link_people(incident, trip=event.trip, user=raiser)
        add_update(incident, U_SOS, label=SYSTEM,
                   note=f'SOS #{event.id} ({event.event_type}) raised'
                        + ('' if created else ': joined the open incident for this trip'),
                   data={'sos_id': event.id, 'event_type': event.event_type,
                         'joined': not created})
    return incident


def attach_sos_safely(event):
    """`attach_sos` that can never fail the SOS that called it."""
    try:
        return attach_sos(event)
    except Exception:  # noqa: BLE001 - the SOS row is already written
        logger.exception('safety: could not attach SOS %s to an incident', event.id)
        return None


def backfill_sos_incidents(limit=50):
    """Attach any SOS that has no incident (an attach that failed, or a row
    from before the module). Returns how many were attached."""
    from servers.sos.models import SOSEvent

    attached = 0
    rows = (SOSEvent.objects.filter(incident__isnull=True, status__in=('open', 'acknowledged'))
            .select_related('user', 'trip', 'trip__zone', 'trip__zone__geo_node',
                            'anchor_node').order_by('id')[:limit])
    for event in rows:
        if attach_sos_safely(event) is not None:
            attached += 1
    return attached


def open_from_ticket(ticket, *, actor, note=''):
    """Escalate a support ticket about safety. Returns `(incident, created)`."""
    user = ticket.user_id
    driver = getattr(user, 'driver', None)
    rider_user = user if getattr(user, 'rider', None) is not None else None
    incident, created = open_incident(
        source=SRC_TICKET, category='other', severity='high',
        title=f'Safety ticket {ticket.id}', trip=ticket.trip_id, driver=driver,
        rider_user=rider_user, ticket=ticket, actor=actor, note=note)
    if not created:
        _add_link(incident, LINK_TICKET, ticket.id, actor)
        add_update(incident, U_LINK, actor=actor,
                   note=f'Support ticket {ticket.id} escalated into this incident',
                   data={'kind': LINK_TICKET, 'target_id': ticket.id})
    return incident, created


def open_from_signal(signal, *, category, severity='high'):
    """Open (or join) the incident for a signal's trip. Idempotent."""
    trip = signal.trip
    title = f'{signal.get_kind_display()} on trip {trip.id}'
    incident, created = open_incident(
        source=SRC_SIGNAL, category=category, severity=severity, title=title, trip=trip,
        driver=trip.driver_id)
    from .models import U_SIGNAL

    add_update(incident, U_SIGNAL, label=SYSTEM,
               note=f'{signal.get_kind_display()} detected',
               data={'signal_id': signal.id, 'kind': signal.kind, **signal.measured})
    return incident, created


# ---------------------------------------------------------------------------
# Working an incident
# ---------------------------------------------------------------------------

def active_sos_count(incident):
    return incident.sos_events.filter(status__in=('open', 'acknowledged')).count()


def note_on_sos_transition(event, new_status, actor, note=''):
    """Called by the SOS acknowledge / resolve / false-alarm views so the
    case file shows what the responders did to the alarm."""
    incident = event.incident
    if incident is None:
        return
    add_update(incident, U_SOS, actor=actor,
               note=f'SOS #{event.id} {new_status.replace("_", " ")}' + (f': {note}' if note else ''),
               data={'sos_id': event.id, 'sos_status': new_status})
    if new_status == 'acknowledged' and incident.status == OPEN:
        before = incident.status
        incident.status = 'investigating'
        incident.save(update_fields=['status', 'updated_at'])
        add_update(incident, U_STATUS, actor=actor, from_status=before,
                   to_status='investigating', note='SOS acknowledged')


def close_blockers(incident):
    """Why this incident cannot be closed yet (empty list: it can)."""
    blockers = []
    live = list(incident.sos_events.filter(status__in=('open', 'acknowledged'))
                .values_list('id', flat=True))
    if live:
        blockers.append({'code': 'SOS_STILL_ACTIVE', 'sos_ids': live,
                         'message': 'Resolve or mark false alarm every linked SOS first.'})
    return blockers


def new_status_ok(current, wanted):
    return current in LIVE_STATUSES and wanted in LIVE_STATUSES


def unlink_allowed(kind, incident, target_id):
    if kind == LINK_SOS:
        return False, 'An SOS stays on its incident.'
    if kind == LINK_TRIP and incident.trip_id == target_id:
        return False, "This is the incident's own trip."
    return True, ''


def refer_to_police(incident, *, actor, reference='', note=''):
    from .provider import get_provider

    result = get_provider().refer(incident, actor=actor, reference=reference, note=note)
    now = timezone.now()
    incident.police_referred_at = now
    incident.police_reference = reference[:120]
    fields = ['police_referred_at', 'police_reference', 'updated_at']
    if incident.status in (OPEN, 'investigating'):
        before = incident.status
        incident.status = 'action_taken'
        fields.append('status')
        add_update(incident, U_STATUS, actor=actor, from_status=before,
                   to_status='action_taken', note='Referred to police')
    incident.save(update_fields=fields)
    add_update(incident, U_POLICE, actor=actor,
               note=note or 'Marked as referred to police', data=result)
    return result


def close_incident(incident, *, actor, summary, outcome):
    now = timezone.now()
    before = incident.status
    incident.status, incident.closed_at = CLOSED, now
    incident.outcome, incident.closing_summary = outcome, summary
    incident.save(update_fields=['status', 'closed_at', 'outcome', 'closing_summary', 'updated_at'])
    add_update(incident, U_CLOSED, actor=actor, from_status=before, to_status=CLOSED,
               note=summary, data={'outcome': outcome})


def assign(incident, assignee, *, actor):
    before = incident.assigned_to_id
    incident.assigned_to = assignee
    incident.save(update_fields=['assigned_to', 'updated_at'])
    add_update(incident, U_ASSIGNED, actor=actor,
               note=(f'Assigned to {actor_label(assignee)}' if assignee else 'Unassigned'),
               data={'from': before, 'to': assignee.id if assignee else None})


def link_added(incident, kind, target_id, actor):
    link, created = _add_link(incident, kind, target_id, actor)
    if created:
        add_update(incident, U_LINK, actor=actor, note=f'Linked {kind} {target_id}',
                   data={'kind': kind, 'target_id': target_id})
    return link, created


def link_removed(incident, kind, target_id, actor):
    removed, _ = SafetyIncidentLink.objects.filter(
        incident=incident, kind=kind, target_id=target_id).delete()
    if removed:
        add_update(incident, U_UNLINK, actor=actor, note=f'Unlinked {kind} {target_id}',
                   data={'kind': kind, 'target_id': target_id})
    return bool(removed)
