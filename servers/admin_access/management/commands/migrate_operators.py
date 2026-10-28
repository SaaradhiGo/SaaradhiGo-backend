"""Move named operators onto the new access model.

    python manage.py migrate_operators                 # dry run, changes nothing
    python manage.py migrate_operators --apply         # writes
    python manage.py migrate_operators --plan qa       # the QA mapping
    python manage.py migrate_operators --plan file.json

DRY RUN IS THE DEFAULT, AND THAT IS THE POINT
---------------------------------------------
This grants human beings authority over money and identity documents. The
default has to be the harmless one, so that a half-remembered command run
against the wrong environment prints a table instead of handing somebody
the payout queue. `--apply` is the deliberate act.

IT NEVER INVENTS A PERSON
-------------------------
Every assignment comes from a named plan. An account named in the plan that
does not exist is REPORTED, not created: an operator account appearing
because a migration script was run is exactly the kind of thing an access
review is supposed to catch.

IT IS IDEMPOTENT
----------------
Re-running creates nothing new. So it is safe to leave in a deploy path
while the migration is in progress, and safe to run twice when you lose
track of whether it took.

WHAT IT DOES NOT DO
-------------------
It does not revoke anything. Removing authority is `revoke_assignment`,
which enforces the Super Admin floor and wants a reason. A migration that
could silently remove access would be a worse instrument than the problem
it solves.

EVERY LINE IT APPLIES IS AUDITED, AND NAMES WHO APPLIED IT
-----------------------------------------------------------
On 2026-10-27 this command applied the QA plan and the audit log recorded
nothing, because audit rows were something views wrote and this is not a
view. The access models now audit themselves (`admin_audit.access_audit`),
so each assignment and each break-glass flag this writes appends a row
whatever happens here. What this command adds is the WHO: `--apply`
requires `--actor-email`, the address of the operator running it, and
every row names them. An apply with no name, or a name that is not an
active operator, is refused before anything is written -- see
`admin_audit.actor_option` for the rule. A dry run needs no name because
it writes nothing.
"""

import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from servers.admin_access.models import AdminAssignment, AdminRole, GeoNode
from servers.admin_access.roles import SUPER_ADMIN
from servers.admin_access.services import super_admin_shortfall
from servers.admin_audit.actor_option import (
    add_actor_argument,
    attributed,
    require_actor,
)
from servers.admin_audit.actor_option import resolve_actor as _resolve_actor
from servers.admin_audit.context import audit_context

COMMAND = 'migrate_operators'
#: Label on a DRY RUN's context. A dry run writes nothing, so no row ever
#: carries it; it is here so a stray write in a dry run would at least be
#: attributed to the command.
ACTOR_LABEL = 'migrate_operators (management command, dry run)'

#: The QA mapping, from the owner's decisions of 2026-10-26.
#:
#: `break_glass` marks the shared emergency account. It is NOT given a
#: Super Admin assignment: it is excluded from the floor by design, and an
#: emergency account that permanently holds everything is not a key under
#: glass, it is a second administrator nobody is accountable for.
QA_PLAN = [
    {
        'email': 'srini@saaradhigo.com',
        'role': SUPER_ADMIN,
        'scope': 'GLOBAL',
        'note': 'Owner. Named individual, per the Super Admin rule.',
    },
    {
        'email': 'admin@saaradhigo.com',
        'break_glass': True,
        'note': 'Shared emergency account. Credentials offline; sign-in '
                'demands a reason and alerts every Super Admin.',
    },
    {
        'email': 'qa.admin@deeptrics.com',
        'role': 'city_ops_manager',
        'scope': 'IN-TG-HYD',
        'note': 'QA account. Deliberately NOT Super Admin.',
    },
]


def load_plan(source):
    """`QA_PLAN` for "qa", otherwise the JSON file at `source`.

    Shared with `record_retroactive_access_audit`, which must describe the
    same lines this command applied.
    """
    if source == 'qa':
        return QA_PLAN
    try:
        with open(source, encoding='utf-8') as handle:
            return json.load(handle)
    except OSError as exc:
        raise CommandError(f'Cannot read plan {source!r}: {exc}') from exc


def resolve_actor(email):
    """The operator account behind `--actor-email`, or None when not given.

    Kept for callers that imported it from here; the rule itself lives in
    `admin_audit.actor_option.resolve_actor` (active, operator, not the
    break-glass account) so every command refuses the same addresses.
    """
    if not email:
        return None
    return _resolve_actor(email)


class Command(BaseCommand):
    help = 'Assign named operators to roles and scopes. Dry run by default.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Actually write. Without this, nothing changes.',
        )
        parser.add_argument(
            '--plan', default='qa',
            help='"qa" for the built-in QA mapping, or a path to a JSON file '
                 'of the same shape.',
        )
        parser.add_argument(
            '--allow-production', action='store_true',
            help='Required to apply the BUILT-IN qa plan outside QA. A '
                 'production migration should use an explicit plan file.',
        )
        add_actor_argument(
            parser,
            help='Email of the operator running this. Required with --apply; '
                 'every audit row the apply writes names them. A dry run '
                 'does not need it.',
        )

    def handle(self, *args, **options):
        plan = load_plan(options['plan'])
        apply_changes = options['apply']
        reason = f'Operator migration, plan {options["plan"]!r}.'

        if not apply_changes:
            with audit_context(actor_label=ACTOR_LABEL, reason=reason):
                rows, problems = self._apply_plan(plan, apply_changes=False)
            self._report(rows, problems, apply_changes=False)
            return

        self._guard_environment(options)
        # Before the first write: a nameless apply is refused outright.
        actor = require_actor(options, COMMAND)
        with attributed(actor, COMMAND, reason=reason):
            rows, problems = self._apply_plan(plan, apply_changes)

        self._report(rows, problems, apply_changes)

        if problems and apply_changes:
            raise CommandError(
                f'{len(problems)} account(s) in the plan could not be '
                f'migrated. Nothing about them was changed; the rest were '
                f'applied. Fix the plan or create the accounts, then re-run.'
            )

    # -- internals -----------------------------------------------------

    def _apply_plan(self, plan, apply_changes):
        user_model = get_user_model()
        rows, problems = [], []

        for entry in plan:
            email = entry['email']
            user = user_model.objects.filter(email__iexact=email).first()
            if user is None:
                problems.append(
                    f'{email}: no such account. Not created -- an operator '
                    f'account must not appear because a script ran.'
                )
                continue

            actions = []

            if entry.get('break_glass'):
                if user.is_break_glass:
                    actions.append('already break-glass')
                else:
                    actions.append('SET break-glass')
                    if apply_changes:
                        user.is_break_glass = True
                        user.save(update_fields=['is_break_glass'])

            role_code = entry.get('role')
            if role_code:
                outcome = self._assign(
                    user, role_code, entry['scope'], entry.get('note', ''),
                    apply_changes,
                )
                actions.append(outcome)

            rows.append((email, user.pk, '; '.join(actions) or 'nothing to do'))

        return rows, problems

    def _guard_environment(self, options):
        """Refuse to apply the built-in QA plan to production by accident."""
        from django.conf import settings

        is_prod = getattr(settings, 'IS_PRODUCTION', False)
        if is_prod and options['plan'] == 'qa' and not options['allow_production']:
            raise CommandError(
                'Refusing to apply the built-in QA plan in production. '
                'A production migration runs from an explicit plan file, '
                'reviewed against `list_admin_access` output first.'
            )

    def _assign(self, user, role_code, scope_code, note, apply_changes):
        try:
            role = AdminRole.objects.get(code=role_code)
        except AdminRole.DoesNotExist:
            return f'ROLE {role_code} MISSING -- run seed_admin_access'
        try:
            scope = GeoNode.objects.get(code=scope_code)
        except GeoNode.DoesNotExist:
            return f'SCOPE {scope_code} MISSING -- run seed_admin_access'

        existing = AdminAssignment.objects.filter(
            admin=user, role=role, scope=scope,
        ).first()
        if existing and existing.is_active:
            return f'already {role_code}@{scope_code}'

        if not apply_changes:
            return f'WOULD ASSIGN {role_code}@{scope_code}'

        with transaction.atomic():
            if existing:
                existing.is_active = True
                existing.save(update_fields=['is_active'])
            else:
                AdminAssignment.objects.create(
                    admin=user, role=role, scope=scope,
                    reason=note or 'Operator migration.',
                )
        return f'ASSIGNED {role_code}@{scope_code}'

    def _report(self, rows, problems, apply_changes):
        mode = 'APPLIED' if apply_changes else 'DRY RUN -- nothing was changed'
        self.stdout.write(self.style.MIGRATE_HEADING(f'Operator migration: {mode}'))
        self.stdout.write('')
        for email, pk, actions in rows:
            self.stdout.write(f'  {email:<34} id={pk:<5} {actions}')

        if problems:
            self.stdout.write('')
            for problem in problems:
                self.stdout.write(self.style.WARNING(f'  - {problem}'))

        self.stdout.write('')
        shortfall = super_admin_shortfall()
        if shortfall:
            self.stdout.write(self.style.WARNING(
                f'Super Admin floor: {shortfall} short. The emergency '
                f'account does not count toward it -- the floor exists to '
                f'guarantee two accountable PEOPLE. Appoint a second named '
                f'Super Admin before go-live.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS('Super Admin floor: met.'))

        if not apply_changes:
            self.stdout.write('')
            self.stdout.write('Re-run with --apply to write these changes.')
