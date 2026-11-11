"""The threshold sweep: every minute, every city, raise or clear.

DURABILITY
----------
`acks_late` (the project default) is safe here because the sweep is
idempotent by construction: `raise_alert` returns the open alert for the
same (city, metric) instead of a second one, escalation is an update of
that row, and a clear is a conditional UPDATE on `acknowledged_at IS
NULL`. A redelivered or doubled run converges on the same alerts.

It writes no trip, no money and no driver status; its only side effects
are `OperatorAlert` rows and their audit rows.

Scheduled as `command-centre-threshold-sweep-every-minute` in
`base.settings.CELERY_BEAT_SCHEDULE`, next to the driver presence sweep.
"""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name='command_centre.evaluate_alert_thresholds')
def evaluate_alert_thresholds():
    from .thresholds import evaluate_all

    summary = evaluate_all()
    raised = sum(v.get('raised', 0) for v in summary.values())
    cleared = sum(v.get('cleared', 0) for v in summary.values())
    if raised or cleared:
        logger.info('command_centre threshold sweep: raised=%s cleared=%s cities=%s',
                    raised, cleared, len(summary))
    return {'cities': summary, 'raised': raised, 'cleared': cleared}


@shared_task(name='command_centre.refresh_city_rollups')
def refresh_city_rollups():
    """Recount the last two hours of five-minute city buckets (`rollups`).

    Every minute (beat), plus the previous day once a night (`backfill_rollups`).
    acks_late is safe: the task replaces the rows of the buckets it recounts
    inside one transaction, from the source tables, so a redelivered or doubled
    run writes the same rows; a cache lock keeps two runs from overlapping. No
    retry: the next minute is the retry. Writes `CityRollup` rows only.
    """
    from .rollups import refresh_recent

    return refresh_recent()


@shared_task(name='command_centre.backfill_city_rollups')
def backfill_city_rollups(days=1):
    """Recount whole days of buckets (nightly: yesterday, for outcomes that
    landed after the minute task's two-hour window). Same idempotency."""
    from .rollups import backfill

    return backfill(int(days))
