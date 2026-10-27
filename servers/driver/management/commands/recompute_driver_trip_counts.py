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
    python manage.py recompute_driver_trip_counts --apply    # write
"""

from django.core.management.base import BaseCommand
from django.db.models import Count, Q


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

    def handle(self, *args, **options):
        from servers.driver.models import Driver

        apply_changes = options['apply']

        # Count only trips that actually completed. A cancelled trip is not a
        # trip the driver did, and must never be counted.
        drivers = Driver.objects.annotate(
            completed=Count(
                'trips',
                filter=Q(trips__status_id__status_code='completed'),
                distinct=True,
            ),
        ).only('id', 'total_trips')

        drifted = []
        for driver in drivers:
            if (driver.total_trips or 0) != driver.completed:
                drifted.append((driver.id, driver.total_trips or 0, driver.completed))

        if not drifted:
            self.stdout.write(self.style.SUCCESS(
                'every driver\'s total_trips already matches their completed trips'
            ))
            return

        self.stdout.write(f'{len(drifted)} driver(s) differ from the trip table:')
        for driver_id, stored, actual in drifted:
            self.stdout.write(f'  driver {driver_id}: stored={stored} actual={actual}')

        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                'report only -- pass --apply to write these values'
            ))
            return

        for driver_id, _stored, actual in drifted:
            Driver.objects.filter(id=driver_id).update(total_trips=actual)

        self.stdout.write(self.style.SUCCESS(f'updated {len(drifted)} driver(s)'))
