"""Celery tasks for Promotions & Incentives (module 12).

    promotions.on_trip_completed   on_commit of a trip completing: counts the trip toward the driver's
                                   incentives and the referee's referral. Safe under at-least-once
                                   delivery: (campaign, trip) and (referral, trip) are unique rows, the
                                   counts are recomputed from them, and every reward carries a unique
                                   idempotency key, so a redelivered or doubled run pays nothing twice.
    promotions.sweep               beat, every 5 minutes: scheduled campaigns start, campaigns past their
                                   window end, reservations on cancelled trips go back to their budget,
                                   qualified referrals that were unpaid for want of budget are retried.
                                   Every step converges on a re-run.

Neither changes a fare, a commission, a trip status or an approval; they move campaign counters and
write the campaign ledger.
"""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name='promotions.on_trip_completed', bind=True, max_retries=3,
             default_retry_delay=60, acks_late=True)
def on_trip_completed(self, trip_id):
    from servers.ride.models import Trip

    from . import incentives, referrals

    trip = (Trip.objects.select_related('user_id', 'driver_id', 'driver_id__user_id',
                                        'driver_id__home_city', 'zone', 'zone__geo_node')
            .filter(pk=trip_id).first())
    if trip is None or not trip.status_id or trip.status_id.status_code != 'completed':
        return {'trip': trip_id, 'counted': False}
    try:
        counted = {'incentives': len(incentives.on_trip_completed(trip)),
                   'referrals': len(referrals.on_trip_completed(trip) or [])}
    except Exception as exc:  # noqa: BLE001
        logger.exception('promotions.on_trip_completed failed for trip %s', trip_id)
        raise self.retry(exc=exc)
    return {'trip': trip_id, **counted}


@shared_task(name='promotions.sweep', acks_late=True)
def sweep():
    from .workflow import sweep as run

    summary = run()
    if any(summary.values()):
        logger.info('promotions sweep: %s', summary)
    return summary


def enqueue_trip_completed(trip_id):
    """Hand a completed trip to the task. Never raises: the sweep and the idempotent counts make
    a lost enqueue a delay, not a loss, and completing a trip must not depend on the broker."""
    try:
        on_trip_completed.delay(trip_id)
    except Exception:  # noqa: BLE001
        logger.exception('promotions_enqueue_failed trip=%s', trip_id)
