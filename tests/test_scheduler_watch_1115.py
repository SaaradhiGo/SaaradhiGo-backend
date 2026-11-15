"""15 Nov (US round 4): the console alerts Super Admin when the scheduled jobs have not run for two
hours, and the alert clears itself once they run again."""
from datetime import timedelta

import pytest
from django.utils import timezone

from servers.command_centre.models import PLATFORM_ALERT_KINDS, SCHEDULER_STOPPED, OperatorAlert
from servers.platform_ops import scheduler_watch
from servers.platform_ops.models import SchedulerHeartbeat
from servers.platform_ops.tasks import scheduler_heartbeat

pytestmark = pytest.mark.django_db


def _open():
    return OperatorAlert.objects.filter(kind=SCHEDULER_STOPPED, acknowledged_at__isnull=True)


def test_a_silent_scheduler_raises_one_critical_alert_and_a_run_clears_it():
    now = timezone.now()
    scheduler_watch.beat(now - timedelta(hours=1, minutes=59))
    assert scheduler_watch.check(now) == 'running'
    assert not _open().exists()

    scheduler_watch.beat(now - timedelta(hours=3))
    assert scheduler_watch.check(now) == 'stopped'
    assert scheduler_watch.check(now + timedelta(minutes=5)) == 'stopped'
    alert = _open().get()
    assert alert.severity == 'critical' and 'not run for 3 hours' in alert.title
    assert SCHEDULER_STOPPED in PLATFORM_ALERT_KINDS  # Super Admin only

    assert scheduler_heartbeat()['scheduler'] == 'running'
    assert not _open().exists()
    assert SchedulerHeartbeat.objects.get(name='beat').last_run_at > now - timedelta(minutes=1)


def test_the_console_alert_list_runs_the_check_itself(monkeypatch):
    """A dead beat cannot raise its own alert, so reading the list does."""
    from django.core.cache import cache

    calls = []
    monkeypatch.setattr(scheduler_watch, 'check', lambda now=None: calls.append(1))
    cache.delete(scheduler_watch.THROTTLE_KEY)
    scheduler_watch.check_on_read()
    scheduler_watch.check_on_read()
    assert calls == [1]  # throttled to once a minute


def test_the_beat_schedule_sends_the_heartbeat_every_five_minutes():
    from django.conf import settings

    entry = settings.CELERY_BEAT_SCHEDULE['platform-scheduler-heartbeat']
    assert entry['task'] == 'platform_ops.scheduler_heartbeat' and entry['schedule'] == 300.0
