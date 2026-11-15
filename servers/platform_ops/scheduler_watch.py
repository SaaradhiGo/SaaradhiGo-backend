"""Is the scheduler running? (15 Nov, US round 4.)

Celery beat sends `platform_ops.scheduler_heartbeat` every five minutes and a worker runs it, so a
fresh stamp proves both halves. When the stamp is older than SCHEDULER_SILENT_HOURS (2), the console
shows a critical `platform.scheduler_stopped` alert to Super Admin; it clears itself once a run lands.

The check cannot ride on beat (a dead beat would never raise it), so the console's alert list runs
it on read, at most once a minute, and the heartbeat task runs it too, which is what clears it.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from servers.command_centre.models import CRITICAL, SCHEDULER_STOPPED, OperatorAlert
from servers.command_centre.services import acknowledge, raise_alert

from .models import SchedulerHeartbeat

logger = logging.getLogger(__name__)

NAME = 'beat'
SOURCE_TYPE = 'scheduler'
THROTTLE_KEY = 'scheduler_watch:checked'


def silent_after():
    return timedelta(hours=float(getattr(settings, 'SCHEDULER_SILENT_HOURS', 2)))


def beat(now=None):
    """Stamp a run. Called by the heartbeat task only."""
    now = now or timezone.now()
    SchedulerHeartbeat.objects.update_or_create(name=NAME, defaults={'last_run_at': now})
    return now


def last_run():
    row = SchedulerHeartbeat.objects.filter(name=NAME).first()
    return row.last_run_at if row else None


def check(now=None):
    """Raise or clear the alert. Returns 'stopped', 'running' or 'unknown' (no stamp yet)."""
    now = now or timezone.now()
    last = last_run()
    if last is None:
        return 'unknown'
    if now - last > silent_after():
        hours = (now - last).total_seconds() / 3600
        raise_alert(
            SCHEDULER_STOPPED, severity=CRITICAL,
            title=(f'Scheduled jobs have not run for {hours:.0f} hours: payouts, fees, card '
                   f'retries and sweeps are on hold'),
            payload={'last_run_at': last.isoformat(), 'silent_hours': round(hours, 1)},
            source_type=SOURCE_TYPE, source_id=NAME)
        return 'stopped'
    for alert in OperatorAlert.objects.filter(kind=SCHEDULER_STOPPED, acknowledged_at__isnull=True):
        acknowledge(alert, by=None, note='cleared: scheduled jobs are running again')
    return 'running'


def check_on_read():
    """For the console's alert list: at most once a minute, and never fails the list."""
    try:
        if cache.add(THROTTLE_KEY, 1, timeout=60):
            check()
    except Exception:  # noqa: BLE001 - a watch, not a control
        logger.exception('scheduler_watch check failed')
