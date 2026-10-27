"""Create the three QA test accounts -- admin, rider, driver -- in QA only.

WHY THIS EXISTS
---------------
Automated tests can create a user, prove a login and delete it. That shows the
code works; it does not give the owner a way to open the product and use it.
Validating the application by hand needs accounts that persist, that the owner
controls, and whose passwords nobody else has seen.

`bootstrap_qa_operator` already does this for the operator. This does the same
for all three roles, and adds the two things the website now needs:

  * an **email address** on each account, because email+password sign-in is
    the journey being validated; and
  * a **verified** address, because `email_login` refuses an unverified one.
    These accounts are created by someone with shell access to the
    deployment, which is a stronger proof of control than clicking a link in
    an inbox, so marking them verified is correct rather than a shortcut.

HOW A PASSWORD GETS IN WITHOUT BEING EXPOSED
--------------------------------------------
It is read from the environment, which on QA means a Railway variable the
owner sets themselves. It is never printed, never logged, never defaulted and
never returned -- not masked, not at DEBUG. Nothing here writes a credential
to a terminal, a chat message or the repository.

If a password is not set for a role, that role is skipped with an explanation
rather than invented. A built-in default password is how a test account
becomes a production incident.

    QA_ADMIN_BOOTSTRAP_PHONE     QA_ADMIN_BOOTSTRAP_EMAIL     QA_ADMIN_BOOTSTRAP_PASSWORD
    QA_RIDER_BOOTSTRAP_PHONE     QA_RIDER_BOOTSTRAP_EMAIL     QA_RIDER_BOOTSTRAP_PASSWORD
    QA_DRIVER_BOOTSTRAP_PHONE    QA_DRIVER_BOOTSTRAP_EMAIL    QA_DRIVER_BOOTSTRAP_PASSWORD

SAFETY
------
It refuses to run in production -- refuses, not warns -- and an unlabelled
non-debug deployment is treated as production, which is the conservative
reading. It is idempotent: running it twice converges rather than creating a
second account, and an existing password is left alone unless
`--reset-password` is passed, so a redeploy cannot silently rotate a
credential somebody is using.

It never grants `is_superuser` to the rider or the driver, and it never
escalates an account that already exists with a different role: that is
reported and skipped, because a silent role change is the escalation every
other guard in this codebase exists to prevent.
"""

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from base.seeding import require_seedable_environment
from django.db import transaction
from django.utils import timezone

User = get_user_model()

MIN_PASSWORD_LENGTH = 12

#: (role, env prefix, human label, is_staff)
ROLES = (
    ('admin', 'QA_ADMIN', 'operator', True),
    ('rider', 'QA_RIDER', 'rider', False),
    ('driver', 'QA_DRIVER', 'driver', False),
)


class Command(BaseCommand):
    help = (
        'Create or converge the QA admin, rider and driver test accounts from '
        'environment variables. Never prints a password. QA only.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--reset-password',
            action='store_true',
            help='Also set each password from the environment. Without this, '
                 'an existing account keeps the password it has.',
        )
        parser.add_argument(
            '--role',
            choices=[r[0] for r in ROLES],
            help='Only this role. Default: all three.',
        )

    def handle(self, *args, **options):
        # One gate for every test-account seed; see base/seeding.py.
        require_seedable_environment('the QA bootstrap accounts')
        self._refuse_in_production()

        wanted = options.get('role')
        reset = options.get('reset_password', False)

        created, converged, skipped = [], [], []

        for role, prefix, label, is_staff in ROLES:
            if wanted and role != wanted:
                continue

            phone = (os.environ.get(f'{prefix}_BOOTSTRAP_PHONE') or '').strip()
            email = (os.environ.get(f'{prefix}_BOOTSTRAP_EMAIL') or '').strip().lower()
            password = os.environ.get(f'{prefix}_BOOTSTRAP_PASSWORD') or ''

            problem = self._validate(prefix, phone, password)
            if problem:
                skipped.append(f'{label}: {problem}')
                continue

            outcome = self._apply(role, phone, email, password, is_staff, reset)
            (created if outcome == 'created' else converged).append(label)

        self._report(created, converged, skipped, reset)

    # -- helpers ----------------------------------------------------------

    def _refuse_in_production(self):
        environment = (os.environ.get('ENVIRONMENT') or '').strip().lower()
        debug_env = (os.environ.get('DEBUG_ENV') or 'False').strip()
        looks_like_production = (
            environment == 'production'
            or (not environment and debug_env != 'True')
        )
        if looks_like_production:
            raise CommandError(
                'Refusing to run: this looks like production '
                f'(ENVIRONMENT={environment or "<unset>"!r}, '
                f'DEBUG_ENV={debug_env!r}).\n'
                'These are shared test accounts. A production account is '
                'created deliberately, with a credential nobody else has.'
            )

    def _validate(self, prefix, phone, password):
        if not phone:
            return f'{prefix}_BOOTSTRAP_PHONE is not set'
        if not password:
            return (f'{prefix}_BOOTSTRAP_PASSWORD is not set (no default: a '
                    'built-in password is how a test account becomes an '
                    'incident)')
        if len(password) < MIN_PASSWORD_LENGTH:
            return (f'{prefix}_BOOTSTRAP_PASSWORD is shorter than '
                    f'{MIN_PASSWORD_LENGTH} characters')
        return None

    def _apply(self, role, phone, email, password, is_staff, reset):
        with transaction.atomic():
            user, was_created = User.objects.get_or_create(
                phone_number=phone,
                defaults={
                    'username': phone,
                    'role': role,
                    'is_staff': is_staff,
                    'full_name': f'QA {role.title()}',
                    **({'ops_role': 'admin'} if role == 'admin' else {}),
                },
            )

            fields = []

            if not was_created and user.role != role:
                # Never silently re-role an existing account.
                raise CommandError(
                    f'{phone} already exists with role {user.role!r}, not '
                    f'{role!r}. Refusing to change it: a silent role change is '
                    'privilege escalation. Pick a different number, or change '
                    'the role deliberately in the Django admin.'
                )

            if email and (user.email or '').lower() != email:
                user.email = email
                fields.append('email')

            # Verified, because shell access to the deployment is a stronger
            # proof of control than an inbox. Without this, email_login
            # refuses the account and the owner cannot sign in at all.
            if user.email and not user.email_verified_at:
                user.email_verified_at = timezone.now()
                fields.append('email_verified_at')

            if is_staff and not user.is_staff:
                user.is_staff = True
                fields.append('is_staff')

            if was_created or reset:
                user.set_password(password)
                fields.append('password')

            if fields:
                user.save(update_fields=list(dict.fromkeys(fields)))

            if role == 'rider':
                from servers.rider.models import Rider
                Rider.objects.get_or_create(user_id=user)
            elif role == 'driver':
                from servers.driver.models import Driver
                Driver.objects.get_or_create(user_id=user)

        return 'created' if was_created else 'converged'

    def _report(self, created, converged, skipped, reset):
        if created:
            self.stdout.write(self.style.SUCCESS(
                f'Created: {", ".join(created)}'))
        if converged:
            note = 'password reset' if reset else 'password unchanged'
            self.stdout.write(
                f'Already existed, converged ({note}): {", ".join(converged)}')
        for line in skipped:
            self.stdout.write(self.style.WARNING(f'Skipped {line}'))

        if not created and not converged:
            raise CommandError(
                'No accounts were created. Set the environment variables '
                'listed in this command\'s help and run it again.'
            )

        self.stdout.write(
            '\nSign in at the operator console with the phone number and the '
            'password you set, or on the website with the email address and '
            'the same password. No password was printed by this command.'
        )
