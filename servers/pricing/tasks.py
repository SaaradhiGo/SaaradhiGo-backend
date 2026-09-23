"""Pricing background work. Thin adapters only; the logic lives alongside.

Nothing in this module charges anybody. The fare shadow observes completed trips
and writes to `TripFareShadow`, which no payment, wallet, commission or
settlement path reads.
"""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(
    name='pricing.fare_shadow_sweep',
    bind=True,
    max_retries=1,
    default_retry_delay=300,
    acks_late=True,
)
def fare_shadow_sweep(self):
    """Observe recently completed trips that have measured actuals.

    A sweep rather than a completion hook: the actuals land on a delay, and a
    sweep also catches trips whose trail arrived late. Bounded per run, and a
    no-op while FARE_SHADOW_ENABLED is False.

    Idempotent -- one observation per trip is enforced by the one-to-one column,
    so redelivery cannot double-count a trip in the analysis.
    """
    from servers.pricing.fare_shadow import sweep_shadow

    result = sweep_shadow()
    return {k: v for k, v in result.items() if k != 'by_status'} | {
        'by_status': result.get('by_status', {}),
    }
