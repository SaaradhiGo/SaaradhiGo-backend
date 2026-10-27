"""Has the audit log been tampered with?

    python manage.py verify_audit_chain
    python manage.py verify_audit_chain --limit 5000

Every audit row stores a SHA-256 of (the previous row's hash + its own
canonical content), so the log is a chain. Editing a row, removing one, or
reordering two breaks every hash after that point.

WHY THIS COMMAND EXISTS
-----------------------
The append-only trigger stops mistakes and ordinary code. It cannot stop an
actor holding the application's own database credentials, because the
application connects as the OWNER and an owner can disable a trigger. The
real fix is a separate non-owner application role, tracked as a production
blocker in docs/admin/AUDIT.md §14.

Until then the chain means tampering cannot be SILENT: to rewrite history
undetectably somebody must recompute every subsequent hash, which is a
deliberate act and not something that happens by accident.

Run it on a schedule and alert on a non-zero exit.
"""

from django.core.management.base import BaseCommand

from servers.admin_audit.hashchain import verify_chain
from servers.admin_audit.models import AdminAuditLog


class Command(BaseCommand):
    help = 'Verify the admin audit log hash chain. Non-zero exit if broken.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--limit', type=int, default=None,
            help='Check only the oldest N rows. Default: all of them.',
        )

    def handle(self, *args, **options):
        checked, problems = verify_chain(
            AdminAuditLog.objects.all(), limit=options['limit'],
        )

        if checked == 0:
            self.stdout.write('The audit log is empty. Nothing to verify.')
            return

        if not problems:
            self.stdout.write(self.style.SUCCESS(
                f'Audit chain intact: {checked} row(s) verified. '
                f'Every row hashes to what it stores and links to the row '
                f'before it.'
            ))
            return

        self.stdout.write(self.style.ERROR(
            f'AUDIT CHAIN BROKEN: {len(problems)} problem(s) across '
            f'{checked} row(s).'
        ))
        self.stdout.write('')
        for problem in problems:
            self.stdout.write(self.style.ERROR(
                f"  row {problem['id']} ({problem['created_at']}): "
                f"{problem['issue']}"
            ))
            self.stdout.write(f"    {problem['detail']}")
        self.stdout.write('')
        self.stdout.write(
            'A break means the table was changed outside the application. '
            'Preserve a copy before investigating, and treat the rows after '
            'the first break as unverified.'
        )
        # Non-zero so a scheduled run can alert.
        raise SystemExit(1)
