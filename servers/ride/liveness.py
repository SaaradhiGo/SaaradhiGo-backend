"""Is this active ride still alive, and if not, who decides what to do?

WHY THIS EXISTS
---------------
QA trip 42 reached `in_progress` and both clients disappeared. What followed was
not a crash; it was a quiet, permanent loss of supply:

  * the rider could not cancel a started ride, which is correct policy;
  * `auto_cancel_trip` only covers trips nobody ACCEPTED, so no timeout applied;
  * `driver:active_trip:<id>` in Redis has no TTL and nothing cleared it;
  * `add_driver_location` removes a driver from the geo index whenever that key
    is set, so even after the driver reconnected and pinged, dispatch kept
    reporting `drivers_notified=0`;
  * `/ride/active/` returned the dead trip forever.

Recovery took an engineer opening the driver's WebSocket and sending `complete`.
In a ten-driver pilot that is ten percent of supply gone until someone with a
shell notices.

WHAT THIS IS NOT
----------------
It is not `TIMEOUT -> CANCEL`. Absence of connectivity is not evidence of
abandonment. A legitimate ride can cross a tunnel, sit in a basement car park,
run for hours in traffic, be backgrounded by an aggressive OEM battery manager,
or end with a flat battery while the driver is still driving the passenger.

Cancelling those would be worse than the bug: it would abandon rides that were
in progress and hand riders a cancelled trip mid-journey.

So the model is:

    DETECTION  ->  RECOVERY  ->  ESCALATION  ->  OPERATOR RESOLUTION

Nothing here changes a trip's status, its fare, or any settlement. The detector
raises a flag; a human decides. The only state this module repairs on its own is
*ephemeral* Redis state that PostgreSQL already disagrees with, because that is
not a decision -- it is a cache being wrong.
"""

import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

# Liveness classifications. Operational vocabulary, deliberately distinct from
# trip status so nobody can confuse "looks quiet" with "is cancelled".
HEALTHY = 'healthy'
STALE = 'stale'
RECOVERY_REQUIRED = 'recovery_required'
OPERATOR_ATTENTION = 'operator_attention'


def _seconds(name, default):
    return int(getattr(settings, name, default))


def write_interval_seconds():
    """How rarely a durable activity write is allowed.

    A driver pings roughly 12 times a minute. Writing `last_driver_activity_at`
    on every ping would mean 12 UPDATEs per minute per active trip, which at 50
    concurrent rides is 600 write transactions a minute for information nobody
    needs to the second. Coalescing to once a minute keeps the evidence useful
    and the write amplification bounded.
    """
    return _seconds('TRIP_LIVENESS_WRITE_INTERVAL_SECONDS', 60)


def stale_after_seconds():
    """Silence beyond this is worth a LOOK, not an action.

    Generous on purpose. The heartbeat TTL is 45 seconds, so a driver crossing a
    tunnel is already invisible in Redis long before this fires.
    """
    return _seconds('TRIP_STALE_AFTER_SECONDS', 600)


def attention_after_seconds():
    """Silence beyond this is worth a PERSON.

    Still not an action. A ride can legitimately be this long; what cannot be
    legitimate is nobody having looked at it.
    """
    return _seconds('TRIP_OPERATOR_ATTENTION_AFTER_SECONDS', 1800)


# ---------------------------------------------------------------------------
# Recording activity
# ---------------------------------------------------------------------------

def record_driver_activity(trip_id, when=None):
    """Note that the driver was alive on this trip, at most once per interval.

    Coalesced with a conditional UPDATE rather than a read-then-write, so two
    concurrent pings cannot both decide they are the one that should write.
    Returns True if a row was actually updated.

    Deliberately narrow: it touches only the activity column and clears any stale
    flag. It never looks at status, money, or the driver's availability -- an
    active driver is evidence, not a decision.
    """
    from servers.ride.models import Trip

    if not trip_id:
        return False
    now = when or timezone.now()
    cutoff = now - timezone.timedelta(seconds=write_interval_seconds())

    from django.db.models import Q
    try:
        updated = Trip.objects.filter(
            Q(last_driver_activity_at__lt=cutoff)
            | Q(last_driver_activity_at__isnull=True),
            id=trip_id,
        ).update(
            last_driver_activity_at=now,
            # Activity is the answer to a stale flag. Clearing it here means a
            # driver who comes back needs no operator involvement at all, which
            # is the whole point of RECOVERY sitting before ESCALATION.
            stale_flagged_at=None,
            stale_reason='',
        )
    except Exception as exc:  # noqa: BLE001
        # Liveness bookkeeping must never break a GPS ping. A missed write costs
        # one minute of resolution; a raised exception costs the trip's telemetry.
        logger.warning('liveness: could not record driver activity for trip %s: %s',
                       trip_id, exc)
        return False
    return bool(updated)


def record_rider_activity(trip_id, when=None):
    """Advisory only. A rider's absence must never block a driver finishing."""
    from servers.ride.models import Trip

    if not trip_id:
        return False
    now = when or timezone.now()
    cutoff = now - timezone.timedelta(seconds=write_interval_seconds())
    from django.db.models import Q
    try:
        return bool(Trip.objects.filter(
            Q(last_rider_activity_at__lt=cutoff)
            | Q(last_rider_activity_at__isnull=True),
            id=trip_id,
        ).update(last_rider_activity_at=now))
    except Exception as exc:  # noqa: BLE001
        logger.warning('liveness: could not record rider activity for trip %s: %s',
                       trip_id, exc)
        return False


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def last_known_activity(trip):
    """Best durable evidence that this trip was alive, newest wins.

    Falls back through the lifecycle timestamps so a trip that has never had an
    activity write still classifies sensibly instead of looking infinitely stale.
    """
    candidates = [
        trip.last_driver_activity_at,
        trip.started_at,
        trip.reached_at,
        trip.accepted_at,
        trip.requested_at,
    ]
    return max([c for c in candidates if c], default=None)


def classify(trip, now=None):
    """Return (classification, silent_seconds, reason).

    Pure: reads the trip, touches nothing. Safe to call from a serializer, an
    operator view, or a test.
    """
    now = now or timezone.now()
    last = last_known_activity(trip)
    if last is None:
        return OPERATOR_ATTENTION, None, 'no_activity_evidence'

    silent = int((now - last).total_seconds())
    if silent < stale_after_seconds():
        return HEALTHY, silent, ''
    if silent < attention_after_seconds():
        return STALE, silent, 'driver_silent'
    return OPERATOR_ATTENTION, silent, 'driver_silent_long'


def classify_with_redis_view(trip, redis_says_active, now=None):
    """Classification plus the one case a machine may fix by itself.

    RECOVERY_REQUIRED means PostgreSQL and Redis disagree in the direction that
    strands supply: the trip is over as far as the database is concerned, but
    Redis still believes the driver is on it. That is a cache being wrong, not a
    business decision, so `reconcile_driver_active_trip` repairs it without
    asking anyone.
    """
    from servers.ride.models import DRIVER_ACTIVE_TRIP_STATUSES

    db_active = trip.status_id.status_code in DRIVER_ACTIVE_TRIP_STATUSES
    if not db_active and redis_says_active:
        return RECOVERY_REQUIRED, None, 'redis_active_trip_but_db_terminal'
    return classify(trip, now=now)


# ---------------------------------------------------------------------------
# Reconciliation -- PostgreSQL wins, always
# ---------------------------------------------------------------------------

def reconcile_driver_active_trip(driver_id):
    """Make Redis agree with the database about this driver's active trip.

    PostgreSQL is the truth. Redis is an optimisation that can be rebuilt, and
    when the two disagree the cache is what changes.

    Two directions:

      repaired_cleared  -- the database has no active trip for this driver and
                           Redis said there was one. This is the trip-42 case,
                           and it is why a reconnecting driver could never become
                           available again.
      repaired_set      -- the database has an active trip and Redis lost the key
                           (eviction, flush, a fresh Redis). Reconstructed so the
                           driver is not offered a second ride.

    Returns one of: 'ok', 'repaired_cleared', 'repaired_set', 'unavailable'.
    Never raises: this runs on a driver's socket connect, and a Redis problem
    must not stop a driver coming online.
    """
    from servers.ride.models import driver_active_trip_ids
    from servers import redis_client as rc

    try:
        from servers.redis_client import (
            DriverTripStateUnavailable,
            clear_driver_active_trip,
            get_driver_active_trip,
            set_driver_active_trip,
        )
    except ImportError:  # pragma: no cover - defensive
        return 'unavailable'

    from servers.driver.models import Driver
    try:
        driver = Driver.objects.get(id=driver_id)
    except Driver.DoesNotExist:
        return 'unavailable'

    db_trip_ids = driver_active_trip_ids(driver)
    db_trip_id = db_trip_ids[0] if db_trip_ids else None

    try:
        redis_trip_id = get_driver_active_trip(driver_id)
    except DriverTripStateUnavailable:
        # Cannot read, so cannot safely repair. The conservative default for
        # dispatch already treats an unreadable driver as busy.
        logger.warning('liveness: cannot reconcile driver %s, Redis unreadable',
                       driver_id)
        return 'unavailable'
    except Exception as exc:  # noqa: BLE001
        logger.warning('liveness: reconcile read failed for driver %s: %s',
                       driver_id, exc)
        return 'unavailable'

    if db_trip_id is None and redis_trip_id is not None:
        clear_driver_active_trip(driver_id)
        logger.warning(
            'driver_active_trip_repaired_cleared driver=%s stale_trip=%s '
            'reason=db_has_no_active_trip',
            driver_id, redis_trip_id,
        )
        return 'repaired_cleared'

    if db_trip_id is not None and redis_trip_id is None:
        set_driver_active_trip(driver_id, db_trip_id)
        logger.warning(
            'driver_active_trip_repaired_set driver=%s trip=%s '
            'reason=redis_lost_the_key',
            driver_id, db_trip_id,
        )
        return 'repaired_set'

    if (db_trip_id is not None and redis_trip_id is not None
            and str(db_trip_id) != str(redis_trip_id)):
        # Redis points at the wrong trip. The database decides.
        set_driver_active_trip(driver_id, db_trip_id)
        logger.warning(
            'driver_active_trip_repaired_set driver=%s trip=%s stale_trip=%s '
            'reason=redis_pointed_at_a_different_trip',
            driver_id, db_trip_id, redis_trip_id,
        )
        return 'repaired_set'

    assert rc is not None  # keep the import meaningful for linters
    return 'ok'


# ---------------------------------------------------------------------------
# Detection -- flag, never act
# ---------------------------------------------------------------------------

def stale_candidates(now=None, limit=200):
    """Active trips that have gone quiet for longer than the stale threshold.

    Ordered oldest-silence-first so an operator queue shows the worst case at the
    top, and bounded so one sweep cannot become an unbounded unit of work.
    """
    from servers.ride.models import DRIVER_ACTIVE_TRIP_STATUSES, Trip

    now = now or timezone.now()
    cutoff = now - timezone.timedelta(seconds=stale_after_seconds())

    from django.db.models import Q
    return list(
        Trip.objects
        .filter(status_id__status_code__in=DRIVER_ACTIVE_TRIP_STATUSES)
        .filter(
            Q(last_driver_activity_at__lt=cutoff)
            | (Q(last_driver_activity_at__isnull=True) & Q(started_at__lt=cutoff))
            | (Q(last_driver_activity_at__isnull=True) & Q(started_at__isnull=True)
               & Q(accepted_at__lt=cutoff))
        )
        .select_related('status_id', 'driver_id', 'driver_id__user_id')
        .order_by('last_driver_activity_at', 'accepted_at')[:limit]
    )


def flag_stale_trips(now=None, limit=200):
    """Flag quiet active trips for operator attention. Changes no trip status.

    Idempotent: a trip already flagged is left alone rather than re-flagged, so
    repeated runs -- including a Celery redelivery after a worker death -- produce
    the same end state and do not re-alert.

    Returns a summary dict for the task's log line.
    """
    now = now or timezone.now()
    flagged, already, reasons = 0, 0, {}

    for trip in stale_candidates(now=now, limit=limit):
        classification, silent, reason = classify(trip, now=now)
        if classification == HEALTHY:
            continue
        if trip.stale_flagged_at is not None:
            already += 1
            continue
        with transaction.atomic():
            # Re-read under a lock so two workers cannot both flag, and so a
            # completion committing between the query and here is respected.
            from servers.ride.models import DRIVER_ACTIVE_TRIP_STATUSES, Trip
            locked = (Trip.objects.select_for_update()
                      .select_related('status_id').filter(id=trip.id).first())
            if locked is None:
                continue
            if locked.status_id.status_code not in DRIVER_ACTIVE_TRIP_STATUSES:
                continue            # it finished while we were looking
            if locked.stale_flagged_at is not None:
                already += 1
                continue
            locked.stale_flagged_at = now
            locked.stale_reason = reason[:64]
            locked.save(update_fields=['stale_flagged_at', 'stale_reason'])
        flagged += 1
        reasons[reason] = reasons.get(reason, 0) + 1
        # No coordinates, no rider name, no phone. An operator opens the trip.
        logger.warning(
            'stale_active_trip_flagged trip=%s status=%s classification=%s '
            'silent_seconds=%s reason=%s',
            trip.id, trip.status_id.status_code, classification, silent, reason,
        )

    return {'flagged': flagged, 'already_flagged': already, 'reasons': reasons}
