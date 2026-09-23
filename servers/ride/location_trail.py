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

from django.db.models import Q

logger = logging.getLogger(__name__)

# Policy thresholds. Read through helpers rather than captured at import time,
# so an environment can be retuned with a variable instead of a release, and so
# tests can override them with `settings`.
#
# The module-level names are kept as the documented defaults and as the value a
# caller gets when Django settings are unavailable.

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


def _policy(name, default):
    from django.conf import settings
    return getattr(settings, f'GPS_TRAIL_{name}', default)


def max_accuracy_metres():
    return float(_policy('MAX_ACCURACY_METRES', MAX_ACCURACY_METRES))


def min_interval_seconds():
    return float(_policy('MIN_INTERVAL_SECONDS', MIN_INTERVAL_SECONDS))


def min_distance_metres():
    return float(_policy('MIN_DISTANCE_METRES', MIN_DISTANCE_METRES))


def max_points_per_trip():
    return int(_policy('MAX_POINTS_PER_TRIP', MAX_POINTS_PER_TRIP))


def trail_enabled():
    from django.conf import settings
    return bool(getattr(settings, 'GPS_TRAIL_ENABLED', False))

_EARTH_RADIUS_M = 6_371_000.0


def _log(event, **fields):
    """Structured trail log line. Counts and ids only, never positions.

    The redaction filter in base.logging_filters would strip lat/lng-shaped
    keys anyway, but the rule for this module is simpler: do not pass them.
    """
    logger.info(event, extra={'event': event, **fields})


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
        return float(accuracy_m) <= max_accuracy_metres()
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
        if 0 <= delta < min_interval_seconds():
            return False, 'too_soon'

    moved = haversine_metres(
        last_kept.latitude, last_kept.longitude,
        candidate.latitude, candidate.longitude,
    )
    if moved < min_distance_metres():
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
            if len(kept) >= max_points_per_trip():
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


# ---------------------------------------------------------------------------
# The durable writer
# ---------------------------------------------------------------------------

def _stream_id_to_datetime(entry_id):
    """Redis stream ids are "<ms>-<seq>". The ms half is server receive time.

    This is the best timestamp available for MVP: the driver app ping carries
    only lat/lng, with no device clock, accuracy, speed or heading. So
    `recorded_at` means "when the fix reached the server" and `received_at`
    means "when it was persisted" -- they genuinely differ, because persistence
    is batched. When the client protocol gains a device timestamp, only this
    function and the Candidate construction below need to change.
    """
    from datetime import datetime
    from datetime import timezone as _tz
    try:
        ms = int(str(entry_id).split('-')[0])
    except (ValueError, IndexError):
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=_tz.utc)


def _sequence_from_entry_id(entry_id):
    """Deterministic ordering key derived from the stream id.

    Derived rather than counted, so re-processing an entry produces the same
    value and ordering stays stable across a retry.
    """
    try:
        ms, _sep, _seq = str(entry_id).partition('-')
        return int(ms) % 2_000_000_000
    except ValueError:
        return None


def late_arrival_minutes():
    """How long after a trip ends its pings are still accepted.

    The drain is periodic, so the pings from the last stretch of a journey are
    always still in the stream when the trip completes. Without this window they
    would be discarded, and every trip's measured distance would be short by
    however far the driver travelled since the previous drain -- systematically,
    and invisibly. A lagging or restarted worker would lose a whole trail.
    """
    from django.conf import settings

    return int(getattr(settings, 'GPS_TRAIL_LATE_ARRIVAL_MINUTES', 60))


def collection_window(trip):
    """(start, end) during which this trip should accumulate a trail.

    `end` is None while the trip is still live. `accepted_at` is the start
    because that is when a driver is assigned and their movement first belongs
    to this trip; the narrower question of what is *billable* is decided later,
    by the journey window in `ride.actual_metrics`, which uses `started_at`.
    Storing the approach and billing only the journey keeps the approach
    available for dispute review without it ever reaching a fare.
    """
    start = trip.accepted_at or trip.requested_at
    end = trip.completed_at or trip.cancelled_at
    return start, end


def _resolve_candidate_trips(driver_ids):
    """driver_id -> [Trip], newest first, for matching events to trips by time.

    Resolved from PostgreSQL, never from Redis: the collection window must
    follow durable truth. One query for the whole batch rather than one per
    event.

    Includes trips that have already ended, within `late_arrival_minutes`. This
    is the difference between measuring a journey and measuring a journey minus
    its last minute: a ping is assigned to the trip whose window contains the
    moment it was *recorded*, not to whatever the driver happens to be doing
    when the drain runs.
    """
    from datetime import timedelta

    from django.utils import timezone

    from servers.ride.models import DRIVER_ACTIVE_TRIP_STATUSES, Trip

    if not driver_ids:
        return {}

    grace_cutoff = timezone.now() - timedelta(minutes=late_arrival_minutes())
    rows = (
        Trip.objects
        .filter(driver_id__in=list(driver_ids))
        .filter(
            Q(status_id__status_code__in=DRIVER_ACTIVE_TRIP_STATUSES)
            | Q(completed_at__gte=grace_cutoff)
            | Q(cancelled_at__gte=grace_cutoff)
        )
        .select_related('status_id')
        .order_by('driver_id', '-requested_at')
    )
    out = {}
    for trip in rows:
        out.setdefault(trip.driver_id_id, []).append(trip)
    return out


def trip_for_event(trips, recorded_at):
    """The trip whose collection window contains this moment, or None.

    Newest first, so a driver who has already started their next trip attributes
    new pings to it while a straggler from the previous trip still lands on the
    previous one.
    """
    for trip in trips or ():
        if trip.driver_id_id is None:
            continue
        start, end = collection_window(trip)
        if start is not None and recorded_at < start:
            continue
        if end is not None and recorded_at > end:
            continue
        return trip
    return None


def _last_point_for_trips(trip_ids):
    """Most recently recorded stored point per trip, for threshold continuity.

    Without this every batch would treat its first event as a trip's first
    point, resetting the time and distance thresholds and letting a stationary
    driver accumulate one row per batch.
    """
    from servers.ride.models import TripLocationPoint

    if not trip_ids:
        return {}
    rows = (
        TripLocationPoint.objects
        .filter(trip_id__in=list(trip_ids))
        .order_by('trip_id', '-recorded_at')
    )
    out = {}
    for p in rows:
        out.setdefault(p.trip_id, p)
    return out


def persist_location_events(events):
    """Turn raw stream events into durable points. Returns (handled_ids, stats).

    Free of Redis: it takes already-read events and returns the ids that were
    handled, so the Celery task owns reading and acknowledging while this owns
    the decision. That split is what lets the whole policy be tested without a
    broker.

    Events whose driver has no active trip are **handled but not stored** --
    off-trip driver location is deliberately never persisted, and leaving those
    entries pending forever would grow the consumer group backlog without
    limit.
    """
    from django.db import transaction

    from servers.ride.models import TripLocationPoint

    stats = {'received': len(events), 'stored': 0, 'no_active_trip': 0,
             'invalid': 0, 'sampled_out': 0}
    if not events:
        return [], stats

    parsed = []
    handled = []
    for entry_id, fields in events:
        handled.append(entry_id)
        try:
            driver_id = int(fields.get('driver_id'))
        except (TypeError, ValueError):
            stats['invalid'] += 1
            continue
        try:
            lat, lon = parse_coordinate(fields.get('lat'), fields.get('lng'))
        except Rejected as exc:
            stats['invalid'] += 1
            key = 'invalid_' + exc.reason
            stats[key] = stats.get(key, 0) + 1
            continue
        recorded_at = _stream_id_to_datetime(entry_id)
        if recorded_at is None:
            stats['invalid'] += 1
            continue
        parsed.append((entry_id, driver_id, lat, lon, recorded_at))

    if not parsed:
        return handled, stats

    trips_by_driver = _resolve_candidate_trips({p[1] for p in parsed})
    last_points = _last_point_for_trips(
        {t.id for trips in trips_by_driver.values() for t in trips}
    )

    to_create = []
    for entry_id, driver_id, lat, lon, recorded_at in parsed:
        # Matched on when the ping was recorded, not on what the driver is doing
        # now -- see _resolve_candidate_trips.
        trip = trip_for_event(trips_by_driver.get(driver_id), recorded_at)
        if trip is None:
            stats['no_active_trip'] += 1
            continue

        candidate = Candidate(latitude=lat, longitude=lon, recorded_at=recorded_at)
        keep, reason = should_keep(candidate, last_points.get(trip.id))
        if not keep:
            stats['sampled_out'] += 1
            stats[reason] = stats.get(reason, 0) + 1
            continue

        point = TripLocationPoint(
            trip=trip, driver_id=driver_id,
            latitude=lat, longitude=lon,
            recorded_at=recorded_at,
            sequence=_sequence_from_entry_id(entry_id),
            source=TripLocationPoint.SOURCE_DRIVER_WS,
            source_event_id=entry_id,
        )
        to_create.append(point)
        # Advance the in-memory cursor so thresholds apply within this batch
        # too, not only against what was already stored.
        last_points[trip.id] = point

    if to_create:
        # ignore_conflicts leans on the (trip, source_event_id) unique
        # constraint: a redelivered entry is skipped rather than raising, which
        # is what makes at-least-once delivery safe without needing
        # exactly-once.
        with transaction.atomic():
            TripLocationPoint.objects.bulk_create(to_create, ignore_conflicts=True)
        stats['stored'] = len(to_create)

    return handled, stats


def drain_location_stream(consumer='worker-1'):
    """Read pending location events and persist the ones worth keeping.

    Bounded by GPS_TRAIL_BATCH_SIZE per read and GPS_TRAIL_MAX_EVENTS_PER_RUN
    in total, so a backlog cannot turn one scheduler tick into an unbounded
    unit of work.

    A no-op while GPS_TRAIL_ENABLED is False, which is the default: the writer
    can ship and be switched on per environment afterwards.
    """
    from django.conf import settings

    from servers.redis_client import (
        ack_location_events, ensure_location_stream_group, read_location_events,
    )

    if not trail_enabled():
        return {'enabled': False}

    batch = int(getattr(settings, 'GPS_TRAIL_BATCH_SIZE', 500))
    budget = int(getattr(settings, 'GPS_TRAIL_MAX_EVENTS_PER_RUN', 10000))

    ensure_location_stream_group()

    totals = {'enabled': True, 'runs': 0, 'received': 0, 'stored': 0,
              'no_active_trip': 0, 'invalid': 0, 'sampled_out': 0, 'acked': 0}
    consumed = 0

    while consumed < budget:
        events = read_location_events(
            count=min(batch, budget - consumed), consumer=consumer,
        )
        if not events:
            break
        consumed += len(events)
        totals['runs'] += 1

        handled, stats = persist_location_events(events)
        for k in ('received', 'stored', 'no_active_trip', 'invalid', 'sampled_out'):
            totals[k] += stats.get(k, 0)

        # Acknowledge only after the rows are committed. If the worker dies
        # before this, the entries stay pending and are re-processed, which the
        # unique constraint renders harmless.
        totals['acked'] += ack_location_events(handled)

    _log('gps_trail_drained', **{k: v for k, v in totals.items() if k != 'enabled'})
    return totals
