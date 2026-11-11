"""Who holds what, and where.

    python manage.py list_admin_access
    python manage.py list_admin_access --format=csv

WHY IT EXISTS
-------------
`ops_role` was readable only from the database. No endpoint returned it, so
"who can approve a payout in Texas" could not be answered from the product
at all — which was found while trying to list the existing holders for the
migration, and is exactly the question an access model is for.

READ-ONLY AND SECRET-FREE. It prints no password, no TOTP secret and no
session token. It is meant to be run against production by the owner and
the output pasted into a review, so it must be safe to paste.

It shows BOTH the legacy `ops_role` and the new assignments during the
migration, so the two can be compared before anybody is moved.
"""

import csv
import sys

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from base.permissions import is_operator
from servers.admin_access.models import AdminAssignment
from servers.admin_access.services import (
    active_super_admin_ids,
    super_admin_shortfall,
)


class Command(BaseCommand):
    help = 'List every operator account, its legacy ops_role and its assignments.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--format', choices=['table', 'csv'], default='table',
            help='csv for pasting into a review or a spreadsheet.',
        )

    def handle(self, *args, **options):
        user_model = get_user_model()
        # Operators only, by the one definition of the word.
        candidates = user_model.objects.filter(
            is_active=True,
        ).order_by('id')
        operators = [u for u in candidates if is_operator(u)]

        assignments = {}
        for assignment in (
            AdminAssignment.objects
            .filter(is_active=True, role__is_active=True)
            .select_related('role', 'scope')
        ):
            assignments.setdefault(assignment.admin_id, []).append(
                f'{assignment.role.code}@{assignment.scope.code}'
            )

        rows = [
            {
                'id': user.id,
                'email': user.email or '',
                'phone': user.phone_number or '',
                'name': (user.full_name or '').strip(),
                'is_superuser': 'yes' if user.is_superuser else 'no',
                'legacy_ops_role': getattr(user, 'ops_role', '') or '',
                'assignments': '; '.join(sorted(assignments.get(user.id, []))) or '(none)',
            }
            for user in operators
        ]

        if options['format'] == 'csv':
            writer = csv.DictWriter(sys.stdout, fieldnames=list(rows[0]) if rows else
                                    ['id', 'email', 'phone', 'name', 'is_superuser',
                                     'legacy_ops_role', 'assignments'])
            writer.writeheader()
            writer.writerows(rows)
            return

        if not rows:
            self.stdout.write('No operator accounts found.')
            return

        self.stdout.write(self.style.MIGRATE_HEADING(
            f'{len(rows)} operator account(s)'
        ))
        self.stdout.write('')
        for row in rows:
            self.stdout.write(
                f"  id={row['id']:<5} {row['email'] or row['phone']:<36} "
                f"{row['name'][:22]:<24}"
            )
            self.stdout.write(
                f"        superuser={row['is_superuser']:<4} "
                f"legacy ops_role={row['legacy_ops_role'] or '-':<12} "
            )
            self.stdout.write(f"        assignments: {row['assignments']}")
            self.stdout.write('')

        # The Super Admin floor, reported rather than enforced here: this
        # command never changes anything.
        super_admins = len(active_super_admin_ids())
        shortfall = super_admin_shortfall()
        if shortfall:
            self.stdout.write(self.style.WARNING(
                f'{super_admins} Super Admin(s) assigned. At least '
                f'{super_admins + shortfall} are required — appoint '
                f'{shortfall} more named individual(s) before go-live.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'{super_admins} Super Admins assigned; the floor is met.'
            ))
