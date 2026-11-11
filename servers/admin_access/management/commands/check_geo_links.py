"""What is invisible to every scoped admin, and why.

    python manage.py check_geo_links
    python manage.py check_geo_links --strict   # non-zero exit if anything is adrift

A row with no geo node is filtered out of every scoped admin's view. That
is the safe direction — better invisible than visible to the wrong
country — but it is still a hole, because a driver nobody can see is a
driver whose KYC nobody reviews and whose payout nobody approves.

So the hole is counted rather than tolerated silently. `--strict` makes it
a deploy check.

Read-only. It changes nothing, so it is safe against production.
"""

from django.core.management.base import BaseCommand

from servers.admin_access.models import ACTIVE, CITY, GeoNode


class Command(BaseCommand):
    help = 'Report rows that no scoped admin can see because they have no geo node.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--strict', action='store_true',
            help='Exit non-zero if anything is unlinked. For CI and deploys.',
        )

    def handle(self, *args, **options):
        from servers.driver.models import Driver
        from servers.pricing.models import ServiceZone
        from servers.ride.models import Trip

        problems = []

        # --- the spine itself ------------------------------------------
        nodes = GeoNode.objects.count()
        if not nodes:
            problems.append(
                'No GeoNode rows at all. Run `manage.py seed_admin_access`. '
                'Until then every scoped admin sees nothing.'
            )
        live_cities = list(
            GeoNode.objects.filter(kind=CITY, status=ACTIVE)
            .values_list('code', flat=True)
        )
        self.stdout.write(f'geo nodes: {nodes}')
        self.stdout.write(f'live cities: {", ".join(live_cities) or "(none)"}')

        # --- zones -----------------------------------------------------
        unlinked_zones = list(
            ServiceZone.objects.filter(geo_node__isnull=True)
            .values_list('code', flat=True)
        )
        if unlinked_zones:
            problems.append(
                f'{len(unlinked_zones)} service zone(s) have no geo node: '
                f'{sorted(unlinked_zones)}. Trips in them are invisible to '
                f'every scoped admin. Normally the node code equals the zone '
                f'code; `seed_admin_access` links them.'
            )

        # --- drivers ---------------------------------------------------
        unassigned_drivers = Driver.objects.filter(home_city__isnull=True).count()
        if unassigned_drivers:
            problems.append(
                f'{unassigned_drivers} driver(s) have no home_city. Their KYC '
                f'and payouts are invisible to every scoped admin. Assign a '
                f'city, or run the driver/0027 backfill when exactly one city '
                f'is live.'
            )

        # --- trips -----------------------------------------------------
        # A trip with no zone predates zone stamping, or was booked outside
        # every polygon. Either way no scoped admin can open it.
        unzoned_trips = Trip.objects.filter(zone__isnull=True).count()
        if unzoned_trips:
            problems.append(
                f'{unzoned_trips} trip(s) have no zone, so they are invisible '
                f'to every scoped admin. These are historical or were booked '
                f'outside all polygons.'
            )

        self.stdout.write('')
        if not problems:
            self.stdout.write(self.style.SUCCESS(
                'Every scopable row reaches a geo node.'
            ))
            return

        for problem in problems:
            self.stdout.write(self.style.WARNING(f'  - {problem}'))

        self.stdout.write('')
        self.stdout.write(
            'Nothing above is a data loss: unlinked rows are hidden from '
            'scoped admins, not deleted, and remain visible at Global scope.'
        )

        if options['strict']:
            from django.core.management.base import CommandError

            raise CommandError(f'{len(problems)} geo-link problem(s).')
