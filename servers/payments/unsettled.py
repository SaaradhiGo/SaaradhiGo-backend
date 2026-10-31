"""A completed trip left unsettled because no commission rule governs it.

Module 8 made a missing commission rule loud rather than defaulted: `credit_driver_wallet`
raises `CommissionNotConfigured`, `settle_or_defer` logs it and leaves the trip completed
but UNSETTLED (nothing is written). Logging is not enough: nobody reads a log at 3 a.m.
This module is what makes it impossible to miss.

THE ALERT
---------
When the deferral happens a CRITICAL Command Centre `OperatorAlert` of kind
`settlement.no_commission_rule` is raised: ONE per trip (idempotent on `source_id`),
anchored to the trip's CITY, with no rider or driver detail in it (trip id, city, fare,
currency, the reason). Who sees it is a money question, not an operations one: only
holders of `finance.read` in scope, and Super Admin. City Ops, Marketing and Analyst do not
(`command_centre.views._visible_alerts`). It is not acknowledgeable: it clears itself.

IT RESOLVES ITSELF
------------------
The alert clears the moment the trip settles, by whichever path: the hourly task
(`payments.retry_unsettled_trips`), a finance holder's "Retry now", or anything else that
writes the settlement. The task also clears any alert whose trip has since been settled.

RETRY IS NOT A MONEY DECISION
-----------------------------
Retrying applies the commission rule that NOW governs the trip (or its booking-time
snapshot, if it has one). It decides nothing about what anyone is paid, so "Retry now"
needs `finance.adjust` (the finance write capability) and an audit row, not an approval.
"""

import logging

from django.utils import timezone

from servers.admin_audit.context import audit_context
from servers.admin_audit.services import record_admin_action
from servers.command_centre.models import (
    CRITICAL, SETTLEMENT_NO_COMMISSION_RULE, OperatorAlert,
)
from servers.command_centre.services import raise_alert

logger = logging.getLogger(__name__)

SOURCE_TYPE = 'trip_settlement'
RETRY_LABEL = 'system:unsettled_retry'
REASON = 'No commission rule governs this trip, so it was left unsettled.'
#: How many trips one hourly run retries: bounded, oldest first.
RETRY_BATCH = 200


def city_node_of(trip):
    """The city GeoNode the trip's money belongs to (its pickup zone's city), or None."""
    zone = getattr(trip, 'zone', None)
    node = getattr(zone, 'geo_node', None) if zone is not None else None
    while node is not None and node.kind != 'city':
        node = node.parent
    return node


def raise_unsettled_alert(trip, detail=''):
    """One critical alert for `trip`; the open one is returned if it already exists."""
    city = city_node_of(trip)
    fare_minor = trip.final_fare_minor or trip.estimated_fare_minor or 0
    return raise_alert(
        SETTLEMENT_NO_COMMISSION_RULE, severity=CRITICAL,
        title=f'Trip {trip.id} cannot be settled: no commission rule'
              f'{" in " + city.name if city is not None else ""}'[:200],
        payload={'trip_id': trip.id, 'city': city.code if city is not None else None,
                 'fare_minor': fare_minor, 'currency': trip.currency or '',
                 'completed_at': trip.completed_at.isoformat() if trip.completed_at else None,
                 'reason': REASON},
        source_type=SOURCE_TYPE, source_id=str(trip.id), city=city)


def open_alerts():
    return OperatorAlert.objects.filter(
        kind=SETTLEMENT_NO_COMMISSION_RULE, acknowledged_at__isnull=True)


def resolve_unsettled_alert(trip_id, why='the trip was settled'):
    """Clear the open alert for `trip_id`, if any. Returns True if one was cleared."""
    alert = open_alerts().filter(source_type=SOURCE_TYPE, source_id=str(trip_id)).first()
    if alert is None:
        return False
    updated = OperatorAlert.objects.filter(pk=alert.pk, acknowledged_at__isnull=True).update(
        acknowledged_at=timezone.now(), acknowledged_by=None,
        acknowledged_note=f'cleared: {why}')
    if not updated:
        return False
    with audit_context(actor=None, actor_label=RETRY_LABEL, reason=why):
        record_admin_action(
            None, action='operator_alert_cleared', target_type='operator_alert',
            target_id=alert.id, before={'acknowledged': False, 'severity': alert.severity},
            after={'acknowledged': True, 'cleared_by': RETRY_LABEL, 'kind': alert.kind,
                   'source_type': alert.source_type, 'source_id': alert.source_id},
            reason=why)
    return True


def is_settled(trip):
    from .models import TripSettlement

    return TripSettlement.objects.filter(trip=trip).exists()


def retry_trip(trip):
    """Try to settle one trip now. Returns True when it is settled afterwards."""
    from servers.driver.utils import settle_or_defer

    if is_settled(trip):
        resolve_unsettled_alert(trip.id, 'the trip is already settled')
        return True
    settled = bool(settle_or_defer(trip))
    if settled:
        resolve_unsettled_alert(trip.id, 'the trip was settled')
    return settled


def retry_all(limit=RETRY_BATCH):
    """The hourly task body. Retries every trip with an open alert, oldest first.

    Returns `{checked, settled, still_unsettled}`."""
    from servers.ride.models import Trip

    summary = {'checked': 0, 'settled': 0, 'still_unsettled': 0}
    for alert in open_alerts().order_by('created_at')[:limit]:
        trip = (Trip.objects.select_related('zone', 'zone__geo_node', 'driver_id',
                                            'driver_id__user_id')
                .filter(pk=alert.source_id).first())
        summary['checked'] += 1
        if trip is None:
            resolve_unsettled_alert(alert.source_id, 'the trip no longer exists')
            continue
        if retry_trip(trip):
            summary['settled'] += 1
        else:
            summary['still_unsettled'] += 1
    if summary['settled']:
        logger.warning('unsettled trip retry: %s', summary)
    return summary


def unsettled_rows(user, node=None, limit=100):
    """`(rows, count)` for the Finance overview: the unsettled trips inside the caller's
    money scope. Trip id, city, completed_at, fare, currency and the reason; nothing about
    the rider or the driver."""
    from servers.payments.scope import money_visible_to
    from servers.ride.models import Trip

    ids = [int(a.source_id) for a in open_alerts().filter(source_type=SOURCE_TYPE)]
    if not ids:
        return [], 0
    trips = money_visible_to(
        user, Trip, node, queryset=Trip.objects.filter(id__in=ids), permission='finance.read')
    count = trips.count()
    rows = []
    for trip in trips.select_related('zone', 'zone__geo_node').order_by('completed_at', 'id')[:limit]:
        city = city_node_of(trip)
        minor = trip.final_fare_minor or trip.estimated_fare_minor or 0
        rows.append({
            'trip_id': trip.id,
            'city': {'code': city.code, 'name': city.name} if city is not None else None,
            'completed_at': trip.completed_at,
            'fare_minor': minor, 'currency': trip.currency or '',
            'reason': REASON,
        })
    return rows, count
