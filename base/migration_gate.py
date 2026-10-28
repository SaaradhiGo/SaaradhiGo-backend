"""Hold the Celery worker at startup until the database has been migrated.

THE RACE THIS CLOSES
--------------------
On Railway the backend service runs `python manage.py migrate --noinput` as its
pre-deploy command. The celery service does not: its start command is just
`celery -A base worker -B -l INFO --concurrency 2`, and nothing orders it after
the backend. Both services are built from the same commit and deploy at roughly
the same time, so whenever a release carries a migration there is a window in
which NEW worker code is running against the OLD schema. In that window a task
that reads a column the migration adds raises `ProgrammingError`, and because
every task here runs with `acks_late`, the message is redelivered -- into the
same broken worker -- until the migration lands. The failure is loud in the logs
and silent to the product: a trip that should have been auto-cancelled is not,
and a payout reconciliation that should have run did not.

The fix is the same one `migrate --check` offers a shell script, applied inside
the worker: do not consume until the migration plan is empty.

WHY THE CHECK IS INSIDE THE PROCESS AND NOT IN THE START COMMAND
----------------------------------------------------------------
The start command lives in Railway's service configuration, not in this
repository. A wrapper script would have to be set there by hand, per
environment, and would be lost the day someone resets the service. A Celery
signal handler ships with the code and runs wherever that code runs: Railway,
docker compose, a developer's laptop. The management command
(`manage.py wait_for_migrations`) exposes the same function for scripts and
for an explicit start command, should one ever be written -- it is the same
gate, not a second implementation.

WHICH SIGNAL, AND WHY IT IS SAFE WITH `-B`
------------------------------------------
`worker_init`. In Celery 5 the worker's `__init__` runs, in order: the loader's
`init_worker()` (where the Django fixup calls `django.setup()`), `celeryd_init`,
logging setup, then `worker_init` -- and only after all of that does
`WorkController.start()` run the bootsteps that create the pool, the embedded
beat and the consumer. So `worker_init` fires in the main process, after Django
and logging are ready, and strictly before any task can be received or any
beat tick can be scheduled. Embedded beat (`-B`) is a bootstep too, so the gate
holds it as well. `celeryd_init` would also work; `worker_init` was chosen
because it is the last hook before consumption and because the Django fixup
itself uses it for its own worker setup, so this is the established place.

Celery's signal dispatcher swallows `Exception` from a receiver and logs it. It
does not swallow `SystemExit`, which is exactly why a timeout raises
`SystemExit(1)` rather than any exception class: a gate that can be logged past
is not a gate.

WHAT "MIGRATED" MEANS
---------------------
Exactly what `migrate --check` tests: `MigrationExecutor(connection)
.migration_plan(loader.graph.leaf_nodes())` is empty. A database that cannot be
reached yet (`OperationalError`) is treated as "not ready", not as an error --
on a fresh environment the worker often starts before the database accepts
connections, and that is a wait, not a fault.

THE OFF SWITCH, AND WHY IT EXISTS
---------------------------------
`MIGRATION_WAIT_TIMEOUT=0` skips the gate entirely, with one WARNING line. It
exists for a situation the gate cannot tell apart from the failure it guards
against: a migration that is deliberately left unapplied for a while (a long
data backfill to be run off-hours, or a release rolled back while its migration
is kept). In that case the plan is legitimately non-empty and the worker must
still run, and the person making that call needs a way to say so without a
code deploy. It is an environment variable so that the decision is visible in
the service configuration, and it is a WARNING in the log so that it is visible
in the worker too. It is not set anywhere by default.

The default timeout is 300 seconds: comfortably longer than any migration this
database has seen, short enough that a worker stuck behind a failed migration
exits (and, under Railway's restart policy, retries) instead of looking healthy
while doing nothing. On timeout the pending migrations are named at CRITICAL,
so the log says what to apply rather than only that something is missing.
"""

import logging
import time
from dataclasses import dataclass

from django.conf import settings
from django.db import OperationalError, connection

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_INITIAL_DELAY_SECONDS = 1.0
DEFAULT_MAX_DELAY_SECONDS = 15.0
MAX_NAMED_PENDING = 20


@dataclass(frozen=True)
class GateResult:
    """What the gate did, for the caller's log line or stdout."""

    attempts: int
    waited_seconds: float
    skipped: bool = False


def pending_migrations():
    """Names of the migrations `migrate` would still apply, in plan order.

    An empty list means `manage.py migrate --check` would exit 0. Raises
    `OperationalError` when the database cannot be reached; the caller decides
    whether that is a wait or a failure.
    """
    # Imported here rather than at module level: `base/__init__.py` imports
    # `base.celery`, which imports this module, so anything imported at the top
    # here is loaded by every `import base` -- including the settings module.
    # The executor pulls in the whole migrations framework, which is a lot to
    # pay for at settings-import time when only the worker and one command use it.
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    return [f'{migration.app_label}.{migration.name}' for migration, _backwards in plan]


def _configured_timeout():
    return getattr(settings, 'MIGRATION_WAIT_TIMEOUT', DEFAULT_TIMEOUT_SECONDS)


def _close_connection():
    # A connection opened by the check in the worker's MAIN process must not be
    # inherited by the prefork children or the embedded beat; the Django fixup
    # closes inherited connections at fork, and this keeps the parent clean too.
    try:
        connection.close()
    except Exception:  # noqa: BLE001 - nothing useful to do with a close failure
        pass


def wait_for_migrations(timeout=None, initial_delay=DEFAULT_INITIAL_DELAY_SECONDS,
                        max_delay=DEFAULT_MAX_DELAY_SECONDS, logger=logger):
    """Block until the migration plan is empty; exit the process if it never is.

    `timeout` is in seconds and defaults to `settings.MIGRATION_WAIT_TIMEOUT`.
    A timeout of zero (or less) skips the gate without touching the database.

    Retries with exponential backoff from `initial_delay`, doubling up to
    `max_delay`, and never sleeping past the deadline. Returns a `GateResult`
    when the plan is empty. On timeout logs the pending migrations at CRITICAL
    and raises `SystemExit(1)` -- see the module docstring for why it is
    `SystemExit` and not an exception.
    """
    if timeout is None:
        timeout = _configured_timeout()
    timeout = float(timeout)

    if timeout <= 0:
        logger.warning(
            'Migration gate DISABLED (MIGRATION_WAIT_TIMEOUT=%s): the worker will '
            'consume tasks whether or not the database is migrated.', timeout,
        )
        return GateResult(attempts=0, waited_seconds=0.0, skipped=True)

    started = time.monotonic()
    delay = float(initial_delay)
    attempts = 0
    pending = None
    try:
        while True:
            attempts += 1
            try:
                pending = pending_migrations()
                not_ready = f'{len(pending)} pending migration(s)' if pending else None
            except OperationalError as exc:
                pending = None
                not_ready = f'database not reachable ({exc})'

            if not_ready is None:
                waited = time.monotonic() - started
                logger.info(
                    'Migration gate passed: database is fully migrated '
                    '(%d attempt(s), waited %.1fs).', attempts, waited,
                    extra={'event': 'migration_gate_passed',
                           'attempts': attempts, 'waited_seconds': round(waited, 1)},
                )
                return GateResult(attempts=attempts, waited_seconds=waited)

            elapsed = time.monotonic() - started
            if elapsed >= timeout:
                break

            sleep_for = min(delay, timeout - elapsed)
            logger.warning(
                'Migration gate: %s; retrying in %.1fs '
                '(attempt %d, %.0fs of %.0fs elapsed).',
                not_ready, sleep_for, attempts, elapsed, timeout,
                extra={'event': 'migration_gate_waiting', 'attempts': attempts,
                       'pending': pending or []},
            )
            time.sleep(sleep_for)
            delay = min(delay * 2, float(max_delay))
    finally:
        _close_connection()

    if pending:
        # Name what to apply. A deploy has one to three pending; an empty
        # database has every migration in the project, which is not worth a
        # log line the width of the history -- the first twenty say enough.
        shown = ', '.join(pending[:MAX_NAMED_PENDING])
        more = len(pending) - MAX_NAMED_PENDING
        detail = f'{len(pending)} still pending: {shown}'
        if more > 0:
            detail += f' and {more} more'
    else:
        detail = 'database never became reachable'
    logger.critical(
        'Migration gate TIMED OUT after %.0fs and %d attempt(s); %s. Refusing to '
        'consume tasks against an unmigrated database. Apply the migrations '
        '(python manage.py migrate) or, if they are pending on purpose, set '
        'MIGRATION_WAIT_TIMEOUT=0 to disable the gate.',
        timeout, attempts, detail,
        extra={'event': 'migration_gate_timeout', 'attempts': attempts,
               'pending': pending or []},
    )
    raise SystemExit(1)


def hold_worker_until_migrated(sender=None, **kwargs):
    """`worker_init` receiver. Connected in `base/celery.py`.

    Keyword-only in effect: Celery calls receivers with `sender=` plus
    `signal=`, and anything else the sender chooses to pass.
    """
    wait_for_migrations()
