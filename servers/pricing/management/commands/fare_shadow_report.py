"""Read-only summary of fare shadow observations.

Exists so the fare decision is made from a number somebody can reproduce, rather
than from a dashboard nobody can audit. It reads `TripFareShadow` and prints;
it writes nothing and charges nobody.

    python manage.py fare_shadow_report --days 7
    python manage.py fare_shadow_report --days 30 --zone HYD
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone


class Command(BaseCommand):
    help = 'Summarise fare shadow observations (read-only).'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=7)
        parser.add_argument('--zone', type=str, default=None)

    def handle(self, *args, **options):
        from servers.pricing.fare_shadow import summarise
        from servers.pricing.models import TripFareShadow

        cutoff = timezone.now() - timedelta(days=options['days'])
        qs = TripFareShadow.objects.filter(observed_at__gte=cutoff)
        if options['zone']:
            qs = qs.filter(zone_code=options['zone'])

        rows = list(qs)
        summary = summarise(rows)

        # Trips that could not be shadowed are printed first and on purpose:
        # they are the denominator of every percentage below, and omitting them
        # is how a partial sample gets mistaken for the whole picture.
        self.stdout.write(f'Window: last {options["days"]} day(s)')
        self.stdout.write(f'Observations: {summary["rows"]}')
        self.stdout.write('By status:')
        counts = {}
        for row in rows:
            counts[row.status] = counts.get(row.status, 0) + 1
        for status, count in sorted(counts.items()):
            self.stdout.write(f'  {status}: {count}')

        self.stdout.write(f'\nComparable trips: {summary["comparable"]}')
        if not summary['comparable']:
            self.stdout.write(self.style.WARNING(
                'No comparable trips. Metering cannot be assessed from this window.'
            ))
            return

        self.stdout.write(f'  metering would charge MORE:  {summary["metering_higher"]}')
        self.stdout.write(f'  metering would charge LESS:  {summary["metering_lower"]}')
        self.stdout.write(f'  unchanged:                   {summary["unchanged"]}')
        self.stdout.write(f'  mean difference per trip:    {summary["mean_metric_delta"]}')
        self.stdout.write(f'  largest increase:            {summary["worst_increase"]}')
        self.stdout.write(f'  largest decrease:            {summary["worst_decrease"]}')

        # Coverage is reported separately because a difference derived from a
        # patchy trail is a weaker fact than one derived from a dense trail, and
        # averaging them together hides that.
        thin = [r for r in rows if (r.coverage_ratio or 0) < 0.8 and r.metric_delta is not None]
        if thin:
            self.stdout.write(self.style.WARNING(
                f'\n{len(thin)} of {summary["comparable"]} comparable trips have '
                f'trail coverage below 80%. Treat their distances as weak evidence.'
            ))
