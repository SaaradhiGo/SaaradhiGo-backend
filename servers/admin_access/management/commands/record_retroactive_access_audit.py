"""Append the audit rows a past `migrate_operators --apply` should have written.

    python manage.py record_retroactive_access_audit --plan qa \\
        --actor-email srini@saaradhigo.com \\
        --performed-at 2026-10-27T01:14:00Z

WHY A ROW AFTER THE FACT, AND NOT A QUIET ONE
---------------------------------------------
On 2026-10-27 the QA plan was applied -- one Super Admin at Global, one City
Ops Manager at Hyderabad, one account flagged break-glass -- and the audit
log recorded none of it, because at the time only views wrote audit rows.
The access models now audit themselves, so this cannot recur. What remains
is the hole in the record for the night it did.

The honest repair is a row that says, in its own payload, that it was
written later by a command and not at the time by the actor: `after`
carries `recorded_retroactively: true`, `performed_at` (the original
moment, as given), and `recorded_by`. `created_at` stays the moment this
row was appended, because that is what it is. A row back-dated to look
contemporaneous would be the one kind of entry this log exists to make
impossible.

IT DESCRIBES WHAT IS THERE
--------------------------
It records the CURRENT state as the change: the assignment that exists,
the flag that is set. If a plan line's state is not present -- the account
is missing, the assignment was never made, the flag is off -- it refuses
the whole run before writing anything, because a retroactive row for a
change that cannot be seen is a guess, and nothing was written for the
other lines either so a re-run after fixing the plan starts clean.

IT IS APPEND-ONLY AND IDEMPOTENT
--------------------------------
It never edits or deletes a row; the table forbids both. A second run
finds the row it wrote for each target by its action and target and skips
it, saying so. `verify_audit_chain` passes before and after, because these
rows are appended through the ordinary model path and chain like any
other.
"""

from datetime import timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.dateparse import parse_datetime

from servers.admin_access.management.commands.migrate_operators import (
    load_plan,
    resolve_actor,
)
from servers.admin_access.models import AdminAssignment
from servers.admin_audit.models import AdminAuditLog

RECORDED_BY = 'record_retroactive_access_audit'
ASSIGNMENT_ACTION = 'operator_assignment_recorded_retroactively'
BREAK_GLASS_ACTION = 'break_glass_flag_recorded_retroactively'


def parse_performed_at(value):
    """An aware ISO-8601 instant, normalised to UTC.

    A naive timestamp is refused rather than assumed: the whole point of
    the field is to pin the original moment, and "01:14, somewhere" does
    not.
    """
    parsed = parse_datetime(str(value).strip())
    if parsed is None:
        raise CommandError(
            f'--performed-at {value!r} is not an ISO-8601 timestamp '
            f'(expected e.g. 2026-10-27T01:14:00Z).'
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CommandError(
            f'--performed-at {value!r} has no timezone. Give the moment in '
            f'UTC with a Z or an offset, so the row pins a real instant.'
        )
    return parsed.astimezone(dt_timezone.utc)


class Command(BaseCommand):
    help = ('Append retroactive audit rows for a plan that migrate_operators '
            'applied without any. Append-only; idempotent.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--plan', default='qa',
            help='"qa" for the built-in QA mapping, or the JSON plan file '
                 'that was applied.',
        )
        parser.add_argument(
            '--actor-email', required=True,
            help='Email of the operator who ran migrate_operators. Becomes '
                 'the actor on every row.',
        )
        parser.add_argument(
            '--performed-at', required=True,
            help='When migrate_operators --apply ran, ISO-8601 with timezone '
                 '(e.g. 2026-10-27T01:14:00Z). Stored in the payload.',
        )

    def handle(self, *args, **options):
        plan = load_plan(options['plan'])
        actor = resolve_actor(options['actor_email'])
        performed_at = parse_performed_at(options['performed_at'])

        pending, problems = self._inspect(plan)
        if problems:
            raise CommandError(
                'Nothing was written. These plan lines do not describe the '
                'current state, and a retroactive row for a change that '
                'cannot be seen would be a guess:\n  ' + '\n  '.join(problems)
            )

        written, skipped = [], []
        with transaction.atomic():
            for target_type, target_id, action, after, description in pending:
                exists = AdminAuditLog.objects.filter(
                    action=action, target_type=target_type,
                    target_id=str(target_id),
                ).exists()
                if exists:
                    skipped.append(description)
                    continue
                AdminAuditLog.objects.create(
                    actor=actor,
                    actor_label=self._label(actor),
                    action=action,
                    target_type=target_type,
                    target_id=str(target_id),
                    # Not captured at the time; saying so is the record.
                    before={},
                    after={
                        'recorded_retroactively': True,
                        'performed_at': performed_at.isoformat(),
                        'recorded_by': RECORDED_BY,
                        'plan': options['plan'],
                        **after,
                    },
                    reason=(
                        f'Recorded retroactively. migrate_operators --plan '
                        f'{options["plan"]} --apply made this change at '
                        f'{performed_at.isoformat()} and, under the rules then '
                        f'in force, wrote no audit row. created_at is when '
                        f'this row was appended; after.performed_at is when '
                        f'the change happened.'
                    ),
                )
                written.append(description)

        self._report(written, skipped)

    # -- internals -----------------------------------------------------

    def _inspect(self, plan):
        """What would be written, and what cannot be."""
        user_model = get_user_model()
        pending, problems = [], []

        for entry in plan:
            email = entry['email']
            user = user_model.objects.filter(email__iexact=email).first()
            if user is None:
                problems.append(f'{email}: no such account.')
                continue

            if entry.get('break_glass'):
                if not user.is_break_glass:
                    problems.append(
                        f'{email}: is not flagged break-glass, so there is '
                        f'no change to record.'
                    )
                else:
                    pending.append((
                        'User', user.pk, BREAK_GLASS_ACTION,
                        {
                            'is_break_glass': True,
                            'admin_id': user.pk,
                            'note': entry.get('note', ''),
                        },
                        f'{email}: break-glass flag',
                    ))

            role_code = entry.get('role')
            if role_code:
                scope_code = entry.get('scope')
                assignment = (
                    AdminAssignment.objects
                    .select_related('role', 'scope')
                    .filter(admin=user, role__code=role_code,
                            scope__code=scope_code, is_active=True)
                    .first()
                )
                if assignment is None:
                    problems.append(
                        f'{email}: holds no active {role_code}@{scope_code}, '
                        f'so there is no change to record.'
                    )
                else:
                    pending.append((
                        'AdminAssignment', assignment.pk, ASSIGNMENT_ACTION,
                        {
                            'admin_id': user.pk,
                            'role': assignment.role.code,
                            'scope': assignment.scope.code,
                            'is_active': True,
                            'assignment_created_at':
                                assignment.created_at.isoformat(),
                            'note': entry.get('note', ''),
                        },
                        f'{email}: {role_code}@{scope_code}',
                    ))

        return pending, problems

    @staticmethod
    def _label(actor):
        return (getattr(actor, 'full_name', None)
                or getattr(actor, 'email', None)
                or str(actor))[:255]

    def _report(self, written, skipped):
        self.stdout.write(self.style.MIGRATE_HEADING(
            'Retroactive access audit'))
        for line in written:
            self.stdout.write(self.style.SUCCESS(f'  recorded  {line}'))
        for line in skipped:
            self.stdout.write(f'  skipped   {line} (already recorded)')
        self.stdout.write('')
        self.stdout.write(
            f'{len(written)} row(s) appended, {len(skipped)} already present. '
            f'Run verify_audit_chain to confirm the chain.'
        )
