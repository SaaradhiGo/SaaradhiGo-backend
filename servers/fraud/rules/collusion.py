"""collusion: a rider and a driver who ride together far more than chance explains.

Two shapes over completed trips in the window: the same rider-driver pair with `min_pair_trips` or
more trips, and a driver with `min_driver_trips` or more trips of which `concentration_pct` or more
are with `top_riders` riders. The subject is the DRIVER (the rider ids are `counterpart_user_ids`).
A driver with an incentive in progress is scored higher: that is when trading trips pays.
"""

from collections import defaultdict

from django.db.models import Count

from servers.fraud.models import HIGH

from .base import Hit, Param, Rule, clamp_score, completed_status_id, severity_for, window_start

MAX_PAIRS = 500
MAX_TRIP_REFS = 50


def _incentive_in_progress(driver_ids):
    from servers.promotions.models import IN_PROGRESS, IncentiveProgress

    return set(IncentiveProgress.objects.filter(driver_id__in=driver_ids, status=IN_PROGRESS,
                                                campaign__state='live')
               .values_list('driver_id', flat=True))


def evaluate(th, country, now):
    from servers.driver.models import Driver
    from servers.ride.models import Trip

    done = completed_status_id()
    if done is None:
        return []
    base = Trip.objects.filter(
        status_id=done, completed_at__gte=window_start(now, days=th['window_days']),
        zone__country=country, driver_id__isnull=False)
    found = defaultdict(lambda: {'signals': set(), 'riders': set(), 'pair_trips': 0})

    pairs = (base.values('driver_id', 'user_id').annotate(n=Count('id'))
             .filter(n__gte=th['min_pair_trips']).order_by('-n')[:MAX_PAIRS])
    for row in pairs:
        entry = found[row['driver_id']]
        entry['signals'].add('repeat_pair')
        entry['riders'].add(row['user_id'])
        entry['pair_trips'] = max(entry['pair_trips'], row['n'])

    busy = (base.values('driver_id').annotate(total=Count('id'))
            .filter(total__gte=th['min_driver_trips']).order_by('-total')[:MAX_PAIRS])
    for row in busy:
        top = list(base.filter(driver_id=row['driver_id']).values('user_id')
                   .annotate(n=Count('id')).order_by('-n', 'user_id')[:th['top_riders']])
        share = sum(r['n'] for r in top) * 100 / row['total']
        if share >= th['concentration_pct']:
            entry = found[row['driver_id']]
            entry['signals'].add('rider_concentration')
            entry['riders'] |= {r['user_id'] for r in top}
            entry['share'] = round(share)
            entry['total'] = row['total']

    drivers = {d.pk: d for d in Driver.objects.filter(pk__in=list(found)).select_related('user_id')}
    live = _incentive_in_progress(list(found))
    hits = []
    for driver_id, entry in found.items():
        driver = drivers.get(driver_id)
        if driver is None:
            continue
        incentive = driver_id in live
        trip_ids = list(base.filter(driver_id=driver_id, user_id__in=entry['riders'])
                        .order_by('-completed_at', '-id').values_list('pk', flat=True)[:MAX_TRIP_REFS])
        score = clamp_score(40 + 5 * max(entry['pair_trips'] - th['min_pair_trips'], 0)
                            + (15 if 'rider_concentration' in entry['signals'] else 0)
                            + (25 if incentive else 0))
        signals = sorted(entry['signals']) + (['incentive_in_progress'] if incentive else [])
        severity = HIGH if incentive and len(entry['signals']) > 1 else severity_for(score)
        hits.append(Hit(
            subject=driver.user_id, severity=severity, score=score, signals=signals,
            refs={'trip_ids': sorted(trip_ids), 'counterpart_user_ids': sorted(entry['riders'])},
            measures={'top_pair_trips': entry['pair_trips'],
                      'rider_share_pct': entry.get('share'), 'driver_trips': entry.get('total')}))
    return hits


RULE = Rule(
    key='collusion', title='Rider-driver collusion', subject_role='driver',
    summary='The same rider and driver complete many trips together, or a driver\'s trips are '
            'mostly with a few riders, especially while an incentive is in progress.',
    data_used='Completed trips (rider, driver, zone country); driver incentive progress.',
    data_missing='No rider-driver relationship (family, employer) is stored, so a legitimate '
                 'daily commute looks the same as trading trips. Pickup and drop proximity '
                 'between pair trips is not compared yet.',
    params=(
        Param('window_days', 'Look back', 'days', 1, 90, 7, 7),
        Param('min_pair_trips', 'Trips by one rider-driver pair', 'trips', 2, 200, 10, 10),
        Param('min_driver_trips', 'Driver trips before concentration is judged', 'trips', 5, 500,
              20, 20),
        Param('top_riders', 'Riders counted as "a small set"', 'riders', 1, 10, 3, 3),
        Param('concentration_pct', 'Share of trips with that set', 'percent', 50, 100, 85, 85),
    ),
    evaluate=evaluate)
