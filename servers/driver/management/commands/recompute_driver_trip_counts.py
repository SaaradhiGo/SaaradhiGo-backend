"""Recompute `Driver.total_trips` from the Trip table.

WHY THIS EXISTS
---------------
`total_trips` is a stored counter that, until now, nothing incremented: it
read 0 for every driver no matter how many trips they had finished. Trip #81
completed and the counter did not move.

The completion path now increments it on the real state transition, which
fixes it going forward. Every trip completed BEFORE that is still uncounted,
and the honest way to repair them is to derive the number from the trips
themselves rather than to type a figure into the database.

It is safe to run repeatedly: the count is computed from Trip each time, so
running it twice gives the same answer as running it once. It never adds to
the existing value.

    python manage.py recompute_driver_trip_counts            # report only
    python manage.py recompute_driver_trip_counts --apply \\
        --actor-email you@saaradhigo.com                     # write

`--apply` changes a stored driver figure, so it requires `--actor-email`:
the operator running it, validated by `admin_audit.actor_option`. The
report needs no name because it writes nothing.

ONE AUDIT ROW PER RUN
---------------------
A driver's trip count is money-adjacent (it feeds earnings screens and the
driver's own history), so an apply leaves a record -- but one per RUN, not
one per driver: a thousand rows saying "counter corrected" would drown the
log the access review reads. The row (`driver_trip_counts_recomputed`)
says how many drivers were examined, how many changed, and lists the
changed ids up to 200 with the true total alongside, so a reader can tell
a trimmed list from a short one. A `--apply` that finds no drift still
writes its row: "checked, nothing to fix" is evidence too.
"""

from django.core.management.base import BaseCommand
from django.db.models import Count, Q

from servers.admin_audit.actor_option import (
    add_actor_argument,
    attributed,
    require_actor,
)
from servers.admin_audit.services import record_admin_action

COMMAND = 'recompute_driver_trip_counts'
AUDIT_ACTION = 'driver_trip_counts_recomputed'
#: How many changed driver ids the summary row lists. The total is always
#: recorded alongside, so a capped list is never mistaken for a short one.
MAX_IDS_IN_ROW = 200


class Command(BaseCommand):
    help = (
        "Recompute Driver.total_trips from completed trips. Reports by "
        "default; --apply writes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Write the recomputed values. Without it, only report.',
        )
        add_actor_argument(parser)

    def handle(self, *args, **options):
        from servers.driver.models import Driver

        apply_changes = options['apply']

        # Before the first write -- and before the report, so a nameless
        # apply is refused outright rather than after a scan.
        actor = require_actor(options, COMMAND) if apply_changes else None

        # Count only trips that actually completed. A cancelled trip is not a
        # trip the driver did, and must never be counted.
        drivers = Driver.objects.annotate(
            completed=Count(
                'trips',
                filter=Q(trips__status_id__status_code='completed'),
                distinct=True,
            ),
        ).only('id', 'total_trips')

        examined = 0
        drifted = []
        for driver in drivers:
            examined += 1
            if (driver.total_trips or 0) != driver.completed:
                drifted.append((driver.id, driver.total_trips or 0, driver.completed))

        if not drifted:
            self.stdout.write(self.style.SUCCESS(
                'every driver\'s total_trips already matches their completed trips'
            ))
        else:
            self.stdout.write(f'{len(drifted)} driver(s) differ from the trip table:')
            for driver_id, stored, actual in drifted:
                self.stdout.write(f'  driver {driver_id}: stored={stored} actual={actual}')

        if not apply_changes:
            if drifted:
                self.stdout.write(self.style.WARNING(
                    'report only -- pass --apply to write these values'
                ))
            return

        with attributed(actor, COMMAND,
                        reason='Driver trip counts recomputed from the Trip table.'):
            for driver_id, _stored, actual in drifted:
                Driver.objects.filter(id=driver_id).update(total_trips=actual)
            self._record_run(examined, drifted)

        self.stdout.write(self.style.SUCCESS(f'updated {len(drifted)} driver(s)'))

    def _record_run(self, examined, drifted):
        changed_ids = [driver_id for driver_id, _stored, _actual in drifted]
        row = record_admin_action(
            None,
            action=AUDIT_ACTION,
            target_type='driver_trip_counts',
            target_id='',
            before={},
            after={
                'drivers_examined': examined,
                'drivers_changed': len(changed_ids),
                'changed_driver_ids': changed_ids[:MAX_IDS_IN_ROW],
                'changed_driver_ids_listed': min(len(changed_ids), MAX_IDS_IN_ROW),
                'changed_driver_ids_truncated': len(changed_ids) > MAX_IDS_IN_ROW,
            },
            reason=('Driver.total_trips recomputed from completed trips. One '
                    'row per run; the ids listed are capped at '
                    f'{MAX_IDS_IN_ROW}, drivers_changed is the true total.'),
        )
        if row is None:
            self.stderr.write(self.style.WARNING(
                'the summary audit row could not be written; see the log'))
