"""Zero tolerance for drugs and alcohol (14 Nov; Idaho Code 49-3709).

A rider reports that their driver seemed impaired. Where the market requires it
(`MarketRegulation.zero_tolerance_suspend`), the driver is suspended at once and stays suspended
for the investigation: an urgent safety ticket is the console's review item, and reinstating is
the existing console action. The report and the ticket are kept for the market's complaint
retention period (49-3709(3): two years in Idaho).

    POST /ride/trips/<trip_id>/report-impairment/   {"description": "..."}   (the trip's rider)
"""
from django.db import transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.utils import error_response as _error_response, success_response


def error_response(*, code, message, status, field=''):
    return _error_response(code=code, message=message, field=field, issue=message, status=status)

#: What marks a ticket as a zero-tolerance report (one per trip).
MARK = '[Zero tolerance]'


def report(user, trip, description=''):
    """`(ticket, suspended)`. Idempotent per trip: a second report returns the first ticket."""
    from servers.driver.admin_drivers import operator_suspend
    from servers.pricing.regulation import rules_for_zone
    from servers.support.models import SupportTicket

    existing = SupportTicket.objects.filter(trip_id=trip, user_id=user, description__startswith=MARK).first()
    if existing is not None:
        return existing, False
    text = (description or '').strip()[:2000]
    with transaction.atomic():
        ticket = SupportTicket.objects.create(
            user_id=user, trip_id=trip, issue_type='safety', priority='urgent',
            description=f'{MARK} The rider reports the driver seemed impaired by drugs or alcohol. {text}'.strip())
        suspended = False
        driver = trip.driver_id
        if driver is not None and rules_for_zone(getattr(trip, 'zone', None))['zero_tolerance_suspend']:
            outcome, _ = operator_suspend(
                driver.id, reason_code='zero_tolerance',
                note=f'Rider report on trip {trip.id} (ticket {ticket.id}): suspended for the investigation.')
            suspended = outcome in ('suspended', 'already_suspended')
    return ticket, suspended


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def report_impairment(request, trip_id):
    from servers.ride.models import Trip
    from servers.ride.views import trip_not_found

    trip = Trip.objects.select_related('driver_id', 'zone').filter(id=trip_id, user_id=request.user).first()
    if trip is None:
        return trip_not_found(trip_id)
    if trip.driver_id is None:
        return error_response(code='NO_DRIVER', message='This trip had no driver to report.',
                              status=status.HTTP_409_CONFLICT)
    ticket, suspended = report(request.user, trip, request.data.get('description', ''))
    return success_response({'ticket_id': ticket.id, 'driver_paused': suspended,
                             'message': 'Thank you. The driver is paused while we investigate. '
                                        'If you are in danger, call 911.'
                             if suspended else 'Thank you. Our safety team will review this now.'},
                            status.HTTP_201_CREATED)
