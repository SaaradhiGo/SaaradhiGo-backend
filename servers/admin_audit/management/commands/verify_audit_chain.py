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

ROWS FROM BEFORE THE CHAIN
--------------------------
Rows written before migration 0003 carry no hash. They are reported as
"pre-chain, protected by anchor": a CHAIN_ANCHOR row (migration 0005)
records their count and a digest of their content, and this command
recomputes it. They are never rewritten. An unhashed row anywhere after the
anchor, or an anchor that disagrees with the rows, is a failure.

Run it on a schedule and alert on a non-zero exit.
"""

from django.core.management.base import BaseCommand

from servers.admin_audit.hashchain import inspect_chain
from servers.admin_audit.models import AdminAuditLog


class Command(BaseCommand):
    help = 'Verify the admin audit log hash chain. Non-zero exit if broken.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--limit', type=int, default=None,
            help='Check only the oldest N rows. Default: all of them.',
        )

    def handle(self, *args, **options):
        report = inspect_chain(AdminAuditLog.objects.all(),
                               limit=options['limit'])
        checked, problems = report['checked'], report['problems']
        pre_chain = report['pre_chain']

        if checked == 0:
            self.stdout.write('The audit log is empty. Nothing to verify.')
            return

        if pre_chain and pre_chain['anchor_id'] is not None:
            self.stdout.write(
                f"{pre_chain['rows']} pre-chain row(s) (ids "
                f"{pre_chain['first_id']}-{pre_chain['last_id']}) carry no "
                f"hash: protected by CHAIN_ANCHOR row {pre_chain['anchor_id']}, "
                f"digest recomputed and matching."
            )

        if not problems:
            hashed = checked - (pre_chain['rows'] if pre_chain else 0)
            self.stdout.write(self.style.SUCCESS(
                f'Audit chain intact: {checked} row(s) verified '
                f'({hashed} hashed). Every hashed row hashes to what it '
                f'stores and links to the row before it.'
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
