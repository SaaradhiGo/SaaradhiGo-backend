"""Sampling and validation for the durable GPS trail.

Driver location was Redis-only, so nothing survived a trip. This module decides
**which** of a driver's pings become durable rows, and it is deliberately the
only place that decision lives.

Why sampling rather than storing everything
-------------------------------------------
`servers/redis_client.py` sizes the location hot path for 1000 pings/second.
Persisting every ping would be 1000 inserts/second of mostly redundant data —
a stationary driver waiting at a pickup emits the same coordinate over and
over. The trail only has to be good enough to reconstruct a route, compute
actual distance, and answer "where were they at time T". A point every few
seconds or every few tens of metres does that; a point every 200ms does not do
it any better and costs storage, write throughput and query time.

Four filters, cheapest first:

1. **Validity** — coordinates must parse and be in range. A malformed or
   null-island ping is dropped, never stored.
2. **Accuracy** — a device-reported accuracy worse than
   `MAX_ACCURACY_METRES` is noise. Storing a 500m-accurate point next to a
   5m-accurate one makes the derived distance worse, not better.
3. **Time** — at most one point per `MIN_INTERVAL_SECONDS`.
4. **Distance** — a point closer than `MIN_DISTANCE_METRES` to the last kept
   point is dropped, which is what stops a stationary driver filling the table.

Endpoints are always kept: the first point of a trip, and any point explicitly
marked final, regardless of thresholds. Without that, a short trip could
reduce to a single point and actual distance would be unrecoverable.

Privacy
-------
Coordinates are never logged by this module. `base.logging_filters` redacts
`lat`/`lng`-shaped keys, but the rule here is simpler: log counts and trip ids,
never positions. Rider locations are never sampled — only the assigned driver's
track, and only while the trip is in a driver-active status according to
PostgreSQL.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from math import asin, cos, radians, sin, sqrt

logger = logging.getLogger(__name__)

# A phone reporting worse than this is guessing — usually an indoor or
# cold-start fix. Roughly the width of a wide road.
MAX_ACCURACY_METRES = 50.0

# One point per this interval is enough to reconstruct a city route.
MIN_INTERVAL_SECONDS = 5.0

# Below this, successive points are GPS jitter rather than movement. Keeps a
# driver idling at a pickup from generating thousands of rows.
MIN_DISTANCE_METRES = 25.0

# Defence against a broken client: never accept more than this many points for
# one trip. A 60-minute trip sampled every 5s is ~720 points, so this is ~7x
# headroom before something is clearly wrong.
MAX_POINTS_PER_TRIP = 5000

_EARTH_RADIUS_M = 6_371_000.0


@dataclass(frozen=True)
class Candidate:
    """One incoming ping, before any decision has been made about it."""
    latitude: Decimal
    longitude: Decimal
    recorded_at: object            # datetime
    accuracy_m: float | None = None
    speed_kmh: float | None = None
    heading_deg: float | None = None
    is_final: bool = False


class Rejected(Exception):
    """A candidate that must not be stored. Carries a stable machine reason."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def parse_coordinate(lat, lon):
    """Return (Decimal, Decimal) or raise Rejected.

    Strings are accepted because the Redis stream stores everything as bytes or
    str. Decimal, not float: these are compared against Trip.pickup_lat, which
    is a DecimalField, and float round-tripping introduces drift.
    """
    try:
        lat_d = Decimal(str(lat))
        lon_d = Decimal(str(lon))
    except (InvalidOperation, TypeError, ValueError):
        raise Rejected('unparseable_coordinate')

    if not (Decimal('-90') <= lat_d <= Decimal('90')):
        raise Rejected('latitude_out_of_range')
    if not (Decimal('-180') <= lon_d <= Decimal('180')):
        raise Rejected('longitude_out_of_range')
    # Exact 0,0 is the classic "no fix" sentinel, not a location off Africa.
    if lat_d == 0 and lon_d == 0:
        raise Rejected('null_island')
    return lat_d, lon_d


def haversine_metres(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres. Floats are fine for a threshold test."""
    p1, p2 = radians(float(lat1)), radians(float(lat2))
    dp = p2 - p1
    dl = radians(float(lon2)) - radians(float(lon1))
    h = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_M * asin(sqrt(min(1.0, h)))


def accuracy_is_acceptable(accuracy_m):
    """Unknown accuracy is accepted: many Android builds omit it entirely, and
    refusing those would mean no trail at all on those devices."""
    if accuracy_m is None:
        return True
    try:
        return float(accuracy_m) <= MAX_ACCURACY_METRES
    except (TypeError, ValueError):
        return True


def should_keep(candidate, last_kept):
    """Decide whether `candidate` becomes a durable row.

    `last_kept` is the previously stored point for this trip, or None for the
    first. Returns (True, 'reason_kept') or (False, 'reason_dropped') rather
    than a bare bool, so the caller can count drop reasons without re-deriving
    them.
    """
    if not accuracy_is_acceptable(candidate.accuracy_m):
        return False, 'accuracy_too_poor'

    # First point of a trip, and any explicitly final point, are structural:
    # without them a short trip's distance cannot be reconstructed.
    if last_kept is None:
        return True, 'first_point'
    if candidate.is_final:
        return True, 'final_point'

    prev_time = getattr(last_kept, 'recorded_at', None)
    if prev_time is not None and candidate.recorded_at is not None:
        # A device clock that jumps backwards is untrustworthy for ordering but
        # the point itself may still be real, so fall through to distance.
        delta = (candidate.recorded_at - prev_time).total_seconds()
        if 0 <= delta < MIN_INTERVAL_SECONDS:
            return False, 'too_soon'

    moved = haversine_metres(
        last_kept.latitude, last_kept.longitude,
        candidate.latitude, candidate.longitude,
    )
    if moved < MIN_DISTANCE_METRES:
        return False, 'too_close'

    return True, 'kept'


def sample(candidates, last_kept=None):
    """Reduce a batch of candidates to the points worth storing.

    Pure: takes and returns plain objects, touches no database and no Redis, so
    the policy can be tested without either. The batch shape mirrors how points
    will actually arrive — drained from `driver_location_stream` in groups —
    rather than one at a time.

    Returns (kept, stats) where stats counts each drop reason.
    """
    kept = []
    stats = {'received': 0, 'kept': 0}
    previous = last_kept

    for raw in candidates:
        stats['received'] += 1
        keep, reason = should_keep(raw, previous)
        if keep:
            if len(kept) >= MAX_POINTS_PER_TRIP:
                stats['over_trip_cap'] = stats.get('over_trip_cap', 0) + 1
                continue
            kept.append(raw)
            previous = raw
            stats['kept'] += 1
        else:
            stats[reason] = stats.get(reason, 0) + 1

    return kept, stats


def trip_is_collecting(trip):
    """True while this trip should be accumulating a trail.

    Reads the durable status, reusing the same canonical set as the driver
    active-trip invariant, so "collect a trail" and "this driver is busy" can
    never disagree. Collection therefore stops the instant a trip becomes
    terminal, with no separate flag to get out of sync.
    """
    from servers.ride.models import DRIVER_ACTIVE_TRIP_STATUSES

    if trip is None or trip.driver_id_id is None:
        return False
    code = trip.status_id.status_code if trip.status_id else None
    return code in DRIVER_ACTIVE_TRIP_STATUSES
