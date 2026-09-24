"""Classify completed trips by how well their economics can be reconstructed.

Counts only. This command writes nothing and is safe to run against production.

It exists because backfilling `TripSettlement` for historical trips is only
defensible for the rows where the economics are actually knowable. A financial table
that silently mixes recorded facts with guesses is worse than one that has a gap and
says so, and the categories below are the honest way to find out which is which
BEFORE anything is written.

Categories:

  A  exact ledger linkage       a WalletTransaction with the TRIP_<id>_EARNING key
                                exists. Gross, commission and net are derivable
                                from immutable facts plus the ledger row.
  B  reconstructable            no keyed ledger row, but a fare and a completed
                                settlement-shaped TransactionHistory row exist, so
                                the economics follow from recorded amounts.
  C  partially reconstructable  a fare exists and the trip completed, but nothing
                                records that money moved. Gross is known; whether
                                the driver was ever settled is not.
  D  ambiguous                  conflicting evidence -- more than one candidate
                                ledger row, or amounts that disagree.
  E  missing financial evidence no fare at all. Nothing can be reconstructed.

Only A and B should ever be backfilled, and then with `source=reconstructed` so no
reader mistakes a derived commission rate for the one that was applied.
"""

from collections import Counter
from decimal import Decimal

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = ('Count completed trips by how reconstructable their settlement '
            'economics are. Read-only; writes nothing.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--sample', type=int, default=0,
            help='Print up to N trip ids per category, for spot-checking. '
                 'Ids only -- no amounts, no names, no phone numbers.',
        )

    def handle(self, *args, **options):
        from servers.payments.models import TransactionHistory, TripSettlement
        from servers.ride.models import Trip
        from servers.rider.models import WalletTransaction

        completed = (
            Trip.objects
            .filter(status_id__status_code='completed')
            .select_related('status_id')
            .only('id', 'estimated_fare', 'final_fare', 'payment_method',
                  'driver_id', 'status_id')
            .order_by('id')
        )

        total = completed.count()
        if not total:
            self.stdout.write('No completed trips found.')
            return

        # Bulk-load the evidence rather than querying per trip: this runs against
        # production and must not be an N+1 over the whole ride history.
        keyed = {}
        for ref, n in (
            WalletTransaction.objects
            .filter(idempotency_key__startswith='TRIP_')
            .values_list('idempotency_key', 'id')
        ):
            if ref.endswith('_EARNING'):
                try:
                    tid = int(ref.split('_')[1])
                except (IndexError, ValueError):
                    continue
                keyed.setdefault(tid, []).append(n)

        history = Counter(
            TransactionHistory.objects
            .filter(status='completed')
            .values_list('trip_id_id', flat=True)
        )
        already_settled = set(
            TripSettlement.objects.values_list('trip_id', flat=True)
        )

        buckets = Counter()
        samples = {k: [] for k in 'ABCDE'}
        sample_n = options['sample']

        for trip in completed.iterator(chunk_size=500):
            fare = trip.final_fare or trip.estimated_fare
            ledger_rows = keyed.get(trip.id, [])

            if fare is None or Decimal(str(fare)) <= 0:
                bucket = 'E'
            elif len(ledger_rows) > 1:
                # The idempotency key should make this impossible. If it happens,
                # it is exactly the case a backfill must not guess at.
                bucket = 'D'
            elif len(ledger_rows) == 1:
                bucket = 'A'
            elif history.get(trip.id):
                bucket = 'B'
            else:
                bucket = 'C'

            buckets[bucket] += 1
            if sample_n and len(samples[bucket]) < sample_n:
                samples[bucket].append(trip.id)

        self.stdout.write('')
        self.stdout.write(f'Completed trips: {total}')
        self.stdout.write(f'Already have a settlement: {len(already_settled)}')
        self.stdout.write('')

        labels = {
            'A': 'exact ledger linkage        (backfillable)',
            'B': 'reconstructable from amounts (backfillable, mark reconstructed)',
            'C': 'partially reconstructable    (DO NOT backfill)',
            'D': 'ambiguous                    (DO NOT backfill -- investigate)',
            'E': 'missing financial evidence   (nothing to backfill)',
        }
        for key in 'ABCDE':
            n = buckets[key]
            pct = (n / total * 100) if total else 0
            self.stdout.write(f'  {key}  {n:>7}  {pct:5.1f}%  {labels[key]}')

        backfillable = buckets['A'] + buckets['B']
        self.stdout.write('')
        self.stdout.write(
            f'Safely backfillable (A+B): {backfillable} of {total} '
            f'({backfillable / total * 100:.1f}%)'
        )
        if buckets['D']:
            self.stdout.write(self.style.WARNING(
                f'{buckets["D"]} trips have MORE THAN ONE keyed ledger row. The '
                'idempotency key should make that impossible; investigate before '
                'any backfill.'
            ))

        if sample_n:
            self.stdout.write('')
            self.stdout.write('Sample trip ids (ids only):')
            for key in 'ABCDE':
                if samples[key]:
                    self.stdout.write(f'  {key}: {samples[key]}')

        self.stdout.write('')
        self.stdout.write(
            'Read-only. Nothing was written. A backfill of A+B must set '
            'source=reconstructed, because the commission rate would be DERIVED '
            'rather than the rate that was applied.'
        )
