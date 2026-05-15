"""SOS / panic API endpoints.

The rider or driver POSTs to /sos/ when something is wrong. We:
  1. Persist the event row immediately (durable record).
  2. Return 200 with the SOS id within ~1s so the client UI can show
     "Help is on the way".
  3. Dispatch the actual ops alerts (push + SMS to emergency contact +
     ops topic) asynchronously via Celery so a slow third-party can't
     stall the ack to the caller.

Acknowledge / resolve endpoints are admin-only and write an immutable
SOSEventUpdate row inside the same transaction as the parent state flip.
"""

import logging

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from django.db import transaction

from base.utils import success_response, error_response
from base.permissions import IsAdmin

from .models import SOSEvent, SOSEventUpdate

logger = logging.getLogger(__name__)


def _client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR') or None


def _user_label(user):
    if not user:
        return ''
    return (
        getattr(user, 'full_name', None)
        or getattr(user, 'phone_number', None)
        or str(user)
    )[:255]


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def raise_sos(request):
    """Raise an SOS event.

    Body:
        trip_id:    int       optional — links the SOS to a live trip
        event_type: string    one of panic/vehicle_breakdown/medical/
                              harassment/accident/other (default: panic)
        lat, lng:   decimal   optional — caller's coords
        accuracy_m: float     optional — GPS accuracy
        note:       string    optional — free-text
        initiated_by: string  rider/driver (default: rider)
    """
    from servers.ride.models import Trip

    data = request.data if hasattr(request, 'data') else {}
    event_type = (data.get('event_type') or 'panic')[:32]
    initiated_by = (data.get('initiated_by') or 'rider')[:10]
    if initiated_by not in ('rider', 'driver'):
        initiated_by = 'rider'

    trip = None
    trip_id = data.get('trip_id')
    if trip_id:
        try:
            trip = Trip.objects.get(id=trip_id)
        except Trip.DoesNotExist:
            trip = None

    try:
        with transaction.atomic():
            event = SOSEvent.objects.create(
                user=request.user,
                user_label=_user_label(request.user),
                initiated_by=initiated_by,
                trip=trip,
                event_type=event_type if event_type in dict(SOSEvent.EVENT_TYPE_CHOICES) else 'panic',
                latitude=data.get('lat') or data.get('latitude'),
                longitude=data.get('lng') or data.get('longitude'),
                location_accuracy_m=data.get('accuracy_m'),
                note=(data.get('note') or '')[:10000],
                ip_address=_client_ip(request),
                user_agent=(request.META.get('HTTP_USER_AGENT') or '')[:512],
            )
    except Exception as e:
        logger.exception(f"raise_sos: failed to persist event: {e}")
        return error_response(
            code='INTERNAL_ERROR',
            message='Could not record SOS event',
            field='general',
            issue=str(e),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    logger.warning(
        f"SOS RAISED id={event.id} by={initiated_by} user={request.user.id} "
        f"trip={trip.id if trip else None} type={event.event_type} "
        f"lat={event.latitude} lng={event.longitude}"
    )

    # Dispatch the fan-out (ops push, SMS to emergency contact, ops live
    # board) on commit so a rollback can't trigger phantom alerts. The
    # task itself runs in Celery — no inline I/O.
    def _dispatch():
        try:
            from .tasks import dispatch_sos
            dispatch_sos.delay(event.id)
        except Exception as e:
            logger.exception(f"SOS dispatch enqueue failed for {event.id}: {e}")

    transaction.on_commit(_dispatch)

    return success_response(
        {
            'sos_id': event.id,
            'status': event.status,
            'message': 'SOS recorded. Help is on the way.',
        },
        status.HTTP_201_CREATED,
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsAdmin])
def acknowledge_sos(request, sos_id):
    """Mark an SOS event acknowledged by ops."""
    return _admin_transition(request, sos_id, target='acknowledged')


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsAdmin])
def resolve_sos(request, sos_id):
    """Mark an SOS event resolved. Accepts a resolution note."""
    return _admin_transition(request, sos_id, target='resolved')


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsAdmin])
def false_alarm_sos(request, sos_id):
    """Mark an SOS event as false alarm. Accepts a note explaining why."""
    return _admin_transition(request, sos_id, target='false_alarm')


def _admin_transition(request, sos_id, target):
    note = (request.data.get('note') or '')[:10000] if hasattr(request, 'data') else ''
    try:
        with transaction.atomic():
            try:
                event = SOSEvent.objects.select_for_update().get(id=sos_id)
            except SOSEvent.DoesNotExist:
                return error_response(
                    code='NOT_FOUND',
                    message='SOS event not found',
                    field='sos_id',
                    issue=f'No SOS event with id {sos_id}',
                    status=status.HTTP_404_NOT_FOUND,
                )

            if event.status == target:
                return success_response(
                    {'sos_id': event.id, 'status': event.status},
                    status.HTTP_200_OK,
                )
            event.status = target
            event.save(update_fields=['status'])
            SOSEventUpdate.objects.create(
                event=event,
                actor=request.user,
                actor_label=_user_label(request.user),
                new_status=target,
                note=note,
            )
    except Exception as e:
        logger.exception(f"_admin_transition: failed: {e}")
        return error_response(
            code='INTERNAL_ERROR',
            message='Failed to update SOS event',
            field='general',
            issue=str(e),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    return success_response(
        {'sos_id': event.id, 'status': event.status},
        status.HTTP_200_OK,
    )
