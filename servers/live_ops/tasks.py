"""The supply sampler: once a minute, count idle online drivers per ~1 km cell.

DURABILITY
----------
`acks_late` (the project default) is safe: a sample is keyed by
(minute, city, cell) and written with `ignore_conflicts`, so a redelivered or
doubled run writes the same rows once. It reads Redis and writes only
`SupplySample` counts: no trip, no money, no driver status, no driver id.

Scheduled as `live-ops-supply-sample-every-minute` in
`base.settings.CELERY_BEAT_SCHEDULE`.
"""

import logging
from collections import Counter
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from .config import SUPPLY_SAMPLE_RETENTION_HOURS
from .dots import ONLINE

logger = logging.getLogger(__name__)


def sample_now(now=None, positions=None, geography=None):
    """Write this minute's samples; returns how many rows were offered."""
    from .geo import Geography
    from .heatmap import cell_of
    from .models import SupplySample
    from .positions import place, read_live_positions

    now = (now or timezone.now()).replace(second=0, microsecond=0)
    geography = geography or Geography.get(force=True)
    if positions is None:
        positions = read_live_positions()
    if not positions:
        return 0
    counts = Counter()
    for p in place(positions, geography):
        # Idle drivers are supply; a driver on a trip cannot take a request.
        # A driver outside every zone and without a home city has no city, so
        # nobody's scope could cover them: not sampled.
        if p.status == ONLINE and p.city is not None:
            counts[(p.city.id, *cell_of(p.lat, p.lng))] += 1
    SupplySample.objects.bulk_create(
        [SupplySample(city_id=city, cell_row=row, cell_col=col, drivers=n, sampled_at=now)
         for (city, row, col), n in counts.items()],
        ignore_conflicts=True)
    return len(counts)


def prune(now=None):
    from .models import SupplySample

    cutoff = (now or timezone.now()) - timedelta(hours=SUPPLY_SAMPLE_RETENTION_HOURS)
    deleted, _ = SupplySample.objects.filter(sampled_at__lt=cutoff).delete()
    return deleted


@shared_task(name='live_ops.sample_supply')
def sample_supply():
    written = sample_now()
    pruned = prune()
    if pruned:
        logger.info('live_ops supply sample: cells=%s pruned=%s', written, pruned)
    return {'cells': written, 'pruned': pruned}
