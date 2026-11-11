"""Append the ONE row that says the two linking migrations ran unaudited.

    python manage.py record_retroactive_linking_audit --performed-on 2026-10-27 \\
        --actor-email you@saaradhigo.com

THE RULING (owner, 2026-10-28, ruling 3)
----------------------------------------
Zone-to-geo and driver home-city linking leave full audit rows from the
seed path (`admin_access.linking`), but the two data migrations that did
the same work on QA and production on 2026-10-27 -- `pricing.0010` and
`driver.0027` -- ran with historical models and, deliberately, wrote no
row (see their docstrings and `linking.is_historical`). The links they
made are a known gap in the record. The honest repair is one summary row
that says so in its own payload.

WHY ONE ROW AND NOT ONE PER LINK
--------------------------------
A migration is one event. It did not decide each link individually -- the
zone code equals the node code, the sole live city is the home city -- and
inventing a row per zone with a timestamp nobody recorded would be a
reconstruction presented as a record. What IS known is which migrations
ran, on what day, and what the table looks like now; the row carries
exactly that: the two migration names, the number of zones currently
linked to a geo node and of drivers currently holding a home city,
`recorded_retroactively: true` and `performed_on`. `created_at` stays the
moment this row was appended, because that is what it is.

WHO IS THE ACTOR
----------------
The migration ran unattended inside a deploy; nobody typed it. So the
row's label is `system:migration`, the same shape as `system:genesis`
for the first operator. The person who runs THIS command is still named:
`--actor-email` is required at parse time like every other writing
command, validated by `admin_audit.actor_option`, and goes on the row as
the actor snapshot (`actor_id`, `actor_email`, `actor_role`) and in
`after.recorded_by`. Reading the row: the migration did it; this operator
recorded it.

IT IS APPEND-ONLY AND IDEMPOTENT
--------------------------------
It never edits or deletes a row; the table forbids both. A second run
finds the row by its action and skips with a message. The row goes
through the ordinary model path, so it chains like any other and
`verify_audit_chain` passes afterwards.
"""

from datetime import date

from django.core.management.base import BaseCommand, CommandError

from servers.admin_audit.actor_option import add_actor_argument, require_actor
from servers.admin_audit.context import audit_context
from servers.admin_audit.models import AdminAuditLog
from servers.admin_audit.services import record_admin_action

COMMAND = 'record_retroactive_linking_audit'
ACTION = 'linking_migrations_recorded_retroactively'
ACTOR_LABEL = 'system:migration'
TARGET_TYPE = 'linking_migrations'
#: The two data migrations, by their real names, in the order they ran.
MIGRATIONS = [
    'pricing.0010_link_zones_to_geo_nodes',
    'driver.0027_backfill_driver_home_city',
]


def parse_performed_on(value):
    """An ISO-8601 calendar date. A day, not an instant: a migration's
    exact second was never recorded, and the row must not pretend it was."""
    text = str(value).strip()
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise CommandError(
            f'--performed-on {value!r} is not an ISO-8601 date (expected '
            f'e.g. 2026-10-27).'
        )


def current_counts():
    """What the two linked columns hold right now."""
    from servers.driver.models import Driver
    from servers.pricing.models import ServiceZone

    return {
        'service_zones_linked': ServiceZone.objects.filter(geo_node__isnull=False).count(),
        'drivers_with_home_city': Driver.objects.filter(home_city__isnull=False).count(),
    }


class Command(BaseCommand):
    help = ('Append one summary audit row for the two linking migrations that '
            'ran without any. Append-only; idempotent.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--performed-on', required=True,
            help='The day the migrations ran, ISO-8601 (e.g. 2026-10-27). '
                 'Stored in the payload.',
        )
        # Every run of this command writes, so the flag is required at parse
        # time; the same validator as every other writing command applies.
        add_actor_argument(
            parser, required=True,
            help='Email of the operator recording this. Snapshotted on the '
                 'row and named in after.recorded_by; the label stays '
                 'system:migration. Must be an active operator.',
        )

    def handle(self, *args, **options):
        performed_on = parse_performed_on(options['performed_on'])
        actor = require_actor(options, COMMAND)

        existing = AdminAuditLog.objects.filter(action=ACTION).order_by('id').first()
        if existing is not None:
            self.stdout.write(
                f'Already recorded: audit row {existing.pk} ({ACTION}, '
                f'performed_on {existing.after.get("performed_on")}). '
                f'Nothing was written.'
            )
            return

        counts = current_counts()
        reason = (
            f'Recorded retroactively. The data migrations '
            f'{" and ".join(MIGRATIONS)} ran on {performed_on.isoformat()} '
            f'with historical models and, by design, wrote no audit row for '
            f'the links they made (see admin_access.linking). This one row '
            f'records that they ran and what the linked columns hold at the '
            f'moment of recording. created_at is when this row was appended; '
            f'after.performed_on is the day the migrations ran.'
        )

        # The migration is the actor, by label; the operator who records it
        # is the actor, by snapshot. The context's explicit label wins over
        # the one derived from the account (see AdminAuditLog.actor).
        with audit_context(actor=actor, actor_label=ACTOR_LABEL, reason=reason):
            row = record_admin_action(
                None,
                action=ACTION,
                target_type=TARGET_TYPE,
                target_id='',
                # Not captured at the time; saying so is the record.
                before={},
                after={
                    'migrations': list(MIGRATIONS),
                    **counts,
                    'recorded_retroactively': True,
                    'performed_on': performed_on.isoformat(),
                    'recorded_by': actor.email,
                },
                reason=reason,
            )
        if row is None:
            # The row IS the point of this command; a swallowed failure
            # would report success for a repair that did not happen.
            raise CommandError(
                'The audit row could not be written; see the log. Nothing '
                'was kept.'
            )

        self.stdout.write(self.style.MIGRATE_HEADING('Retroactive linking audit'))
        self.stdout.write(self.style.SUCCESS(
            f'  recorded  audit row {row.pk}: {", ".join(MIGRATIONS)} on '
            f'{performed_on.isoformat()}; '
            f'{counts["service_zones_linked"]} zone(s) linked, '
            f'{counts["drivers_with_home_city"]} driver(s) with a home city.'
        ))
        self.stdout.write('')
        self.stdout.write('1 row appended. Run verify_audit_chain to confirm the chain.')
