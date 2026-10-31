"""incentive_farming: many very short trips that count toward a driver incentive.

Reads the trips that counted toward an incentive (`IncentiveTrip`) in the window and keeps the ones
that are short by distance and time. A driver with `min_short_trips` or more is a hit; a high share
of short trips among the counted ones raises the score.
"""

from collections import defaultdict
from decimal import Decimal

from servers.fraud.models import HIGH

from .base import Hit, Param, Rule, clamp_score, severity_for, window_start

MAX_ROWS = 5000
MAX_TRIP_REFS = 50


def is_short(trip, max_km, max_minutes):
    km, minutes = trip.actual_distance_km, trip.actual_duration_min
    if km is None and minutes is None:
        return False
    if km is not None and km >= Decimal(max_km):
        return False
    if minutes is not None and minutes >= Decimal(max_minutes):
        return False
    return True


def evaluate(th, country, now):
    from servers.promotions.models import IncentiveTrip

    rows = (IncentiveTrip.objects
            .filter(created_at__gte=window_start(now, days=th['window_days']),
                    trip__zone__country=country)
            .select_related('trip', 'driver__user_id').order_by('-created_at', '-id')[:MAX_ROWS])
    per_driver = defaultdict(lambda: {'counted': 0, 'short': [], 'campaigns': set(), 'user': None})
    for row in rows:
        entry = per_driver[row.driver_id]
        entry['user'] = row.driver.user_id
        entry['counted'] += 1
        entry['campaigns'].add(row.campaign_id)
        if is_short(row.trip, th['short_trip_km'], th['short_trip_minutes']):
            entry['short'].append(row.trip_id)
    hits = []
    for entry in per_driver.values():
        short = len(entry['short'])
        if short < th['min_short_trips']:
            continue
        share = short * 100 / entry['counted']
        score = clamp_score(35 + 6 * (short - th['min_short_trips']) + (25 if share >= 70 else 0))
        severity = (HIGH if share >= 70 and short >= 2 * th['min_short_trips']
                    else severity_for(score))
        hits.append(Hit(
            subject=entry['user'], severity=severity, score=score, signals=['short_trips_counted'],
            refs={'trip_ids': sorted(entry['short'])[:MAX_TRIP_REFS],
                  'campaign_ids': sorted(entry['campaigns'])},
            measures={'short_trips': short, 'counted_trips': entry['counted'],
                      'short_share_pct': round(share)}))
    return hits


RULE = Rule(
    key='incentive_farming', title='Incentive farming', subject_role='driver',
    summary='A driver\'s incentive progress is mostly very short trips.',
    data_used='Trips that counted toward a driver incentive; their recorded distance and duration.',
    data_missing='Recorded distance and duration come from the trip, not from an independent '
                 'source; a trip with neither is not judged. Pickup and drop being the same '
                 'place is not compared.',
    params=(
        Param('window_days', 'Look back', 'days', 1, 30, 3, 3),
        Param('short_trip_km', 'A trip under this distance is short', 'km', 1, 20, 1, 1),
        Param('short_trip_minutes', 'and under this time', 'minutes', 1, 60, 4, 4),
        Param('min_short_trips', 'Short counted trips before a flag', 'trips', 2, 100, 6, 6),
    ),
    evaluate=evaluate)
