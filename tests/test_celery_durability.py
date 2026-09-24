"""Every registered task must survive losing its worker.

Celery's `task_acks_late` and `task_reject_on_worker_lost` both default to False.
With those defaults the broker forgets a task the moment a worker picks it up, so
a worker killed mid-task loses it silently -- no exception, no retry, nothing in
the logs to say a scheduled action will never happen.

Five of the twelve registered tasks set no options of their own:

    auto_cancel_trip                 a trip nobody accepted is never cancelled,
                                     and the rider waits forever
    reconcile_stuck_withdrawals      stuck payouts stop being surfaced
    reconcile_stuck_payments         a missed webhook strands money undetected
    sweep_stale_driver_presence      ghost drivers stay matchable
    block_expired_driver_licenses    an expired licence keeps driving

This file asserts the configuration rather than mocking a worker crash, because
the configuration IS the behaviour: there is no way to observe acks_late from
inside a task, and a test that kills a real worker proves the setting only on the
machine that ran it. The inventory check is what stops a thirteenth task from
being added without durability.
"""

import pytest
from django.conf import settings


def _celery_app():
    from base.celery import app
    return app


# ---------------------------------------------------------------------------
# The two settings that decide whether a lost worker loses work
# ---------------------------------------------------------------------------

def test_tasks_are_acknowledged_after_execution_not_before():
    """Early ack means a killed worker silently discards the task."""
    assert getattr(settings, 'CELERY_TASK_ACKS_LATE', False) is True, (
        'CELERY_TASK_ACKS_LATE defaults to False; without it a worker killed '
        'mid-task loses the task with no error and no retry'
    )


def test_a_lost_worker_returns_its_task_to_the_queue():
    """acks_late alone is not enough.

    A SIGKILLed worker never gets to reject the message either, so without this
    the task is still lost despite acks_late.
    """
    assert getattr(settings, 'CELERY_TASK_REJECT_ON_WORKER_LOST', False) is True, (
        'CELERY_TASK_REJECT_ON_WORKER_LOST defaults to False, which undoes most '
        'of what acks_late is for'
    )


def test_the_effective_celery_config_carries_both():
    """Assert on the app's resolved config, not just the Django setting.

    `config_from_object(settings, namespace='CELERY')` is what maps one to the
    other, and a typo in the setting name would leave the Django value set and
    the Celery value at its default.
    """
    conf = _celery_app().conf
    assert conf.task_acks_late is True
    assert conf.task_reject_on_worker_lost is True


def test_a_wedged_task_cannot_hold_a_worker_slot_forever():
    conf = _celery_app().conf
    assert conf.task_soft_time_limit, 'no soft time limit is configured'
    assert conf.task_time_limit, 'no hard time limit is configured'
    assert conf.task_soft_time_limit < conf.task_time_limit, (
        'the soft limit must fire first so a task can log before it is killed'
    )


def test_workers_do_not_prefetch_work_they_may_lose():
    """ETA tasks are held in worker memory until their countdown elapses.

    auto_cancel_trip and compute_trip_actuals are both scheduled with a
    countdown. A worker that prefetches them and then crashes discards a
    scheduled cancellation, which is exactly the case acks_late is meant to
    cover.
    """
    conf = _celery_app().conf
    assert conf.worker_prefetch_multiplier == 1, (
        f'prefetch multiplier is {conf.worker_prefetch_multiplier}; a crashing '
        'worker discards everything it has prefetched'
    )


# ---------------------------------------------------------------------------
# The inventory guard
# ---------------------------------------------------------------------------

def _registered_project_tasks():
    """Task names belonging to this project, not Celery's built-ins."""
    app = _celery_app()
    app.loader.import_default_modules()
    return {
        name: task for name, task in app.tasks.items()
        if not name.startswith('celery.')
    }


def test_every_registered_task_is_durable_against_worker_loss():
    """The guard that matters for a task added tomorrow.

    A task may satisfy this globally or by setting acks_late itself. What it may
    not do is be acked early, which is what happens by accident.
    """
    tasks = _registered_project_tasks()
    assert tasks, 'no project tasks were discovered; the check would be vacuous'

    weak = []
    for name, task in sorted(tasks.items()):
        acks_late = getattr(task, 'acks_late', None)
        if acks_late is None:
            acks_late = _celery_app().conf.task_acks_late
        if not acks_late:
            weak.append(name)

    assert not weak, (
        'these tasks are acknowledged before they run, so a killed worker loses '
        'them silently:\n  ' + '\n  '.join(weak)
    )


def test_the_task_inventory_is_what_the_runbook_says_it_is():
    """Twelve tasks were inventoried. A thirteenth should be a deliberate act.

    Not a lock on the number -- it is a prompt to think about durability,
    retries and idempotency for anything new, and to update the Celery section
    of the launch runbook.
    """
    tasks = _registered_project_tasks()
    expected = {
        # The OTP SMS path. Registered under its module path rather than a short
        # name like its siblings -- found by this very check, which is the point.
        'base.utils.send_otp_via_sns',
        'auth_user.send_push_notification_task',
        'driver.block_expired_driver_licenses',
        'driver.sweep_stale_driver_presence',
        'payments.reconcile_stuck_payments',
        'payments.reconcile_stuck_withdrawals',
        'pricing.fare_shadow_sweep',
        # No explicit name= on the decorator, unlike its siblings in the same
        # module, so Celery registers the full dotted path. Nothing references
        # it by string, so this is a naming inconsistency rather than a bug.
        'servers.ride.tasks.auto_cancel_trip',
        'ride.compute_trip_actuals',
        'ride.dispatch_wave',
        'ride.issue_receipt_for_trip',
        'ride.persist_location_trail',
        'sos.dispatch_sos',
    }
    actual = set(tasks)
    added = actual - expected
    removed = expected - actual
    assert not added and not removed, (
        f'task inventory changed.\n  added: {sorted(added)}\n'
        f'  removed: {sorted(removed)}\n'
        'Update the Celery section of the launch runbook, and state the new '
        "task's trigger, idempotency and retry behaviour."
    )


# ---------------------------------------------------------------------------
# Beat topology
# ---------------------------------------------------------------------------

def test_every_scheduled_task_actually_exists():
    """A beat entry naming a task that is not registered fails silently forever."""
    tasks = _registered_project_tasks()
    missing = [
        (key, entry['task'])
        for key, entry in settings.CELERY_BEAT_SCHEDULE.items()
        if entry['task'] not in tasks
    ]
    assert not missing, (
        'beat schedules these names and no such task is registered: '
        f'{missing}'
    )


@pytest.mark.parametrize('financial_task', [
    'payments.reconcile_stuck_payments',
    'payments.reconcile_stuck_withdrawals',
])
def test_the_money_sweeps_are_scheduled(financial_task):
    """These are the backstops for a missed webhook or a stuck payout.

    If either stops being scheduled, money can strand with nothing looking for
    it, and the failure is invisible because nothing runs to report it.
    """
    scheduled = {e['task'] for e in settings.CELERY_BEAT_SCHEDULE.values()}
    assert financial_task in scheduled, (
        f'{financial_task} is not in the beat schedule; a missed webhook or a '
        'stuck payout would never be swept up'
    )
