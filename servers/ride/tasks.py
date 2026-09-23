import logging
from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(
    name='ride.issue_receipt_for_trip',
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
)
def issue_receipt_for_trip(self, trip_id):
    """Render + store + email the rider's receipt for a completed trip.

    Runs out-of-band because receipt issuance renders a PDF (reportlab),
    uploads it to S3 and sends an email. Doing that inline in the trip
    completion transaction held a row lock across three external systems
    and made trip-completion latency depend on SES.

    Idempotent: `issue_receipt` returns the existing Receipt if one is
    already on file for the trip.
    """
    from servers.ride.models import Trip
    from servers.ride.receipts import issue_receipt

    try:
        trip = Trip.objects.select_related('user_id', 'status_id').get(id=trip_id)
    except Trip.DoesNotExist:
        logger.warning('issue_receipt_for_trip: trip %s not found', trip_id)
        return f'trip {trip_id} not found'

    try:
        receipt = issue_receipt(trip)
    except Exception as exc:  # noqa: BLE001
        logger.exception('issue_receipt failed for trip %s: %s', trip_id, exc)
        raise self.retry(exc=exc)

    return f'receipt {getattr(receipt, "receipt_number", None)} for trip {trip_id}'


@shared_task(
    name='ride.dispatch_wave',
    bind=True,
    max_retries=3,
    default_retry_delay=5,
    acks_late=True,
)
def dispatch_wave(self, trip_id, wave_index, dispatch_epoch, radii):
    """Execute one durable dispatch wave.

    Celery is the only owner of wave execution after the durable-dispatch
    change; the rider WebSocket may start a search but never runs the loop,
    so a rider disconnect, a Daphne restart or a deploy no longer aborts the
    driver search.

    `acks_late=True` is set explicitly because the project default is
    `task_acks_late = False`: we would rather re-run a wave after a worker
    crash than lose it. Re-running is safe — `execute_wave` re-reads the
    authoritative `Trip` row first and performs no PostgreSQL writes, so a
    duplicate or stale delivery either no-ops or re-sends an offer that
    acceptance is independently protected against.

    Retries cover transient infrastructure faults only. A trip that is no
    longer dispatchable is a successful no-op, not an error.
    """
    from servers.ride.dispatch import execute_wave

    try:
        return execute_wave(trip_id, wave_index, dispatch_epoch, radii)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            'dispatch_wave failed trip=%s wave=%s epoch=%s: %s',
            trip_id, wave_index, dispatch_epoch, exc,
        )
        logger.info(
            'dispatch_wave_retry',
            extra={
                'event': 'dispatch_wave_retry',
                'trip_id': trip_id,
                'epoch': dispatch_epoch,
                'wave_index': wave_index,
                'attempt': self.request.retries,
                'error': type(exc).__name__,
            },
        )
        raise self.retry(exc=exc)


@shared_task(bind=True, max_retries=0)
def auto_cancel_trip(self, trip_id):
    """Cancel a trip that no driver accepted inside the accept timeout.

    Scheduled once, when the trip is created:
      auto_cancel_trip.apply_async((trip_id,), countdown=TRIP_ACCEPT_TIMEOUT_SECONDS)

    Deliberately never rescheduled — not by a dispatch wave and not by a
    rider retry. That single deadline is what durably bounds the whole
    search, which is why an abandoned dispatch generation cannot extend a
    trip's lifetime.

    The timeout and a driver's acceptance can land in the same instant at
    ~90s. This used to read the trip without a lock and then call a bare
    `trip.save()`, which writes every column from the in-memory instance: an
    acceptance committing between the read and the save was silently
    reverted, cancelling a trip a driver was already driving to and clearing
    `driver_id` back to NULL. The row is now locked and re-checked inside
    the transaction, and only the cancellation columns are written.
    """
    from django.db import transaction
    from django.utils import timezone

    from servers.ride.models import Trip, TripStatus
    from servers.rider.models import Notification

    try:
        with transaction.atomic():
            try:
                trip = (
                    Trip.objects
                    .select_for_update(of=('self',))
                    .select_related('status_id', 'user_id')
                    .get(id=trip_id)
                )
            except Trip.DoesNotExist:
                logger.warning('Trip %s not found for auto-cancel', trip_id)
                return f'Trip {trip_id} not found'

            # Re-checked INSIDE the lock. An acceptance that commits first
            # is now visible here, so the timeout stands down.
            if trip.driver_id_id is not None:
                logger.info(
                    'auto_cancel_trip: trip %s already accepted, standing down', trip_id,
                )
                return f'Trip {trip_id} already accepted'

            current = trip.status_id.status_code if trip.status_id else None
            if current in ('completed', 'cancelled', 'accepted', 'reached', 'in_progress'):
                logger.info(
                    'auto_cancel_trip: trip %s already %s, standing down', trip_id, current,
                )
                return f'Trip {trip_id} already {current}'

            cancelled_status, _ = TripStatus.objects.get_or_create(
                status_code='cancelled',
                defaults={'description': 'Trip cancelled'},
            )
            trip.status_id = cancelled_status
            trip.cancelled_at = timezone.now()
            trip.cancelled_by = 'system'
            trip.cancellation_reason = 'no_driver_accepted'
            # Narrow write: never restate driver_id or any other column from
            # a possibly-stale in-memory instance.
            trip.save(update_fields=[
                'status_id', 'cancelled_at', 'cancelled_by', 'cancellation_reason',
            ])

            rider = trip.user_id
            trip_pk = trip.id

        # After commit: dismiss candidate cards and drop the cached trip.
        # Outside the transaction so no external call is made under a lock.
        try:
            from servers.redis_client import invalidate_trip
            from servers.ride.dispatch import dismiss_outstanding_offers
            dismiss_outstanding_offers(trip_pk, reason='timeout')
            invalidate_trip(trip_pk)
        except Exception as exc:  # noqa: BLE001
            logger.warning('auto-cancel cleanup failed for %s: %s', trip_id, exc)

        try:
            Notification.objects.create(
                user_id=rider,
                title='Ride Cancelled',
                message=(
                    'Your ride request was automatically cancelled because no '
                    'driver accepted in time. Please try again.'
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning('auto-cancel notification failed for %s: %s', trip_id, exc)

        logger.info(
            'dispatch_terminal',
            extra={
                'event': 'dispatch_terminal',
                'trip_id': trip_pk,
                'outcome': 'timeout_no_driver_accepted',
            },
        )
        return f'Trip {trip_id} auto-cancelled'

    except Exception as e:
        logger.error('Error auto-cancelling trip %s: %s', trip_id, e)
        raise
