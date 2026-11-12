"""Masked calls between a trip's rider and driver (2026-11-09).

    POST /calls/trips/<trip_id>/start/     the rider or the assigned driver; the role is theirs
    POST /calls/sessions/<id>/end/         either participant; records the duration
    GET  /calls/trips/<trip_id>/           the trip's call events (who, when, how long)

Neither side ever receives the other's number. A response carries the bridge (a temporary
number to dial, or a session the provider rings), its expiry and ids; nothing else.
"""

from datetime import timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.utils import error_response, success_response

from .models import CallSession
from .providers import CallingUnavailable, get_provider

ACTIVE = ('accepted', 'reached', 'in_progress')
ENDED = ('completed', 'cancelled')
#: A call may still be placed this long after the trip ends (a lost phone, a wrong drop).
AFTER = timedelta(minutes=15)


def _err(code, message, http):
    return error_response(code=code, message=message, field='trip_id', issue=message, status=http)


def _trip_and_role(trip_id, user):
    from servers.ride.models import Trip

    trip = (Trip.objects.select_related('status_id', 'user_id', 'driver_id__user_id')
            .filter(pk=trip_id).first())
    if trip is None:
        return None, None
    if trip.user_id_id == user.id:
        return trip, 'rider'
    if trip.driver_id is not None and trip.driver_id.user_id_id == user.id:
        return trip, 'driver'
    return None, None


def call_window_end(trip, now):
    """When calls stop connecting, or None if the trip may not be called now."""
    code = getattr(trip.status_id, 'status_code', None)
    if code in ACTIVE and trip.driver_id_id:
        return now + AFTER
    if code in ENDED and trip.driver_id_id:
        ended = trip.completed_at or trip.cancelled_at
        if ended and now < ended + AFTER:
            return ended + AFTER
    return None


def session_json(s, bridge=None):
    out = {'session_id': s.id, 'trip_id': s.trip_id, 'from_role': s.from_role,
           'started_at': s.started_at.isoformat(), 'expires_at': s.expires_at.isoformat(),
           'ended_at': s.ended_at.isoformat() if s.ended_at else None,
           'duration_seconds': s.duration_seconds}
    if bridge is not None:
        out.update({'kind': bridge.kind, 'number': bridge.number or None,
                    'provider': s.provider})
    return out


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def start_call(request, trip_id):
    trip, role = _trip_and_role(trip_id, request.user)
    if trip is None:
        return _err('NOT_FOUND', 'Trip not found.', status.HTTP_404_NOT_FOUND)
    now = timezone.now()
    until = call_window_end(trip, now)
    if until is None:
        return _err('CALL_NOT_AVAILABLE', 'Calling is available during the trip and for 15 minutes after.',
                    status.HTTP_409_CONFLICT)
    try:
        provider = get_provider(getattr(trip.zone, 'country', None))
    except CallingUnavailable:
        return _err('CALLING_UNAVAILABLE', 'Calling is not available right now.',
                    status.HTTP_503_SERVICE_UNAVAILABLE)
    session = CallSession.objects.create(trip=trip, from_role=role, provider=provider.name,
                                         expires_at=until)
    caller, callee = ((trip.user_id, trip.driver_id.user_id) if role == 'rider'
                      else (trip.driver_id.user_id, trip.user_id))
    bridge = provider.start(session, caller.phone_number, callee.phone_number)
    session.provider_ref = bridge.ref[:100]
    session.save(update_fields=['provider_ref'])
    return success_response({'call': session_json(session, bridge)}, status.HTTP_201_CREATED)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def end_call(request, session_id):
    session = CallSession.objects.filter(pk=session_id).first()
    trip, _role = _trip_and_role(session.trip_id, request.user) if session else (None, None)
    if trip is None:
        return _err('NOT_FOUND', 'Call not found.', status.HTTP_404_NOT_FOUND)
    if session.ended_at is None:
        session.ended_at = timezone.now()
        session.duration_seconds = max(0, int((session.ended_at - session.started_at).total_seconds()))
        session.save(update_fields=['ended_at', 'duration_seconds'])
        try:
            get_provider(getattr(trip.zone, 'country', None)).end(session)
        except CallingUnavailable:
            pass
    return success_response({'call': session_json(session)}, status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def trip_calls(request, trip_id):
    trip, _role = _trip_and_role(trip_id, request.user)
    if trip is None:
        return _err('NOT_FOUND', 'Trip not found.', status.HTTP_404_NOT_FOUND)
    return success_response({'calls': [session_json(s) for s in trip.calls.all()]},
                            status.HTTP_200_OK)
