"""Raising and counting Command Centre alerts."""

import logging

from django.utils import timezone

from .models import WARNING, OperatorAlert

logger = logging.getLogger(__name__)


def raise_alert(kind, *, title, severity=WARNING, payload=None,
                source_type='', source_id='', city=None):
    """Write one alert. Idempotent per (source_type, source_id) while open.

    A producer that fires twice for the same thing -- a retried task, a
    re-saved row -- must not page twice; the open alert is returned instead
    of a second one. Once acknowledged, a new occurrence raises a new alert,
    because "it happened again" is news.
    """
    if source_type and source_id:
        existing = OperatorAlert.objects.filter(
            source_type=source_type, source_id=str(source_id),
            acknowledged_at__isnull=True,
        ).first()
        if existing is not None:
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


def open_alerts():
    return OperatorAlert.objects.filter(acknowledged_at__isnull=True)


def open_alert_count():
    """For the dashboard tile. Cheap, and never raises: a dashboard that
    fails because the alert table is unreachable hides every other tile."""
    try:
        return open_alerts().count()
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
