"""Raising and counting Command Centre alerts."""

import logging

from django.utils import timezone

from .models import PLATFORM_ALERT_KINDS, WARNING, OperatorAlert

logger = logging.getLogger(__name__)


#: Higher is worse. An alert only ever moves up this scale in place.
SEVERITY_ORDER = {'info': 0, 'warning': 1, 'critical': 2}


def _live_city_only(kind):
    """Kinds about a city's day-to-day running: its numbers and its safety cover."""
    from .models import FRAUD_QUEUE_AGING, SAFETY_COVERAGE_GAP, THRESHOLD_KINDS

    return kind in THRESHOLD_KINDS or kind in (SAFETY_COVERAGE_GAP, FRAUD_QUEUE_AGING)


def _city_is_live(city, kind):
    """A city that is not live (draft or paused) raises no OPERATIONAL alerts --
    its numbers and its safety cover -- because nothing runs there (owner,
    2026-11-03: no "no safety cover" alerts for cities that aren't live). An SOS
    or a money failure is real wherever it happens and always alerts. A place
    above a city (a country, a state) and no place at all always may."""
    if not _live_city_only(kind):
        return True
    if city is None or getattr(city, 'kind', 'city') != 'city':
        return True
    from servers.admin_access.models import ACTIVE

    return city.status == ACTIVE


def raise_alert(kind, *, title, severity=WARNING, payload=None,
                source_type='', source_id='', city=None):
    """Write one alert per underlying event. Returns it, or None for an
    operational alert in a city that is not live.

    ONE PER EVENT. (`source_type`, `source_id`) names the event -- for safety,
    the incident once there is one -- and while an alert for it is open a new
    occurrence never pages twice: the open alert is returned. When the new
    occurrence is WORSE (warning -> critical), that same alert is raised in
    place: severity, kind, title and payload move up, and the console shows
    one row that got worse rather than two. Once the alert is closed, a new
    occurrence is news and raises a new alert.

    Closing is the producer's job when the cause ends (`clear_alerts`).
    """
    if not _city_is_live(city, kind):
        logger.info('operator_alert_skipped_city_not_live', extra={
            'event': 'operator_alert_skipped_city_not_live', 'kind': kind,
            'city': getattr(city, 'code', None)})
        return None
    if source_type and source_id:
        existing = OperatorAlert.objects.filter(
            source_type=source_type, source_id=str(source_id),
            acknowledged_at__isnull=True,
        ).first()
        if existing is not None:
            if SEVERITY_ORDER.get(severity, 0) > SEVERITY_ORDER.get(existing.severity, 0):
                existing.severity, existing.kind, existing.title = severity, kind, title[:200]
                existing.payload = {**(existing.payload or {}), **(payload or {})}
                existing.save(update_fields=['severity', 'kind', 'title', 'payload'])
                logger.warning('operator_alert_escalated', extra={
                    'event': 'operator_alert_escalated', 'alert_id': existing.id,
                    'kind': kind, 'severity': severity})
            return existing

    alert = OperatorAlert.objects.create(
        kind=kind, severity=severity, title=title[:200],
        payload=payload or {}, source_type=source_type,
        source_id=str(source_id) if source_id else '', city=city,
    )
    logger.warning('operator_alert_raised', extra={
        'event': 'operator_alert_raised', 'alert_id': alert.id,
        'kind': kind, 'severity': severity,
        'source_type': source_type, 'source_id': str(source_id),
        'city': getattr(city, 'code', None),
    })
    return alert


def clear_alerts(source_type, source_id, *, note, kinds=None):
    """Close the open alert(s) for an event whose cause has ended. Returns how
    many. `kinds` limits it to alerts currently of those kinds (an incident's
    "not acknowledged" alert ends at acknowledgement; its "nobody assigned"
    alert does not)."""
    rows = OperatorAlert.objects.filter(source_type=source_type, source_id=str(source_id),
                                        acknowledged_at__isnull=True)
    if kinds:
        rows = rows.filter(kind__in=kinds)
    count = 0
    for alert in rows:
        count += acknowledge(alert, by=None, note=note)
    return count


def open_alerts():
    return OperatorAlert.objects.filter(acknowledged_at__isnull=True)


def open_alert_count():
    """For the dashboard tile. Cheap, and never raises: a dashboard that
    fails because the alert table is unreachable hides every other tile."""
    try:
        # The shared dashboard tile is for every operator: platform-health alerts (Super
        # Admin only) are not in it.
        return open_alerts().exclude(kind__in=PLATFORM_ALERT_KINDS).count()
    except Exception:  # noqa: BLE001 - a tile, not a control
        logger.exception('open_alert_count failed')
        return 0


def acknowledge(alert, *, by, note=''):
    """Mark `alert` taken by `by`. Returns False if it already was.

    `by` may be None for a clearance by the threshold sweep (the number
    went back into range); the note says so in that case."""
    if alert.acknowledged_at is not None:
        return False
    alert.acknowledged_at = timezone.now()
    alert.acknowledged_by = by
    alert.acknowledged_note = (note or '')[:10000]
    alert.save(update_fields=['acknowledged_at', 'acknowledged_by', 'acknowledged_note'])
    return True
