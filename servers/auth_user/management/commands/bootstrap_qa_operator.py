"""Create the QA operator account, in QA only.

WHY THIS EXISTS
---------------
Three variables have existed in the QA environment for months --
QA_ADMIN_BOOTSTRAP_PHONE, QA_ADMIN_BOOTSTRAP_CODE, QA_ADMIN_BOOTSTRAP_PASSWORD --
and nothing in the codebase read any of them. They looked exactly like a working
admin-bootstrap mechanism, which is worse than having none: a previous run could
not rehearse a single operator workflow, and the reason was not that the
capability was missing but that it was imaginary.

So either the variables go, or they become real. They become real here, because a
QA operator account is what unblocks rehearsing KYC approval, stale-ride
resolution, SOS handling and payout investigation before a pilot.

SAFETY
------
This command refuses to run in production. Not "warns"; refuses. The guard is on
ENVIRONMENT, the same signal the settings boot guards use, and an unlabelled
DEBUG=False deployment is treated as production -- the conservative reading.

It has no default password and no default phone. If either variable is missing it
exits with an explanation rather than inventing a credential, because a default
admin password is how a test account becomes a production incident.

It never prints or logs the password, not even masked, not even at DEBUG.

It is idempotent: running it twice converges on the same account rather than
creating a second one or rotating the password unasked. Re-setting the password
requires --reset-password, so a redeploy cannot silently change a credential an
operator is already using.

Every run writes an AdminAuditLog row, so the existence of the account is
traceable to the moment it was created.

USAGE
-----
    railway run --service backend --environment QA \\
        python manage.py bootstrap_qa_operator

Requires QA_ADMIN_BOOTSTRAP_PHONE and QA_ADMIN_BOOTSTRAP_PASSWORD to be set in
that environment. QA_ADMIN_BOOTSTRAP_CODE is accepted and ignored; it is retained
only so the existing variable set does not need editing, and it is not a second
factor.
"""

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

User = get_user_model()


class Command(BaseCommand):
    help = 'Create or update the QA operator account. Refuses to run in production.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--reset-password',
            action='store_true',
            help='Also set the password to QA_ADMIN_BOOTSTRAP_PASSWORD. Without '
                 'this, an existing account keeps the password it has, so a '
                 'redeploy cannot silently rotate a credential in use.',
        )

    def handle(self, *args, **options):
        environment = (os.environ.get('ENVIRONMENT') or '').strip().lower()
        debug_env = (os.environ.get('DEBUG_ENV') or 'False').strip()

        # Treat an unlabelled non-debug deployment as production. Anything that
        # cannot prove it is not production is production.
        looks_like_production = (
            environment == 'production'
            or (not environment and debug_env != 'True')
        )
        if looks_like_production:
            raise CommandError(
                'Refusing to run: this looks like production '
                f'(ENVIRONMENT={environment or "<unset>"!r}, '
                f'DEBUG_ENV={debug_env!r}).\n'
                'This command exists to create a shared QA operator account. A '
                'production operator must be created deliberately, with a '
                'credential nobody else has and a second factor.'
            )

        phone = (os.environ.get('QA_ADMIN_BOOTSTRAP_PHONE') or '').strip()
        password = os.environ.get('QA_ADMIN_BOOTSTRAP_PASSWORD') or ''

        if not phone:
            raise CommandError(
                'QA_ADMIN_BOOTSTRAP_PHONE is not set. There is no default: an '
                'operator account has to be a deliberate choice.'
            )
        if not password:
            raise CommandError(
                'QA_ADMIN_BOOTSTRAP_PASSWORD is not set. There is no default -- a '
                'built-in admin password is how a test account becomes a '
                'production incident.'
            )
        if len(password) < 12:
            raise CommandError(
                'QA_ADMIN_BOOTSTRAP_PASSWORD is shorter than 12 characters. This '
                'account can approve KYC and inspect money; pick something longer.'
            )

        with transaction.atomic():
            user, created = User.objects.get_or_create(
                phone_number=phone,
                defaults={
                    'role': 'admin',
                    'is_staff': True,
                    'is_superuser': True,
                    # Explicit rather than relying on the field default. This is
                    # the QA operator, and it is meant to hold every authority so
                    # every workflow can be rehearsed from one account. A
                    # support-only or finance-only account is created from the
                    # Django admin by setting ops_role, not from here.
                    'ops_role': 'admin',
                },
            )

            changed = []
            if not created:
                # Converge, do not duplicate. An account that drifted out of the
                # operator role is repaired; its password is left alone unless
                # asked for explicitly.
                if user.role != 'admin':
                    user.role = 'admin'
                    changed.append('role')
                if not user.is_staff:
                    user.is_staff = True
                    changed.append('is_staff')
                if not user.is_superuser:
                    user.is_superuser = True
                    changed.append('is_superuser')
                if user.ops_role != 'admin':
                    user.ops_role = 'admin'
                    changed.append('ops_role')

            if created or options['reset_password']:
                user.set_password(password)
                changed.append('password')

            user.save()

        # The password is never written anywhere but the hasher.
        self.stdout.write(self.style.SUCCESS(
            ('Created' if created else 'Updated') + ' QA operator account '
            f'(user id {user.id}, role={user.role}, staff={user.is_staff}).'
        ))
        if changed:
            self.stdout.write(f'  fields set: {", ".join(sorted(set(changed)))}')
        if not created and not options['reset_password']:
            self.stdout.write(
                '  password left unchanged. Pass --reset-password to set it.'
            )
        self.stdout.write(
            '  sign in at the operations console /login with this phone number.'
        )

        try:
            from servers.admin_audit.models import AdminAuditLog
            AdminAuditLog.objects.create(
                actor=None,
                actor_label='bootstrap_qa_operator (management command)',
                action='qa_operator_bootstrapped',
                target_type='User',
                target_id=str(user.id),
                before={},
                # No credential, and no phone number: the user id is enough to
                # find the account, and an audit log is retained and searchable.
                after={
                    'created': created,
                    'fields_set': sorted(set(changed)),
                    'environment': environment or 'unlabelled',
                },
                reason='QA operator bootstrap',
            )
        except Exception as exc:  # noqa: BLE001
            # An audit failure must not leave the operator without an account, but
            # it must be visible.
            self.stderr.write(
                self.style.WARNING(f'  audit row could not be written: {exc}')
            )
