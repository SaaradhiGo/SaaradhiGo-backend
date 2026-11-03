"""Five-minute city rollups: written by a task, read by every overview.

WRITE (`refresh`): recount the buckets in a window from the source tables and
replace those buckets' rows. The task runs every minute over the last two
hours, which covers a trip completing or being cancelled long after it was
requested. `backfill` walks history a day at a time.

READ (`totals`): sum the rows for many cities over a window in one grouped
query per time zone (each city's "today" starts at its own midnight), then add
them up the place path for a state, a country or Global (`roll_up`).

The counting rules are the overview's (`overview.measure_trips`) so a figure
here and the same figure on a list can never disagree. Nothing here is a
guess: a city with no rows has no numbers, and a rate over nothing is None.
"""

import logging
from collections import defaultdict
from datetime import timedelta, timezone as dt_timezone

from django.core.cache import cache
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from base.money import charged_fare

from .models import CityRollup

logger = logging.getLogger(__name__)

BUCKET = timedelta(minutes=5)
#: How far back the minute task recounts. A trip outcome later than this is
#: picked up by the nightly backfill of the previous day.
RECOUNT = timedelta(hours=2)
LOCK_KEY = 'command_centre:rollups:refresh'
COUNTS = ('requested', 'accepted', 'cancelled', 'unfulfilled', 'completed',
          'eta_seconds_sum', 'eta_count', 'sos_raised', 'tickets_raised')


def floor_bucket(when):
    when = when.astimezone(dt_timezone.utc)
    return when.replace(minute=when.minute - when.minute % 5, second=0, microsecond=0)


def _add(rows, city_id, when, currency, **values):
    if city_id is None or when is None:
        return
    row = rows[(city_id, floor_bucket(when), currency or '')]
    for key, value in values.items():
        row[key] += value


def count(start, end):
    """{(city_id, bucket_start, currency): {field: value}} for [start, end)."""
    from servers.ride.models import Trip
    from servers.sos.models import SOSEvent
    from servers.support.models import SupportTicket

    rows = defaultdict(lambda: defaultdict(int))
    requested = (Trip.objects.filter(requested_at__gte=start, requested_at__lt=end,
                                     zone__geo_node__isnull=False)
                 .values_list('zone__geo_node_id', 'requested_at', 'accepted_at', 'reached_at',
                              'status_id__status_code', 'currency'))
    for city, req_at, acc_at, reached_at, code, cur in requested.iterator(chunk_size=5000):
        values = {'requested': 1}
        if acc_at:
            values['accepted'] = 1
            if reached_at and reached_at >= acc_at:
                values['eta_seconds_sum'] = int((reached_at - acc_at).total_seconds())
                values['eta_count'] = 1
        if code == 'cancelled':
            values['cancelled'] = 1
            if not acc_at:
                values['unfulfilled'] = 1
        _add(rows, city, req_at, cur, **values)

    completed = (Trip.objects.filter(completed_at__gte=start, completed_at__lt=end,
                                     status_id__status_code='completed',
                                     zone__geo_node__isnull=False)
                 .annotate(fare=charged_fare())
                 .values_list('zone__geo_node_id', 'completed_at', 'currency', 'fare'))
    for city, done_at, cur, fare in completed.iterator(chunk_size=5000):
        _add(rows, city, done_at, cur, completed=1, collected_minor=int(fare or 0))

    # An SOS sits where it was anchored (a city), else in its trip's city.
    sos = (SOSEvent.objects.filter(created_at__gte=start, created_at__lt=end)
           .values_list('anchor_node_id', 'anchor_node__kind', 'trip__zone__geo_node_id', 'created_at'))
    for anchor, kind, trip_city, created in sos.iterator(chunk_size=5000):
        _add(rows, anchor if kind == 'city' else trip_city, created, '', sos_raised=1)

    tickets = (SupportTicket.objects.filter(created_at__gte=start, created_at__lt=end)
               .values_list('trip_id__zone__geo_node_id', 'user_id__driver__home_city_id', 'created_at'))
    for trip_city, home_city, created in tickets.iterator(chunk_size=5000):
        _add(rows, trip_city or home_city, created, '', tickets_raised=1)
    return rows


def refresh(start, end):
    """Replace the rows of every bucket in [start, end). Returns the row count."""
    start, end = floor_bucket(start), floor_bucket(end) + BUCKET
    rows = count(start, end)
    objs = [
        CityRollup(city_id=city, bucket_start=bucket, currency=cur,
                   collected_minor=values.get('collected_minor', 0),
                   **{k: values.get(k, 0) for k in COUNTS})
        for (city, bucket, cur), values in rows.items()
    ]
    with transaction.atomic():
        CityRollup.objects.filter(bucket_start__gte=start, bucket_start__lt=end).delete()
        CityRollup.objects.bulk_create(objs, batch_size=2000)
    return len(objs)


def refresh_recent(now=None):
    """The minute task's body: the last two hours, once at a time."""
    now = now or timezone.now()
    if not cache.add(LOCK_KEY, '1', timeout=110):
        return None
    try:
        return refresh(now - RECOUNT, now)
    finally:
        cache.delete(LOCK_KEY)


def backfill(days, now=None, log=None):
    now = now or timezone.now()
    day_end = floor_bucket(now)
    total = 0
    for _ in range(days):
        day_start = day_end - timedelta(days=1)
        total += refresh(day_start, day_end - BUCKET)
        if log:
            log(f'{day_start:%Y-%m-%d}: {total} rows so far')
        day_end = day_start
    return total


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def empty():
    return {**{k: 0 for k in COUNTS}, 'money': {}}


def _merge(into, row):
    for k in COUNTS:
        into[k] += row.get(k) or 0
    cur = row.get('currency') or ''
    minor = row.get('collected_minor') or 0
    if cur and minor:
        into['money'][cur] = into['money'].get(cur, 0) + minor
    return into


def totals(windows):
    """Sum the rollups per city.

    `windows` is `{city_id: (start, end)}`. Cities that share a window (the
    same time zone's "today") are read in one grouped query, so a Global read
    is a handful of queries whatever the city count.
    Returns `{city_id: {counts..., 'money': {CUR: minor}}}`.
    """
    by_window = defaultdict(list)
    for city_id, bounds in windows.items():
        by_window[(floor_bucket(bounds[0]), bounds[1])].append(city_id)
    out = {city_id: empty() for city_id in windows}
    for (start, end), ids in by_window.items():
        rows = (CityRollup.objects.filter(bucket_start__gte=start, bucket_start__lt=end,
                                          city_id__in=ids)
                .values('city_id', 'currency')
                .annotate(**{k: Sum(k) for k in (*COUNTS, 'collected_minor')}))
        for row in rows:
            _merge(out[row['city_id']], row)
    return out


def hourly(city_ids, start, end):
    """[(hour_start, totals)] for the given cities summed, hour by hour."""
    from django.db.models.functions import TruncHour

    rows = (CityRollup.objects.filter(bucket_start__gte=start, bucket_start__lt=end,
                                      city_id__in=list(city_ids))
            .annotate(hour=TruncHour('bucket_start', tzinfo=dt_timezone.utc))
            .values('hour', 'currency')
            .annotate(**{k: Sum(k) for k in (*COUNTS, 'collected_minor')})
            .order_by('hour'))
    out = defaultdict(empty)
    for row in rows:
        _merge(out[row['hour']], row)
    return sorted(out.items())


def add(a, b):
    """Two totals as one (money stays per currency)."""
    out = empty()
    for part in (a, b):
        for k in COUNTS:
            out[k] += part.get(k, 0)
        for cur, minor in part.get('money', {}).items():
            out['money'][cur] = out['money'].get(cur, 0) + minor
    return out


def rate(numerator, denominator):
    if not denominator:
        return None
    return round(numerator * 100.0 / denominator, 1)


def figures(t):
    """The derived numbers a row shows, from a totals dict. None, never 0, for
    a rate over nothing."""
    return {
        'requests': t['requested'],
        'completed': t['completed'],
        'unfulfilled': t['unfulfilled'],
        'unfulfilled_rate': rate(t['unfulfilled'], t['requested']),
        # Of the requests, the share that did not end unfulfilled (no driver ever accepted).
        'fulfilment_rate': (round(100.0 - rate(t['unfulfilled'], t['requested']), 1)
                            if t['requested'] else None),
        'acceptance_rate': rate(t['accepted'], t['requested']),
        'eta_minutes': round(t['eta_seconds_sum'] / 60 / t['eta_count'], 1) if t['eta_count'] else None,
        'eta_samples': t['eta_count'],
        'sos_raised': t['sos_raised'],
        'tickets_raised': t['tickets_raised'],
        'collected_minor': dict(sorted(t['money'].items())),
    }
