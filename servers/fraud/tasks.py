"""Celery entry point for Fraud & Risk (module 13).

    fraud.run_rules   beat, every 15 minutes: evaluates every enabled, configured scheduled rule per
                      country over its bounded recent window, raises or extends flags, stamps the
                      rule's last run, then checks the queue-aging alert.

Idempotent by construction (see `raising`): a doubled or redelivered run finds the same hits, the
flags already carry their ids, and nothing is written. Rules only RAISE flags; this task never
acts on an account, a trip, a payout or a notification.
"""

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name='fraud.run_rules')
def run_rules():
    from .runner import run_all

    return run_all()
