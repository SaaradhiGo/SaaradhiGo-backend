"""Derive actual trip distance and duration from durable evidence.

OBSERVE ONLY. This module writes `Trip.actual_distance_km` and
`Trip.actual_duration_min` and nothing else. It never touches `final_fare`,
payments, wallets, commission, settlement, receipts or GMV. The point is to
learn what metered billing *would* do before anything changes what a rider is
charged.

The passenger-journey boundary, proven rather than assumed
---------------------------------------------------------
Billable distance and time must cover the passenger's journey, not the driver's
approach to the pickup. The lifecycle proves where that starts:

* `in_progress` is the **only** transition gated on the rider's OTP
  (`_update_trip_status('in_progress', otp_input=...)` compares `trip.otp`).
  The OTP is, in the codebase's own words, "the rider's secret to read aloud to
  the driver at pickup" — so a successful verification is evidence the rider is
  physically with the driver.
* `in_progress` sets `started_at`. `reached` sets `reached_at`, which is the
  driver *arriving* at the pickup with the rider not yet aboard.
* The transition table only allows `completed` from `in_progress`, and
  `completed` sets `completed_at`.

So the journey is exactly **`started_at` → `completed_at`**, and
`accepted_at → started_at` is unbilled approach time. Points outside that window
are excluded.

Why duration comes from timestamps and distance from GPS
--------------------------------------------------------
**Duration uses the lifecycle timestamps**, not a sum of GPS intervals. They are
authoritative server-side facts, they are always present on a completed trip,
and they do not degrade when telemetry is patchy. Summing GPS intervals would
silently under-report exactly when coverage is worst, which is the opposite of
what a billing input should do.

**Distance has no such authority available**, so it is summed from the trail.
That is also why every result carries coverage statistics: a distance derived
from 40% coverage is not the same fact as one derived from 95%, and the
fare-finalisation policy will need to tell them apart.

No external maps API is used. Fare completion must never depend on Google or
Mapbox availability, so the calculation is local, deterministic and replaceable:
swap `_segment_distance_m` and nothing else changes.
"""

import logging
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings

logger = logging.getLogger(__name__)

# A segment implying a speed above this is not a car on a road — it is a GPS
# jump, a cold-start fix landing in the wrong cell, or two devices sharing a
# driver id. Such a segment is skipped rather than summed, because a single
# 40km jump would otherwise dominate the whole trip's distance.
DEFAULT_MAX_SEGMENT_SPEED_KMH = 150.0

# A gap longer than this means the trail stopped telling us anything for a
# while. The distance across it is a straight line, which under-reads a real
# route, so gaps are measured and reported rather than hidden.
DEFAULT_MAX_ACCEPTABLE_GAP_SECONDS = 120.0


def _cfg(name, default):
    return getattr(settings, f'TRIP_ACTUALS_{name}', default)


def max_segment_speed_kmh():
    return float(_cfg('MAX_SEGMENT_SPEED_KMH', DEFAULT_MAX_SEGMENT_SPEED_KMH))


def max_acceptable_gap_seconds():
    return float(_cfg('MAX_ACCEPTABLE_GAP_SECONDS', DEFAULT_MAX_ACCEPTABLE_GAP_SECONDS))


def _two(value):
    return Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def journey_window(trip):
    """(start, end) of the passenger journey, or (None, None).

    Returns None rather than guessing when either boundary is missing: a trip
    with no `started_at` never had a rider aboard, so it has no billable
    journey, and inventing one would be worse than declining to compute.
    """
    start = getattr(trip, 'started_at', None)
    end = getattr(trip, 'completed_at', None)
    if start is None or end is None or end < start:
        return None, None
    return start, end


def journey_points(trip):
    """Trail points inside the passenger journey, deterministically ordered.

    Ordered by `(recorded_at, sequence, id)` so the same trip always produces
    the same distance. `recorded_at` alone is not enough: two points can share a
    timestamp when a device flushes a buffer, and an unstable tie-break would
    make the result depend on row order.
    """
    from servers.ride.models import TripLocationPoint

    start, end = journey_window(trip)
    if start is None:
        return []
    return list(
        TripLocationPoint.objects
        .filter(trip=trip, recorded_at__gte=start, recorded_at__lte=end)
        .order_by('recorded_at', 'sequence', 'id')
    )


def _segment_distance_m(a, b):
    """Distance between two consecutive points. Replaceable by design."""
    from servers.ride.location_trail import haversine_metres

    return haversine_metres(a.latitude, a.longitude, b.latitude, b.longitude)


def compute_metrics(trip):
    """Derive distance and duration plus the evidence quality behind them.

    Pure: reads, computes, returns. Writes nothing, so it is safe to call for
    analysis, from a shell, or from a test without a transaction.

    Returns a dict with `distance_km`, `duration_min`, and the coverage figures
    the fare policy will need. `distance_km` is None when there is not enough
    evidence to sum a route — deliberately distinct from `0.00`, which means a
    trip that genuinely did not move.
    """
    start, end = journey_window(trip)
    if start is None:
        return {
            'ok': False, 'reason': 'no_journey_window',
            'distance_km': None, 'duration_min': None,
            'points': 0, 'coverage_ratio': None, 'max_gap_seconds': None,
            'rejected_segments': 0,
        }

    duration_seconds = (end - start).total_seconds()
    duration_min = _two(duration_seconds / 60.0)

    points = journey_points(trip)
    if len(points) < 2:
        # One point (or none) cannot describe a path. Duration is still
        # authoritative, so it is returned; distance is explicitly unknown.
        return {
            'ok': False, 'reason': 'insufficient_points',
            'distance_km': None, 'duration_min': duration_min,
            'points': len(points), 'coverage_ratio': Decimal('0.00'),
            'max_gap_seconds': None, 'rejected_segments': 0,
        }

    total_m = 0.0
    rejected = 0
    max_gap = 0.0
    speed_limit = max_segment_speed_kmh()

    # strict=False is correct here: points[1:] is one shorter by construction.
    for prev, cur in zip(points, points[1:], strict=False):
        gap_s = (cur.recorded_at - prev.recorded_at).total_seconds()
        if gap_s > max_gap:
            max_gap = gap_s

        seg_m = _segment_distance_m(prev, cur)

        # Implausible-jump filter. Guard against a zero/negative gap, which
        # would otherwise divide by zero and reject a legitimate point pair.
        if gap_s > 0:
            implied_kmh = (seg_m / 1000.0) / (gap_s / 3600.0)
            if implied_kmh > speed_limit:
                rejected += 1
                continue

        total_m += seg_m

    # Coverage: how much of the journey the trail actually spans. A distance
    # from 40% coverage is not the same fact as one from 95%, and the fare
    # policy has to be able to tell them apart.
    span_s = (points[-1].recorded_at - points[0].recorded_at).total_seconds()
    coverage = Decimal('0.00')
    if duration_seconds > 0:
        coverage = _two(min(1.0, max(0.0, span_s / duration_seconds)))

    return {
        'ok': True, 'reason': 'computed',
        'distance_km': _two(total_m / 1000.0),
        'duration_min': duration_min,
        'points': len(points),
        'coverage_ratio': coverage,
        'max_gap_seconds': _two(max_gap),
        'rejected_segments': rejected,
        'gap_exceeds_threshold': max_gap > max_acceptable_gap_seconds(),
    }


def record_actuals(trip, force=False):
    """Persist the derived metrics onto the Trip. OBSERVE ONLY.

    Writes at most `actual_distance_km` and `actual_duration_min`, via a narrow
    `update_fields`, so it cannot disturb any other column — in particular it
    can never touch `final_fare`, which is what keeps this phase away from
    billing.

    Idempotent: the computation is deterministic for a given trail, so a repeat
    produces the same values. Already-populated trips are skipped unless
    `force`, so a redelivered task neither rewrites history nor fights a manual
    correction.
    """
    result = compute_metrics(trip)

    already = trip.actual_distance_km is not None or trip.actual_duration_min is not None
    if already and not force:
        result['written'] = False
        result['skipped'] = 'already_populated'
        return result

    fields = []
    if result['duration_min'] is not None:
        trip.actual_duration_min = result['duration_min']
        fields.append('actual_duration_min')
    if result['distance_km'] is not None:
        trip.actual_distance_km = result['distance_km']
        fields.append('actual_distance_km')

    if fields:
        trip.save(update_fields=fields)

    result['written'] = bool(fields)
    result['fields'] = fields

    # Ids and numbers only; never coordinates.
    logger.info(
        'trip_actuals_recorded',
        extra={
            'event': 'trip_actuals_recorded',
            'trip_id': trip.id,
            'ok': result['ok'],
            'reason': result['reason'],
            'points': result['points'],
            'distance_km': str(result['distance_km']),
            'duration_min': str(result['duration_min']),
            'coverage_ratio': str(result['coverage_ratio']),
            'rejected_segments': result['rejected_segments'],
            'written': result['written'],
        },
    )
    return result
