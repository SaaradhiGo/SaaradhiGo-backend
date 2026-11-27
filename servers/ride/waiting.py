"""Waiting at pickup (trip charges step 2, 27 Nov): the one rider-caused change to the locked total.

The waiting terms are locked with the fare (`FarePricing.waiting_terms`, only when `trip_extras` was on
at booking). At completion, whole minutes from the driver's arrival to the start past the free minutes
are charged at the locked rate per minute. It is the driver's earnings for that time: it goes into
`final_fare_minor`, so the driver fee or commission applies and it is taxed as the fare. Tolls, airport
fees and permits never change after booking.
"""
import logging

logger = logging.getLogger(__name__)


def apply_waiting(trip):
    """Charge the waiting on `trip` (not saved: the caller saves the trip). Never raises."""
    try:
        from servers.pricing.trip_charges import waiting_charge
        from servers.ride.models import FarePricing

        if trip.waiting_minor is not None:
            return 0                                     # charged once
        terms = (FarePricing.objects.filter(trip_id=trip).order_by('-id')
                 .values_list('waiting_terms', flat=True).first())
        if not terms:
            return 0
        minutes, minor = waiting_charge(terms, trip.reached_at, trip.started_at, trip.currency or 'INR')
        trip.waiting_minutes, trip.waiting_minor = minutes, minor
        if minor:
            base = trip.final_fare_minor if trip.final_fare_minor is not None else (trip.estimated_fare_minor or 0)
            trip.final_fare_minor = int(base) + int(minor)
        return minor
    except Exception:  # noqa: BLE001 - completing a trip never fails on waiting
        logger.exception('waiting charge failed for trip %s', getattr(trip, 'pk', None))
        return 0
