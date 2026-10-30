"""The escalation sweep: an alarm nobody took raises its voice. Once.

Two rules, each driven by the country's `SafetyCountryConfig`:

* an SOS still `open` after `sos_ack_minutes`;
* a CRITICAL incident still unassigned after `incident_assign_minutes`.

Each raises a Command Centre alert of its own kind and pushes the responders
at the place (plus Global holders) AND every Super Admin. Each fires exactly
once per event: the marker (`SOSEvent.escalated_at`,
`SafetyIncident.assign_escalated_at`) is set by a conditional UPDATE that only
one run can win, so a redelivered or doubled run converges on one alert and
one push.

NO CONFIG, NO TIMER
-------------------
A country with no active config (or with that timer left blank) has no timer
for that rule. The sweep does not invent one and does not borrow another
country's. An SOS whose country cannot be told at all (no anchor, no trip,
raiser's phone from an unsupported country) likewise has no timer; it already
carries the unanchored alert and sits in every responder's queue.

COST
----
One query per rule, bounded (`BATCH`), over `status='open'` rows older than
the shortest configured timer, using the existing `(status, -created_at)`
index on SOS events; incidents use `safety_inc_status_idx`. The common case,
nothing overdue, is two empty index scans.
"""

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from servers.admin_audit.services import record_admin_action
from servers.command_centre.models import (
    CRITICAL, SAFETY_INCIDENT_UNASSIGNED, SAFETY_SOS_ESCALATED,
)
from servers.command_centre.services import raise_alert

from . import services
from .config import active_configs
from .models import CLOSED, U_ESCALATED, SafetyIncident
from .scope import responders_for, super_admins

logger = logging.getLogger(__name__)

BATCH = 200


def notify(users, title, body, data):
    """Push to each user once. Never raises: a push is not the record."""
    from servers.auth_user.services import send_push_notification

    sent, seen = 0, set()
    for user in users:
        if user.id in seen:
            continue
        seen.add(user.id)
        try:
            if send_push_notification(user, title, body, data):
                sent += 1
        except Exception:  # noqa: BLE001
            logger.exception('safety: push to user %s failed', user.id)
    return sent


def audience(node):
    """The responders at `node` (everyone with the capability when there is no
    node), Global holders included, plus every Super Admin."""
    return [*responders_for(node), *super_admins()]


def _shortest(configs, field):
    values = [getattr(c, field) for c in configs.values() if getattr(c, field)]
    return min(values) if values else None


def escalate_sos(event, config, now):
    minutes = config.sos_ack_minutes
    with transaction.atomic():
        won = type(event).objects.filter(
            pk=event.pk, status='open', escalated_at__isnull=True).update(escalated_at=now)
        if not won:
            return False
        raise_alert(
            SAFETY_SOS_ESCALATED, severity=CRITICAL,
            title=f'SOS #{event.id} not acknowledged after {minutes} min',
            payload={'sos_id': event.id, 'incident_id': event.incident_id,
                     'minutes': minutes, 'country': config.country},
            source_type='sos_escalation', source_id=event.id, city=event.anchor_node)
        if event.incident_id:
            services.add_update(
                event.incident, U_ESCALATED, label=services.SYSTEM,
                note=f'SOS #{event.id} not acknowledged after {minutes} minutes: escalated',
                data={'sos_id': event.id, 'minutes': minutes})
        record_admin_action(
            None, action='safety_sos_escalated', target_type='sos_event', target_id=event.id,
            before={'escalated': False}, after={'escalated': True, 'minutes': minutes,
                                                 'config_version': config.version},
            reason=f'No acknowledgement within {minutes} minutes')
    users = audience(event.anchor_node)
    transaction.on_commit(lambda: notify(
        users, 'SOS still unacknowledged',
        f'SOS #{event.id} has had no response for {minutes} minutes',
        {'type': 'sos_escalation', 'sos_id': str(event.id)}))
    return True


def escalate_incident(incident, config, now):
    minutes = config.incident_assign_minutes
    with transaction.atomic():
        won = SafetyIncident.objects.filter(
            pk=incident.pk, assign_escalated_at__isnull=True, assigned_to__isnull=True,
            severity=CRITICAL).exclude(status=CLOSED).update(assign_escalated_at=now)
        if not won:
            return False
        raise_alert(
            SAFETY_INCIDENT_UNASSIGNED, severity=CRITICAL,
            title=f'Critical incident #{incident.id} unassigned after {minutes} min',
            payload={'incident_id': incident.id, 'minutes': minutes, 'country': config.country},
            source_type='incident_escalation', source_id=incident.id, city=incident.anchor_node)
        services.add_update(
            incident, U_ESCALATED, label=services.SYSTEM,
            note=f'Critical and unassigned for {minutes} minutes: escalated',
            data={'minutes': minutes})
        record_admin_action(
            None, action='safety_incident_escalated', target_type='safety_incident',
            target_id=incident.id, before={'escalated': False},
            after={'escalated': True, 'minutes': minutes, 'config_version': config.version},
            reason=f'Critical incident unassigned for {minutes} minutes')
    users = audience(incident.anchor_node)
    transaction.on_commit(lambda: notify(
        users, 'Critical incident unassigned',
        f'Incident #{incident.id} has had no owner for {minutes} minutes',
        {'type': 'incident_escalation', 'incident_id': str(incident.id)}))
    return True


def sweep(now=None):
    """One pass. Returns a summary dict (also logged when it did something)."""
    from servers.sos.models import SOSEvent

    now = now or timezone.now()
    summary = {'backfilled': services.backfill_sos_incidents(),
               'sos_escalated': 0, 'incidents_escalated': 0}
    configs = active_configs()
    if not configs:
        return summary

    shortest = _shortest(configs, 'sos_ack_minutes')
    if shortest:
        cutoff = now - timedelta(minutes=shortest)
        events = (SOSEvent.objects
                  .filter(status='open', escalated_at__isnull=True, created_at__lte=cutoff)
                  .select_related('anchor_node', 'user', 'trip', 'trip__zone',
                                  'trip__zone__geo_node')
                  .order_by('created_at')[:BATCH])
        for event in events:
            country = services.country_for(event.anchor_node, trip=event.trip, user=event.user)
            config = configs.get(country)
            if config is None or not config.sos_ack_minutes:
                continue
            if event.created_at > now - timedelta(minutes=config.sos_ack_minutes):
                continue
            if escalate_sos(event, config, now):
                summary['sos_escalated'] += 1

    shortest = _shortest(configs, 'incident_assign_minutes')
    if shortest:
        cutoff = now - timedelta(minutes=shortest)
        incidents = (SafetyIncident.objects
                     .filter(severity=CRITICAL, assigned_to__isnull=True,
                             assign_escalated_at__isnull=True, opened_at__lte=cutoff)
                     .exclude(status=CLOSED).select_related('anchor_node')
                     .order_by('opened_at')[:BATCH])
        for incident in incidents:
            config = configs.get(incident.country)
            if config is None or not config.incident_assign_minutes:
                continue
            if incident.opened_at > now - timedelta(minutes=config.incident_assign_minutes):
                continue
            if escalate_incident(incident, config, now):
                summary['incidents_escalated'] += 1

    if summary['sos_escalated'] or summary['incidents_escalated'] or summary['backfilled']:
        logger.warning('safety escalation sweep: %s', summary)
    return summary
