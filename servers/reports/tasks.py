"""The two rebuild sweeps (Celery beat, `base.settings.CELERY_BEAT_SCHEDULE`).

  reports.rebuild_recent   every 30 minutes: today and yesterday of every city (city-local days)
  reports.rebuild_week     nightly: the last 7 local days of every city, plus the rider cohorts
                           (a late settlement or refund lands on an earlier day)

Both are idempotent: `compute.rebuild_day` replaces a day's rows from the source data, so a
redelivered or doubled run ends in the same rows. They write only the derived rollup tables.
One city failing is logged and does not stop the others.
"""

import logging
from datetime import timedelta

from celery import shared_task

from . import compute

logger = logging.getLogger(__name__)

RECENT_DAYS = 2
NIGHTLY_DAYS = 7


def _sweep(days, trigger, cohorts):
    rebuilt, failed = 0, []
    for city in compute.active_cities():
        try:
            today = compute.local_today(city)
            for back in range(days):
                compute.rebuild_day(city, today - timedelta(days=back), trigger=trigger)
                rebuilt += 1
            if cohorts:
                compute.rebuild_cohorts(city)
        except Exception:  # noqa: BLE001 - one city must not stop the rest
            logger.exception('reports: %s failed for city %s', trigger, city.code)
            failed.append(city.code)
    return {'days_rebuilt': rebuilt, 'failed_cities': failed}


@shared_task(name='reports.rebuild_recent')
def rebuild_recent():
    return _sweep(RECENT_DAYS, 'beat-recent', cohorts=False)


@shared_task(name='reports.rebuild_week')
def rebuild_week():
    return _sweep(NIGHTLY_DAYS, 'beat-nightly', cohorts=True)
