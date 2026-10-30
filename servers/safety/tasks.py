"""Celery entry points for the safety module. The first two run every minute, the coverage sweep every five.

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


@shared_task(name='safety.coverage_sweep')
def coverage_sweep():
    """Raise / clear `safety_coverage_gap` per live city. Idempotent."""
    from .coverage import sweep

    return sweep()
