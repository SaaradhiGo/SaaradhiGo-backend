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

IT CREATES; IT NEVER CHANGES (owner, 2026-10-28)
------------------------------------------------
    bootstrap_qa_accounts is idempotent and never changes existing accounts
    (passwords, assignments, break-glass, MFA). It may only create missing
    seed accounts.

Railway runs this on every QA boot. A command that runs unattended on every
deploy and also edits accounts is a command that will, one day, edit the
wrong thing at the wrong moment with nobody watching: re-verify an address
somebody deliberately un-verified, hand `is_staff` back to an account that
was demoted, rotate a password an operator is signed in with. So an
account that already exists (matched by phone number) is NOT WRITTEN, in
any field, by any flag: no email, no verification stamp, no authority, no
password, no profile row. The command compares what is there with what the
environment says and REPORTS the drift -- "exists, left alone; differs in:
email, is_staff" -- so the owner can decide, and change it deliberately
with the tool for that change (`set_operator_password` for an operator's
password, the Django admin or the website's own flows for the rest).

There is no `--reset-password`. A flag that rewrites a credential on an
existing account is the thing the ruling rules out, and a flag that only a
person would pass still ships on a command a deploy runs.

A missing account is created complete: phone, email (verified), role,
`is_staff` for the operator, the password from the environment, and the
Rider or Driver profile row. The three accounts are independent: one that
exists is reported while the others are created.

It refuses to run in production -- refuses, not warns -- and an unlabelled
non-debug deployment is treated as production, which is the conservative
reading. It never grants `is_superuser` to anyone.

WHO RAN IT
----------
The operator account it creates is an access row, so the run names an
operator: `--actor-email` when a person runs this, or `QA_BOOTSTRAP_ACTOR_EMAIL`
when the QA start command does (Railway runs this on every boot with nobody
at the keyboard). Either way the address must be an active operator and
never the break-glass account (`admin_audit.actor_option`). On an EMPTY
database there is no operator to name and this is one of the three commands
that can create the first; then, and only then, it runs without one and the
rows are labelled `system:genesis`. A run that creates nothing writes
nothing: no account, no profile row, no audit row.
"""

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from base.seeding import require_seedable_environment
from django.db import transaction
from django.utils import timezone

from servers.admin_audit.actor_option import (
    add_actor_argument,
    attributed,
    require_actor,
)

User = get_user_model()

COMMAND = 'bootstrap_qa_accounts'
MIN_PASSWORD_LENGTH = 12

#: (role, env prefix, human label, is_staff)
ROLES = (
    ('admin', 'QA_ADMIN', 'operator', True),
    ('rider', 'QA_RIDER', 'rider', False),
    ('driver', 'QA_DRIVER', 'driver', False),
)


class Command(BaseCommand):
    help = (
        'Create the QA admin, rider and driver test accounts that are missing, '
        'from environment variables. Existing accounts are never changed, only '
        'reported. Never prints a password. QA only.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--role',
            choices=[r[0] for r in ROLES],
            help='Only this role. Default: all three.',
        )
        add_actor_argument(
            parser,
            help='Email of the operator running this; goes on the audit row. '
                 'The QA start command sets QA_BOOTSTRAP_ACTOR_EMAIL instead. '
                 'May be omitted only when no operator exists yet.')

    def handle(self, *args, **options):
        # One gate for every test-account seed; see base/seeding.py.
        require_seedable_environment('the QA bootstrap accounts')
        self._refuse_in_production()

        # Before the first write. None only on an empty database.
        actor = require_actor(options, COMMAND, unattended=True, allow_genesis=True)

        wanted = options.get('role')

        created, existing, skipped = [], [], []

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

            # Looked up, never written. The comparison is the whole of what
            # happens to an account that exists.
            user = User.objects.filter(phone_number=phone).first()
            if user is not None:
                drift = self._drift(user, role, email, is_staff)
                existing.append((label, drift))
                continue

            # Creating the operator account appends an audit row by itself
            # (`admin_audit.access_audit`); the context puts this command's
            # name and actor on it. Rider and driver accounts are not
            # operator-shaped and write no audit row.
            with attributed(
                actor, COMMAND,
                reason=f'QA bootstrap: the {label} test account.',
                extra_after={'genesis': actor is None},
            ):
                self._create(role, phone, email, password, is_staff)
            created.append(label)

        self._report(created, existing, skipped)

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

    def _create(self, role, phone, email, password, is_staff):
        """The one write path: a complete account that was not there."""
        with transaction.atomic():
            user = User(
                phone_number=phone,
                username=phone,
                email=email or None,
                # Verified, because shell access to the deployment is a
                # stronger proof of control than an inbox. Without this,
                # email_login refuses the account and the owner cannot sign
                # in on the website at all.
                email_verified_at=timezone.now() if email else None,
                role=role,
                is_staff=is_staff,
                is_superuser=False,
                full_name=f'QA {role.title()}',
                **({'ops_role': 'admin'} if role == 'admin' else {}),
            )
            user.set_password(password)
            user.save()

            if role == 'rider':
                from servers.rider.models import Rider
                Rider.objects.create(user_id=user)
            elif role == 'driver':
                from servers.driver.models import Driver
                Driver.objects.create(user_id=user)
        return user

    def _drift(self, user, role, email, is_staff):
        """How an existing account differs from what the environment
        describes. Reported, never corrected."""
        drift = []
        if user.role != role:
            drift.append(f'role (is {user.role!r}, not {role!r})')
        if email and (user.email or '').lower() != email:
            drift.append('email')
        if user.email and not user.email_verified_at:
            drift.append('verified')
        if is_staff and not user.is_staff:
            drift.append('is_staff')
        if not user.is_active:
            drift.append('inactive')
        if role == 'rider':
            from servers.rider.models import Rider
            if not Rider.objects.filter(user_id=user).exists():
                drift.append('rider profile row missing')
        elif role == 'driver':
            from servers.driver.models import Driver
            if not Driver.objects.filter(user_id=user).exists():
                drift.append('driver profile row missing')
        return drift

    def _report(self, created, existing, skipped):
        if created:
            self.stdout.write(self.style.SUCCESS(
                f'Created: {", ".join(created)}'))
        for label, drift in existing:
            if drift:
                self.stdout.write(self.style.WARNING(
                    f'{label}: exists, left alone; differs in: '
                    f'{", ".join(drift)}. This command never changes an '
                    f'existing account; change it deliberately with the '
                    f'tool for that change.'))
            else:
                self.stdout.write(f'{label}: exists, left alone.')
        for line in skipped:
            self.stdout.write(self.style.WARNING(f'Skipped {line}'))

        if not created and not existing:
            raise CommandError(
                'No accounts were created. Set the environment variables '
                'listed in this command\'s help and run it again.'
            )

        self.stdout.write(
            '\nSign in at the operator console with the phone number and the '
            'password you set, or on the website with the email address and '
            'the same password. No password was printed by this command.'
        )
