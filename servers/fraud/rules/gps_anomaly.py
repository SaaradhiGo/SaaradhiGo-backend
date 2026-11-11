"""gps_anomaly: a driver whose trip trails do not add up.

Two tests per completed trip, over the durable sampled trail (`TripLocationPoint`) from the trip's
start to its end:

* impossible jumps: a segment implying more than `MAX_SPEED_KMH` (200, the same line the operator
  end-trip fare uses, `ride.operator_end`) is a jump;
* a trail far short of the billed distance: with at least `min_points` points and a billed
  distance of `min_billed_km` or more, a trail shorter than `min_trail_pct` percent of it.

A trip is anomalous when it has `min_jumps` or more jumps, or a short trail. A driver with
`min_anomalous_trips` anomalous trips in the window is a hit. Nothing here changes a fare.
"""

from decimal import Decimal
from itertools import groupby

from servers.fraud.models import HIGH

from .base import Hit, Param, Rule, clamp_score, completed_status_id, severity_for, window_start

MAX_TRIPS = 500
MAX_TRIP_REFS = 50


def measure_trail(points):
    """`(jumps, trail_km)` for time-ordered points, with operator_end's jump rule."""
    from servers.ride.location_trail import haversine_metres
    from servers.ride.operator_end import MAX_SPEED_KMH

    jumps, total_m = 0, 0.0
    for prev, cur in zip(points, points[1:], strict=False):
        seg_m = haversine_metres(prev[0], prev[1], cur[0], cur[1])
        gap_s = (cur[2] - prev[2]).total_seconds()
        if gap_s > 0:
            if (seg_m / 1000.0) / (gap_s / 3600.0) > MAX_SPEED_KMH:
                jumps += 1
                continue
        elif seg_m > 50.0:
            jumps += 1
            continue
        total_m += seg_m
    return jumps, total_m / 1000.0


def judge(points, billed_km, th):
    """The anomaly signals for one trip's points: a list, empty when the trip looks fine."""
    jumps, trail_km = measure_trail(points)
    signals = []
    if jumps >= th['min_jumps']:
        signals.append('impossible_jump')
    if (billed_km is not None and Decimal(billed_km) >= th['min_billed_km']
            and len(points) >= th['min_points']
            and trail_km * 100 < float(billed_km) * th['min_trail_pct']):
        signals.append('trail_short_of_billed')
    return signals, jumps, trail_km


def evaluate(th, country, now):
    from servers.ride.models import Trip, TripLocationPoint

    done = completed_status_id()
    if done is None:
        return []
    trips = list(Trip.objects.filter(
        status_id=done, completed_at__gte=window_start(now, hours=th['window_hours']),
        zone__country=country, driver_id__isnull=False, started_at__isnull=False)
        .select_related('driver_id__user_id').order_by('-completed_at', '-id')[:MAX_TRIPS])
    if not trips:
        return []
    by_id = {t.pk: t for t in trips}
    rows = (TripLocationPoint.objects.filter(trip_id__in=list(by_id))
            .order_by('trip_id', 'recorded_at', 'sequence', 'id')
            .values_list('trip_id', 'latitude', 'longitude', 'recorded_at'))
    per_driver = {}
    for trip_id, group in groupby(rows, key=lambda r: r[0]):
        trip = by_id[trip_id]
        points = [(r[1], r[2], r[3]) for r in group if r[3] >= trip.started_at]
        signals, jumps, trail_km = judge(points, trip.actual_distance_km, th)
        if not signals:
            continue
        entry = per_driver.setdefault(trip.driver_id_id, {
            'user': trip.driver_id.user_id, 'trips': [], 'signals': set(), 'jumps': 0})
        entry['trips'].append(trip_id)
        entry['signals'] |= set(signals)
        entry['jumps'] += jumps
    hits = []
    for entry in per_driver.values():
        count = len(entry['trips'])
        if count < th['min_anomalous_trips']:
            continue
        score = clamp_score(40 + 12 * (count - th['min_anomalous_trips'])
                            + (10 if len(entry['signals']) > 1 else 0))
        severity = HIGH if count >= 2 * th['min_anomalous_trips'] else severity_for(score)
        hits.append(Hit(
            subject=entry['user'], severity=severity, score=score, signals=sorted(entry['signals']),
            refs={'trip_ids': sorted(entry['trips'])[:MAX_TRIP_REFS]},
            measures={'anomalous_trips': count, 'jumps': entry['jumps']}))
    return hits


RULE = Rule(
    key='gps_anomaly', title='GPS anomaly', subject_role='driver',
    summary='Completed trips whose location trail has impossible jumps, or is far shorter than '
            'the distance billed.',
    data_used='Completed trips in the last day; their sampled location trail and billed distance.',
    data_missing='The app does not send a mock-location or developer-options flag, the strongest '
                 'spoofing signal, nor the device\'s own sensor speed. A dead zone also leaves a '
                 'short trail, so a single trip is never enough.',
    params=(
        Param('window_hours', 'Look back', 'hours', 1, 168, 24, 24),
        Param('min_jumps', 'Impossible jumps for a trip to count', 'jumps', 1, 50, 2, 2),
        Param('min_billed_km', 'Judge trail length only for trips billed at least', 'km', 1, 100,
              3, 3),
        Param('min_points', 'and with at least this many trail points', 'points', 2, 200, 5, 5),
        Param('min_trail_pct', 'Trail shorter than this share of billed is short', 'percent', 5,
              95, 50, 50),
        Param('min_anomalous_trips', 'Anomalous trips before a flag', 'trips', 1, 50, 2, 2),
    ),
    evaluate=evaluate)
