import logging
from celery import shared_task

logger = logging.getLogger(__name__)


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
        trip.save()

        # Notify rider
        Notification.objects.create(
            user_id=trip.user_id,
            title='Ride Cancelled',
            message=f'Your ride request was automatically cancelled because no driver accepted within the timeout period. Please try again.',
        )

        logger.info(f"Trip {trip_id} auto-cancelled due to timeout")
        return f"Trip {trip_id} auto-cancelled"

    except Trip.DoesNotExist:
        logger.warning(f"Trip {trip_id} not found for auto-cancel")
        return f"Trip {trip_id} not found"
    except Exception as e:
        logger.error(f"Error auto-cancelling trip {trip_id}: {str(e)}")
        raise
