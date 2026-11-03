"""Recount the five-minute city rollups for the last N days.

    python manage.py backfill_rollups --days 60

Run once after deploying the rollups (and on the load stack after seeding);
the minute task keeps the last two hours fresh and a nightly task recounts
yesterday. Idempotent: each day's buckets are replaced from the source tables.
Writes CityRollup rows only, so it needs no actor.
"""

from django.core.management.base import BaseCommand

from servers.command_centre.rollups import backfill


class Command(BaseCommand):
    help = 'Recount the five-minute city rollups for the last N days.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=60)

    def handle(self, *args, **options):
        total = backfill(options['days'], log=self.stdout.write)
        self.stdout.write(self.style.SUCCESS(f'{total} rollup rows written.'))
