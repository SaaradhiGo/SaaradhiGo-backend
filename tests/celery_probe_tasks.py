"""Probe tasks for the kill-the-worker matrix. Not product code.

WHY A PROBE IS NEEDED
---------------------
The claim under test is a BROKER contract: with `task_acks_late` and
`task_reject_on_worker_lost`, a message reserved by a worker that is then SIGKILLed
is redelivered rather than silently lost.

Demonstrating that requires killing a worker *while it is executing a task*. Every
real task in this project finishes in milliseconds -- `auto_cancel_trip` takes a row
lock and returns, `persist_location_trail` is a no-op when the trail is disabled --
so there is no reliable window in which to kill one. A test that tried would pass or
fail on scheduler luck.

So these tasks exist to hold the window open. They are registered through the same
Celery app and therefore inherit the same global acknowledgement settings, which is
the thing being proven.

WHAT THIS PROVES AND WHAT IT DOES NOT
-------------------------------------
Proves: the configuration really does redeliver a task whose worker died mid-execution,
against a real Redis broker and a real worker process.

Does NOT prove: that any particular product task is safe to run twice. That is a
different property, and it is tested directly against the real tasks in
`test_celery_worker_loss.py`.

The distinction matters. Conflating them is how a green suite ends up asserting less
than it appears to.
"""

import os
import time

from celery import shared_task

# A run log on disk rather than in the database: the point is to observe execution
# across a process kill, and a killed process may leave a transaction unflushed.
RUN_LOG = os.environ.get('CELERY_PROBE_RUN_LOG', '')


def _record(marker):
    if not RUN_LOG:
        return
    with open(RUN_LOG, 'a', encoding='utf-8') as fh:
        fh.write(marker + '\n')
        fh.flush()
        os.fsync(fh.fileno())


@shared_task(name='probe.slow_task')
def slow_task(token, seconds=8.0):
    """Start, sleep, finish. Killed between the first two markers.

    Two markers rather than one, because "started twice" and "completed twice" are
    different facts and the matrix needs to tell them apart.
    """
    _record(f'start:{token}')
    time.sleep(float(seconds))
    _record(f'finish:{token}')
    return token


@shared_task(name='probe.quick_task')
def quick_task(token):
    """Control: a task that completes long before any kill could land."""
    _record(f'quick:{token}')
    return token
