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

CREATE-IF-MISSING ONLY (owner, AUDIT 27.40)
--------------------------------------------
It creates the operator when no account has the phone number and NEVER writes
one that exists: not the password, not `role`, `is_staff`, `is_superuser` or
`ops_role` (it used to "repair" those, and to reset the password with
`--reset-password`; both are gone). An existing account is reported with how it
differs, and changed on purpose with the tool for that change
(`set_operator_password` for the password). The write goes through
`base.seeding.get_or_create_account`.

Every run writes an AdminAuditLog row, so the existence of the account is
traceable to the moment it was created. When the run grants or repairs
authority (`is_staff`, `is_superuser`, `role`, `ops_role`) that row is the
one the access models write for themselves (`admin_audit.access_audit`),
named `qa_operator_bootstrapped` through the audit context; when the run
changes no authority -- a repeat, or a password reset -- this command
writes the run record itself, because the receivers only record real
changes and a run is still worth a line.

WHO RAN IT
----------
The row names an operator: `--actor-email` when a person runs this, or the
`QA_BOOTSTRAP_ACTOR_EMAIL` variable when a deploy does, either way an active
operator and never the break-glass account (`admin_audit.actor_option`). On
an EMPTY database there is no operator to name and this is one of the three
commands that can create the first; then, and only then, it runs without one
and the row says so.

USAGE
-----
    railway run --service backend --environment QA \\
        python manage.py bootstrap_qa_operator --actor-email you@saaradhigo.com

Requires QA_ADMIN_BOOTSTRAP_PHONE and QA_ADMIN_BOOTSTRAP_PASSWORD to be set in
that environment. QA_ADMIN_BOOTSTRAP_CODE is accepted and ignored; it is retained
only so the existing variable set does not need editing, and it is not a second
factor.
"""

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from base.seeding import get_or_create_account, require_seedable_environment
from django.db import transaction

from servers.admin_audit.actor_option import (
    add_actor_argument,
    attributed,
    require_actor,
)
from servers.admin_audit.services import record_admin_action

User = get_user_model()

COMMAND = 'bootstrap_qa_operator'
ACTION = 'qa_operator_bootstrapped'


class Command(BaseCommand):
    help = ('Create the QA operator account if it is missing; never changes an existing '
            'one. Refuses to run in production.')

    def add_arguments(self, parser):
        add_actor_argument(
            parser,
            help='Email of the operator running this; goes on the audit row. '
                 'A deploy sets QA_BOOTSTRAP_ACTOR_EMAIL instead. May be '
                 'omitted only when no operator exists yet.')

    def handle(self, *args, **options):
        require_seedable_environment('the QA operator account')
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

        # Before the first write. None only on an empty database.
        actor = require_actor(options, COMMAND, unattended=True, allow_genesis=True)

        # Known before the write so the audit row can say so.
        created = not User.objects.filter(phone_number=phone).exists()
        changed = []
        # `fields_set` is filled in below, but the dict is shared with the
        # context now so the structural row carries it when it is written.
        run_facts = {
            'created': created,
            'fields_set': [],
            'environment': environment or 'unlabelled',
            'genesis': actor is None,
        }

        with attributed(actor, COMMAND, action=ACTION,
                        reason='QA operator bootstrap',
                        extra_after=run_facts) as audit:
            self._converge(phone=phone, password=password, created=created,
                           changed=changed, options=options, audit=audit,
                           environment=environment)

    @staticmethod
    def _drift(user):
        """How an existing operator differs from the bootstrap's. Reported only."""
        drift = []
        if user.role != 'admin':
            drift.append('role')
        if not user.is_staff:
            drift.append('is_staff')
        if not user.is_superuser:
            drift.append('is_superuser')
        if user.ops_role != 'admin':
            drift.append('ops_role')
        return drift

    def _converge(self, *, phone, password, created, changed, options,
                  audit, environment):
        """Create or repair the account, then make sure the run left a row."""
        with transaction.atomic():
            # A new account only. An existing one comes back untouched and
            # its drift (below) is reported, never corrected.
            user, made = get_or_create_account(
                User,
                lookup={'phone_number': phone},
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
                password=password,
            )
            created = made
            if made:
                changed.append('password')
            else:
                changed.extend(self._drift(user))

        # The password is never written anywhere but the hasher.
        if created:
            self.stdout.write(self.style.SUCCESS(
                f'Created QA operator account (user id {user.id}, role={user.role}, '
                f'staff={user.is_staff}).'
            ))
        else:
            self.stdout.write(self.style.WARNING(
                f'QA operator account exists (user id {user.id}); left alone.'
            ))
            if changed:
                self.stdout.write(
                    f'  differs from the bootstrap in: {", ".join(sorted(set(changed)))}. '
                    f'This command never changes an existing account; change it on '
                    f'purpose (set_operator_password for the password).'
                )
        self.stdout.write(
            '  sign in at the operations console /login with this phone number.'
        )

        if audit.rows:
            # The authority change wrote its own row, named and annotated
            # through the context above. A second one would say the same.
            return

        # No credential, and no phone number: the user id is enough to find
        # the account, and an audit log is retained and searchable. The actor
        # comes from the context; `record_admin_action` never raises, so an
        # audit failure cannot leave the operator without an account -- but
        # it must be visible.
        row = record_admin_action(
            None,
            action=ACTION,
            target_type='User',
            target_id=user.id,
            before={},
            after={
                'created': created,
                'fields_set': sorted(set(changed)),
                'environment': environment or 'unlabelled',
                'genesis': audit.actor is None,
            },
            reason='QA operator bootstrap',
        )
        if row is None:
            self.stderr.write(
                self.style.WARNING('  audit row could not be written; see the log')
            )
