"""Durable dispatch: driver search orchestration owned by Celery, not a socket.

Dispatch waves used to run as an `asyncio.create_task` inside
`RideRequestConsumer`, and `disconnect()` cancelled that task. A rider whose
phone dropped its connection during the search — routine on Indian mobile
networks — lost waves 2 and 3 entirely, and `auto_cancel_trip` then closed
the trip as `no_driver_accepted` even though most drivers were never asked.
Dispatch also died on any Daphne restart or deploy, and could not scale
beyond the single process holding the socket.

This module moves wave orchestration into Celery. The WebSocket may *start*
a search and *observe* its progress; it never executes the wave loop, and
its lifetime is irrelevant to the outcome.

Division of responsibility
--------------------------
PostgreSQL is the only authority on whether a trip may still be dispatched.
Redis is an optimisation: the GEO index that answers "who is nearby", and a
best-effort record of who has already been offered this trip.

Two different kinds of idempotency live here, and conflating them is how this
sort of code goes wrong:

* **Business idempotency — mandatory, PostgreSQL-backed.** Running a wave
  twice, or running a wave left over from an abandoned search, must not
  assign a driver twice, corrupt trip state, extend the search, reopen a
  cancelled trip or touch money. This holds because `_load_dispatch_state`
  re-reads the `Trip` row on every single execution and a wave performs
  **no writes to PostgreSQL at all** — it only reads, then sends offers. A
  stale task is therefore harmless by construction, which is why nothing
  here depends on revoking Celery tasks.

* **Notification de-duplication — best effort, Redis-backed.** Not offering
  the same driver the same ride twice within one search. If Redis is flushed
  a driver may see a duplicate card; that is cosmetic. Their acceptance is
  still gated by the row-locked checks in `_accept_trip`.

The dispatch generation ("epoch")
---------------------------------
`dispatch_epoch` is a task argument and is deliberately NOT persisted. It
exists only to scope the notification de-duplication set so that a
rider-triggered retry re-reaches drivers who ignored the first search —
which is exactly what the previous implementation did, since each call
started with an empty in-process `offered` set.

It is safe for it to be non-durable because **no lifecycle decision ever
reads it**. Eligibility is computed purely from `Trip.driver_id` and
`Trip.status_id`. Concretely, a wave from an abandoned generation can only:

  * find the trip ineligible and no-op, or
  * send an extra offer for a trip that genuinely is still searching.

It cannot extend the search either, because the durable deadline is
`auto_cancel_trip`, scheduled exactly once when the trip is created and
never rescheduled by a wave or a retry. See `docs/adr/0008` for the full
argument. Adding a `dispatch_generation` column would let PostgreSQL
distinguish generations, but nothing would consult it, so it would be
schema with no reader.
"""

import logging
import uuid

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings

logger = logging.getLogger(__name__)

# A trip may be dispatched only from these statuses. `None` covers a row
# whose status FK was never populated.
DISPATCHABLE_STATUSES = frozenset({None, 'requested'})

# Hard ceiling on any single wave radius, mirroring the previous consumer
# behaviour (`min(int(radius), 5000)`).
MAX_WAVE_RADIUS_M = 5000


def new_dispatch_epoch():
    """Opaque identifier for one search generation. Not persisted."""
    return uuid.uuid4().hex


def wave_plan(radius=None):
    """Radii for one generation, outermost last.

    A rider-triggered retry passes an explicit radius and gets a single
    wave, which is what `_handle_retry` did before (`waves = [radius]`).
    Otherwise the configured expanding plan applies.
    """
    if radius:
        return [min(int(radius), MAX_WAVE_RADIUS_M)]
    configured = getattr(settings, 'DISPATCH_RADIUS_WAVES_M', (1500, 3000, 5000))
    plan = [int(r) for r in configured if int(r) > 0]
    return plan or [MAX_WAVE_RADIUS_M]


def wave_gap_seconds():
    return float(getattr(settings, 'DISPATCH_WAVE_SECONDS', 20))


def _log(event, **fields):
    """Structured dispatch log line.

    The JSON formatter in `base.logging_filters` promotes `extra` keys to
    top-level fields, so these are queryable in CloudWatch. Deliberately
    carries ids and counts only — never coordinates, phone numbers or OTPs.
    """
    logger.info(event, extra={'event': event, **fields})


# ---------------------------------------------------------------------------
# Authoritative state
# ---------------------------------------------------------------------------

class DispatchState:
    """Snapshot of the only facts that decide whether a wave may proceed."""

    __slots__ = ('trip', 'status', 'dispatchable', 'missing')

    def __init__(self, trip=None, status=None, dispatchable=False, missing=False):
        self.trip = trip
        self.status = status
        self.dispatchable = dispatchable
        self.missing = missing

    @property
    def reason(self):
        if self.missing:
            return 'trip_missing'
        if self.trip is not None and self.trip.driver_id_id is not None:
            return 'already_accepted'
        if self.status not in DISPATCHABLE_STATUSES:
            return f'status_{self.status}'
        return 'dispatchable'


def load_dispatch_state(trip_id):
    """Read the authoritative dispatch state for a trip from PostgreSQL.

    This is the single gate every wave passes through. It takes no lock: a
    wave only ever reads, so a stale read can at worst send one extra offer,
    and acceptance itself is protected by `SELECT FOR UPDATE` in
    `_accept_trip`.
    """
    from servers.ride.models import Trip

    try:
        trip = (
            Trip.objects
            .select_related('status_id', 'requested_vehicle_type')
            .get(id=trip_id)
        )
    except Trip.DoesNotExist:
        return DispatchState(missing=True)

    status = trip.status_id.status_code if trip.status_id else None
    dispatchable = trip.driver_id_id is None and status in DISPATCHABLE_STATUSES
    return DispatchState(trip=trip, status=status, dispatchable=dispatchable)


# ---------------------------------------------------------------------------
# Offer dismissal — one implementation, shared by every terminal transition
# ---------------------------------------------------------------------------

def dismiss_outstanding_offers(trip_id, *, reason, exclude_driver_id=None):
    """Tell candidate drivers an outstanding offer is dead, and forget it.

    Previously only `_accept_trip` and `auto_cancel_trip` did this, so a
    rider who cancelled over REST left every candidate holding a live
    request card and left the offer set behind in Redis.

    Safe to call twice: `pop_offered_drivers` deletes the set, so a second
    call finds nothing and sends nothing. Safe when Redis is unavailable or
    the key has expired: the helper returns an empty list and this becomes a
    no-op.

    This is UI hygiene only. It never touches the durable trip lifecycle —
    a driver who acts on a stale card is still stopped by the row-locked
    checks in `_accept_trip`.

    Returns the driver ids that were notified.
    """
    from servers.redis_client import pop_offered_drivers

    try:
        offered = pop_offered_drivers(trip_id) or []
    except Exception as exc:  # noqa: BLE001
        _log('dispatch_offer_cleanup', trip_id=trip_id, reason=reason,
             outcome='redis_unavailable', error=type(exc).__name__)
        return []

    excluded = str(exclude_driver_id) if exclude_driver_id is not None else None
    losers = [str(d) for d in offered if str(d) != excluded]

    if not losers:
        _log('dispatch_offer_cleanup', trip_id=trip_id, reason=reason, notified=0)
        return []

    channel_layer = get_channel_layer()
    notified = []
    if channel_layer is not None:
        group_send = async_to_sync(channel_layer.group_send)
        for driver_id in losers:
            try:
                # Same event contract the winning-driver path already uses,
                # handled by DriverLocationConsumer.trip_taken.
                group_send(f'driver_{driver_id}', {
                    'type': 'trip_taken',
                    'trip_id': trip_id,
                })
                notified.append(driver_id)
            except Exception as exc:  # noqa: BLE001
                # A dead socket must not break the caller's cancellation.
                logger.warning(
                    'dispatch_offer_cleanup: notify failed for driver %s on trip %s: %s',
                    driver_id, trip_id, exc,
                )

    _log('dispatch_offer_cleanup', trip_id=trip_id, reason=reason,
         candidates=len(losers), notified=len(notified))
    return notified


# ---------------------------------------------------------------------------
# Starting a search
# ---------------------------------------------------------------------------

def start_dispatch(trip_id, *, radius=None, reason='initial', epoch=None):
    """Begin a dispatch generation by enqueuing its first wave.

    Returns ``{'enqueued': bool, 'epoch': str, 'radii': [...], 'reason': str}``.

    Enqueue failure is reported, never worked around: there is exactly one
    dispatch engine after this change, so we do not fall back to running
    waves in the caller. The trip stays committed and its durable
    `auto_cancel_trip` deadline still applies, so a failed enqueue degrades
    to "no driver found" rather than a corrupt lifecycle.
    """
    from servers.ride.tasks import dispatch_wave

    epoch = epoch or new_dispatch_epoch()
    radii = wave_plan(radius)

    _log('dispatch_started', trip_id=trip_id, epoch=epoch,
         radii=radii, reason=reason)

    try:
        dispatch_wave.apply_async((trip_id, 0, epoch, radii))
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            'dispatch_enqueue_failed for trip %s epoch %s: %s', trip_id, epoch, exc,
        )
        _log('dispatch_enqueue_failed', trip_id=trip_id, epoch=epoch,
             wave_index=0, error=type(exc).__name__)
        return {'enqueued': False, 'epoch': epoch, 'radii': radii, 'reason': reason}

    return {'enqueued': True, 'epoch': epoch, 'radii': radii, 'reason': reason}


# ---------------------------------------------------------------------------
# Wave execution
# ---------------------------------------------------------------------------

def _candidate_driver_ids(trip, radius, epoch):
    """Nearby driver ids for this radius, minus those this generation already
    offered. Vehicle-type geo isolation is preserved by passing the trip's
    requested type straight through to `nearby_drivers`.
    """
    from servers.redis_client import get_generation_offers, nearby_drivers

    vehicle_type = None
    if trip.requested_vehicle_type is not None:
        vehicle_type = trip.requested_vehicle_type.type

    found = nearby_drivers(
        lng=float(trip.pickup_long),
        lat=float(trip.pickup_lat),
        vehicle_type=vehicle_type,
        radius=radius,
    ) or []

    already = get_generation_offers(trip.id, epoch)

    ids = []
    for entry in found:
        key = entry[0] if isinstance(entry, (list, tuple)) else entry
        if not isinstance(key, str) or not key.startswith('driver:'):
            continue
        driver_id = key.split(':')[1]
        if driver_id in already or driver_id in ids:
            continue
        ids.append(driver_id)
    return ids


def _offer_payload(trip, rider_name, vehicle_type):
    """The `ride_request` event body. Identical shape to the previous
    consumer implementation so the driver app needs no change."""
    return {
        'type': 'ride_request',
        'trip_id': trip.id,
        'rider_name': rider_name,
        'pickup_lat': str(trip.pickup_lat),
        'pickup_lng': str(trip.pickup_long),
        'destination_lat': str(trip.destination_lat),
        'destination_lng': str(trip.destination_long),
        'pickup_address': trip.pickup_address or '',
        'destination_address': trip.destination_address or '',
        'estimated_fare': str(trip.estimated_fare) if trip.estimated_fare else '',
        'distance_km': str(trip.estimated_distance_km) if trip.estimated_distance_km is not None else '',
        'duration_min': str(trip.estimated_duration_min) if trip.estimated_duration_min is not None else '',
        'payment_method': str(trip.payment_method) if trip.payment_method else '',
        'vehicle_type': str(vehicle_type) if vehicle_type else '',
    }


def _enqueue_offer_pushes(driver_ids, trip_id, rider_name):
    """Queue one FCM task per candidate, with a single bulk driver query.

    The previous loop issued `Driver.objects.get()` per driver per wave.
    FCM itself was already asynchronous (`send_push_notification` enqueues
    `auth_user.send_push_notification_task`), so only the N+1 needed fixing.

    Notification failure is swallowed: a push is not allowed to fail the
    dispatch state machine.
    """
    from servers.auth_user.services import send_push_notification_task
    from servers.driver.models import Driver

    if not driver_ids:
        return 0

    queued = 0
    try:
        rows = (
            Driver.objects
            .filter(id__in=driver_ids)
            .select_related('user_id')
            .only('id', 'user_id__id', 'user_id__fcm_token')
        )
        targets = [
            r.user_id_id for r in rows
            if getattr(r.user_id, 'fcm_token', None)
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning('dispatch push bulk-load failed for trip %s: %s', trip_id, exc)
        return 0

    for user_id in targets:
        try:
            send_push_notification_task.delay(
                user_id,
                'New Ride Request',
                f'New ride request from {rider_name}',
                {'trip_id': str(trip_id), 'type': 'ride_request'},
            )
            queued += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                'dispatch push enqueue failed for user %s on trip %s: %s',
                user_id, trip_id, exc,
            )
    return queued


def execute_wave(trip_id, wave_index, epoch, radii):
    """Run one dispatch wave. Idempotent and side-effect-free in PostgreSQL.

    Order matters: the eligibility gate runs first, so a stale or duplicate
    delivery for an accepted, cancelled, timed-out or deleted trip exits
    before touching Redis or the channel layer.
    """
    from servers.redis_client import add_generation_offers, add_offered_drivers

    radii = list(radii or [])
    radius = radii[wave_index] if 0 <= wave_index < len(radii) else None
    if radius is None:
        _log('dispatch_wave_skipped', trip_id=trip_id, epoch=epoch,
             wave_index=wave_index, outcome='no_such_wave')
        return {'executed': False, 'reason': 'no_such_wave'}

    state = load_dispatch_state(trip_id)
    if not state.dispatchable:
        # This is the branch that makes stale tasks harmless. It is reached
        # by: acceptance, rider or driver cancellation, timeout, completion,
        # a deleted trip, and any duplicate delivery of an earlier wave.
        _log('dispatch_wave_skipped', trip_id=trip_id, epoch=epoch,
             wave_index=wave_index, radius=radius, outcome=state.reason)
        return {'executed': False, 'reason': state.reason}

    trip = state.trip
    _log('dispatch_wave_started', trip_id=trip_id, epoch=epoch,
         wave_index=wave_index, radius=radius)

    try:
        candidates = _candidate_driver_ids(trip, radius, epoch)
    except Exception as exc:  # noqa: BLE001
        # Redis GEO unavailable. Nothing durable is wrong; let the successor
        # wave (or the durable timeout) carry the trip forward.
        logger.warning('dispatch candidate lookup failed for trip %s: %s', trip_id, exc)
        candidates = []

    _log('dispatch_wave_candidates', trip_id=trip_id, epoch=epoch,
         wave_index=wave_index, radius=radius, candidates=len(candidates))

    delivered = 0
    pushes = 0
    if candidates:
        rider_name = _rider_name(trip)

        # Remember the offers before sending them. Both sets are best
        # effort: the per-generation set suppresses duplicates within this
        # search, the per-trip set feeds the `trip_taken` fanout.
        try:
            add_generation_offers(trip.id, epoch, candidates)
            add_offered_drivers(trip.id, candidates)
        except Exception as exc:  # noqa: BLE001
            logger.warning('dispatch offer bookkeeping failed for trip %s: %s', trip_id, exc)

        vehicle_type = (
            trip.requested_vehicle_type.type
            if trip.requested_vehicle_type is not None else None
        )
        payload = _offer_payload(trip, rider_name, vehicle_type)

        channel_layer = get_channel_layer()
        if channel_layer is not None:
            group_send = async_to_sync(channel_layer.group_send)
            for driver_id in candidates:
                try:
                    group_send(f'driver_{driver_id}', dict(payload))
                    delivered += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        'dispatch offer send failed for driver %s on trip %s: %s',
                        driver_id, trip_id, exc,
                    )

        pushes = _enqueue_offer_pushes(candidates, trip.id, rider_name)

    _log('dispatch_wave_offers', trip_id=trip_id, epoch=epoch,
         wave_index=wave_index, radius=radius,
         attempted=len(candidates), delivered=delivered, pushes_queued=pushes)

    # Tell the rider's socket how the search is going, if it happens to be
    # connected. This is advisory: nothing waits on it and dispatch does not
    # care whether anyone is listening.
    _notify_rider_progress(trip, wave_index, radius, len(candidates), radii)

    _schedule_successor(trip_id, wave_index, epoch, radii)

    return {
        'executed': True,
        'wave_index': wave_index,
        'radius': radius,
        'candidates': len(candidates),
        'delivered': delivered,
        'pushes_queued': pushes,
    }


def _schedule_successor(trip_id, wave_index, epoch, radii):
    """Queue the next wave after the configured gap.

    A worker is never held sleeping; the delay is Celery's `countdown`.

    This re-reads state as a courtesy so an accepted trip does not leave a
    pointless task queued, but correctness does not depend on that check:
    `execute_wave` re-reads PostgreSQL itself, so a successor queued a
    moment before acceptance simply no-ops when it runs.
    """
    from servers.ride.tasks import dispatch_wave

    next_index = wave_index + 1
    if next_index >= len(radii):
        _log('dispatch_terminal', trip_id=trip_id, epoch=epoch,
             wave_index=wave_index, outcome='final_wave_sent')
        return False

    if not load_dispatch_state(trip_id).dispatchable:
        _log('dispatch_terminal', trip_id=trip_id, epoch=epoch,
             wave_index=wave_index, outcome='no_longer_dispatchable')
        return False

    countdown = wave_gap_seconds()
    try:
        dispatch_wave.apply_async(
            (trip_id, next_index, epoch, radii), countdown=countdown,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            'dispatch_enqueue_failed scheduling wave %s for trip %s: %s',
            next_index, trip_id, exc,
        )
        _log('dispatch_enqueue_failed', trip_id=trip_id, epoch=epoch,
             wave_index=next_index, error=type(exc).__name__)
        return False

    _log('dispatch_wave_successor_scheduled', trip_id=trip_id, epoch=epoch,
         wave_index=next_index, radius=radii[next_index], countdown=countdown)
    return True


def _rider_name(trip):
    try:
        return trip.user_id.full_name or 'Rider'
    except Exception:  # noqa: BLE001
        return 'Rider'


def _notify_rider_progress(trip, wave_index, radius, candidate_count, radii):
    """Best-effort progress frame to the rider group.

    Replaces the `await self.send(...)` calls the consumer used to make.
    Sent to the rider's *group*, so it reaches whichever socket the rider
    currently holds — or nobody, harmlessly, if they are offline.
    """
    channel_layer = get_channel_layer()
    if channel_layer is None:
        return
    is_final = (wave_index + 1) >= len(radii)
    event = {
        'type': 'dispatch_progress',
        'trip_id': trip.id,
        'wave': wave_index + 1,
        'waves_total': len(radii),
        'radius_m': radius,
        'drivers_notified': candidate_count,
        'final_wave': is_final,
    }
    try:
        async_to_sync(channel_layer.group_send)(f'rider_{trip.user_id_id}', event)
    except Exception as exc:  # noqa: BLE001
        logger.debug('dispatch progress notify failed for trip %s: %s', trip.id, exc)
