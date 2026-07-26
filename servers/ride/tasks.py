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


@shared_task(bind=True, max_retries=0)
def auto_cancel_trip(self, trip_id):
    """
    Auto-cancel a trip if no driver has accepted within the timeout period.
    Scheduled via: auto_cancel_trip.apply_async(args=[trip_id], countdown=settings.TRIP_ACCEPT_TIMEOUT_SECONDS)
    """
    from servers.ride.models import Trip, TripStatus
    from servers.rider.models import Notification
    from django.utils import timezone

    try:
        trip = Trip.objects.select_related('status_id', 'user_id').get(id=trip_id)

        # Only cancel if still has no driver (not accepted yet)
        if trip.driver_id is not None:
            logger.info(f"Trip {trip_id} already accepted by driver, skipping auto-cancel")
            return f"Trip {trip_id} already accepted"

        # Only cancel if status is not already completed/cancelled
        if trip.status_id and trip.status_id.status_code in ('completed', 'cancelled'):
            logger.info(f"Trip {trip_id} already {trip.status_id.status_code}, skipping")
            return f"Trip {trip_id} already {trip.status_id.status_code}"

        # Cancel the trip
        cancelled_status, _ = TripStatus.objects.get_or_create(
            status_code='cancelled',
            defaults={'description': 'Trip cancelled'}
        )
        trip.status_id = cancelled_status
        trip.cancelled_at = timezone.now()
        trip.cancelled_by = 'system'
        trip.cancellation_reason = 'no_driver_accepted'
        trip.save()

        # Drop the offer set so the losing drivers' cards are dismissed and
        # the key does not linger in Redis.
        try:
            from servers.redis_client import clear_offered_drivers, invalidate_trip
            clear_offered_drivers(trip.id)
            invalidate_trip(trip.id)
        except Exception as exc:  # noqa: BLE001
            logger.warning('auto-cancel redis cleanup failed for %s: %s', trip_id, exc)

        # Notify rider
        Notification.objects.create(
            user_id=trip.user_id,
            title='Ride Cancelled',
            message='Your ride request was automatically cancelled because no driver accepted in time. Please try again.',
        )

        logger.info(f"Trip {trip_id} auto-cancelled due to timeout")
        return f"Trip {trip_id} auto-cancelled"

    except Trip.DoesNotExist:
        logger.warning(f"Trip {trip_id} not found for auto-cancel")
        return f"Trip {trip_id} not found"
    except Exception as e:
        logger.error(f"Error auto-cancelling trip {trip_id}: {str(e)}")
        raise
