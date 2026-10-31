"""Celery tasks for the notifications outbox.

DURABILITY (all four run with the project default `acks_late`)
    send_message         claims its row with a conditional UPDATE (queued -> sending), so a
                         redelivered or doubled task sends nothing twice. Retries are its own
                         rescheduling with backoff, bounded by NOTIFY_MAX_ATTEMPTS, then `failed`.
    dispatch_queued      beat, every minute. Sends due rows (a backstop for a lost on-commit
                         task), requeues rows stuck `sending`, drains the DriverNotice and
                         DriverPayoutNotice outboxes and starts due campaigns. Every step is
                         idempotent.
    run_campaign         expands a scheduled campaign; idempotent per (campaign, user).
    check_provider_health  beat, every 5 minutes: raises / clears notify.provider_failing.
None of them touches money, a fare, a trip or a driver's status.
"""

import logging
from datetime import timedelta

from celery import shared_task
from django.db.models import Q
from django.utils import timezone

from . import campaign_workflow, config, dispatcher, events, health
from .models import FAILED, QUEUED, SENDING, NotificationMessage

logger = logging.getLogger(__name__)

SWEEP_BATCH = 1000
STUCK_SENDING_MINUTES = 10
#: A row newer than this is left to its own on-commit task rather than raced by the sweep.
SETTLE_SECONDS = 20


@shared_task(name='notifications.send_message')
def send_message(message_id):
    return dispatcher.deliver(message_id)


@shared_task(name='notifications.run_campaign')
def run_campaign(campaign_id):
    return campaign_workflow.start(campaign_id)


@shared_task(name='notifications.check_provider_health')
def check_provider_health():
    return health.check()


def requeue_stuck(now=None):
    """Rows left `sending` by a lost worker go back to the queue, or fail at the limit."""
    now = now or timezone.now()
    stuck = NotificationMessage.objects.filter(
        status=SENDING, updated_at__lt=now - timedelta(minutes=STUCK_SENDING_MINUTES))
    failed = stuck.filter(attempts__gte=config.max_attempts()).update(
        status=FAILED, last_error='worker lost mid-send; dead-lettered at the attempt limit',
        updated_at=now)
    requeued = stuck.filter(attempts__lt=config.max_attempts()).update(
        status=QUEUED, not_before=None, updated_at=now)
    return {'requeued': requeued, 'failed': failed}


@shared_task(name='notifications.dispatch_queued')
def dispatch_queued():
    now = timezone.now()
    summary = {'stuck': requeue_stuck(now)}
    due = (NotificationMessage.objects.filter(status=QUEUED)
           .filter(Q(not_before__isnull=True) | Q(not_before__lte=now))
           .filter(created_at__lt=now - timedelta(seconds=SETTLE_SECONDS))
           .order_by('id').values_list('id', flat=True)[:SWEEP_BATCH])
    ids = list(due)
    for message_id in ids:
        send_message.delay(message_id)
    summary['dispatched'] = len(ids)
    summary['notices'] = events.drain_all(now)
    started = []
    for campaign_id in campaign_workflow.due_ids(now):
        run_campaign.delay(campaign_id)
        started.append(campaign_id)
    summary['campaigns'] = started
    if any([ids, started, summary['stuck']['requeued'], summary['notices']['commission_messages'],
            summary['notices']['payout_messages']]):
        logger.info('notifications dispatch_queued: %s', summary)
    return summary


