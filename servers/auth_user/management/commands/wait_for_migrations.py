"""Block until `manage.py migrate --check` would pass, then exit 0.

    python manage.py wait_for_migrations                # settings.MIGRATION_WAIT_TIMEOUT
    python manage.py wait_for_migrations --timeout 60   # override, in seconds
    python manage.py wait_for_migrations && celery -A base worker -B -l INFO

WHY A COMMAND WHEN THE WORKER ALREADY WAITS
-------------------------------------------
The Celery worker runs this same gate from its `worker_init` signal
(base/migration_gate.py), so nothing has to change in a start command for the
worker to be safe. This command is the same function for everything that is
not the worker: a deploy script that wants an explicit ordering step, a
one-off job container, a future start command that prefers the wait to be
visible in the service configuration. One implementation, two entry points;
the behaviour cannot drift between them.

Exit status is the gate's: 0 once the plan is empty, 1 when the timeout
elapses (the pending migrations are logged at CRITICAL first). A timeout of 0
skips the check and exits 0, exactly as it does in the worker.

WHY IT LIVES UNDER `servers.auth_user`
--------------------------------------
It is infrastructure and `base` would be the honest home, but `base` is not in
INSTALLED_APPS, so Django would never find a command there (the same reason
`check_storage` lives under `servers.driver`). `auth_user` already holds the
other operational commands (`cache_diagnose`, `bootstrap_qa_accounts`). Move it
to `base` if that ever becomes an app.
"""

from django.core.management.base import BaseCommand

from base.migration_gate import wait_for_migrations


class Command(BaseCommand):
    help = (
        'Wait until every migration has been applied (what `migrate --check` '
        'tests), retrying with backoff; exit 1 if that does not happen in time.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--timeout',
            type=float,
            default=None,
            help='Seconds to wait before giving up. Defaults to '
                 'settings.MIGRATION_WAIT_TIMEOUT; 0 skips the check.',
        )

    def handle(self, *args, **options):
        result = wait_for_migrations(timeout=options['timeout'])
        if result.skipped:
            self.stdout.write(self.style.WARNING(
                'Migration gate skipped (timeout is 0); nothing was checked.'
            ))
            return
        self.stdout.write(self.style.SUCCESS(
            f'Database is fully migrated '
            f'({result.attempts} attempt(s), waited {result.waited_seconds:.1f}s).'
        ))
