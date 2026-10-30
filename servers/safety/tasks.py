"""Celery entry points for the safety module. Both run every minute.

Idempotent by construction (see `escalation` and `detection`), so the
project-default `acks_late` is safe: a redelivered or doubled run converges
on the same alerts, signals and pushes.
"""

from celery import shared_task


@shared_task(name='safety.escalation_sweep')
def escalation_sweep():
    from .escalation import sweep

    return sweep()


@shared_task(name='safety.evaluate_signals')
def evaluate_signals():
    from .detection import evaluate

    return evaluate()
