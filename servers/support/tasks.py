"""Celery tasks for the Support module.

`support.sweep_waiting_tickets` runs hourly (`CELERY_BEAT_SCHEDULE`). It is
idempotent (a reminder is sent once per ticket, a closed ticket is no longer
waiting), so a redelivered or doubled run converges.
"""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name='support.sweep_waiting_tickets')
def sweep_waiting_tickets():
    from .waiting import sweep_waiting

    summary = sweep_waiting()
    if summary['reminded'] or summary['closed']:
        logger.info('support waiting sweep: %s', summary)
    return summary
