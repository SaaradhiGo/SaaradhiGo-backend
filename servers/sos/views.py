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

WHO MAY SEE THE QUEUE, AND WHAT THEY SEE
----------------------------------------
Every admin view here is gated on `ScopedPermission('safety.respond')`,
which Safety Officer and Super Admin hold and nobody else does. The queue
is filtered to the operator's scope: an event is anchored by its trip's
zone if it has a trip, else by the raiser's home city if the raiser is a
driver (`scoping.SCOPE_PATHS['sos.SOSEvent']`). An event outside the
caller's scope answers NOT FOUND, never FORBIDDEN -- a 403 would confirm
the id exists.

WHERE AN EVENT IS ROUTED
------------------------
Anchored on creation (`anchoring.resolve_anchor`, owner's ruling of
2026-10-28): the trip's zone, else the raiser's GPS fix resolved to a city,
else a driver's home city. An event with none of those is UNANCHORED: it
is in every Safety Officer's queue whatever their scope, at Global, and a
Command Centre alert is raised for it. The scope filter is
`scoping.SCOPE_PATHS['sos.SOSEvent']` plus `scoping.UNSCOPED_UNION`.

Contact details -- the raiser's name and phone, the driver's label -- are
MASKED in every response, for every role including Super Admin. The one
way to the real values is `reveal_sos_contact`: `safety.respond`, the
event still open or acknowledged, and an `sos_contact_revealed` audit row
written with the operator's name on it. A closed incident has nobody to
reach, so its contact stays masked for good.
"""

import logging
from datetime import timedelta

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from django.db import transaction
from django.utils import timezone
from django.db.models import Q

from base.utils import success_response, error_response
from servers.admin_access.bridge import ScopedPermission
from servers.admin_access.masking import mask_contact, mask_phone
from servers.admin_access.scoping import (
    ScopeNotHeld, scoped_or_none, scoped_to_request,
)
from servers.admin_audit.services import record_admin_action

from .models import SOSEvent, SOSEventUpdate

logger = logging.getLogger(__name__)

# How long an identical, still-open SOS suppresses a repeat.
#
# Short on purpose. The cost of being too long is a real second
# emergency being silently folded into the first; the cost of being too
# short is ops seeing two alerts. Those are not symmetric.
SOS_DEDUPE_WINDOW_SECONDS = 120


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
        initiated_by: string  ignored; derived from the authenticated user's role
    """
    from servers.ride.models import Trip

    data = request.data if hasattr(request, 'data') else {}
    event_type = (data.get('event_type') or 'panic')[:32]
    initiated_by = getattr(request.user, 'role', '')
    if initiated_by not in ('rider', 'driver'):
        return error_response(
            code='INVALID_ROLE',
            message='Only riders and drivers can raise SOS events',
            field='role',
            issue=f'Authenticated user role={initiated_by!r}',
            status=status.HTTP_403_FORBIDDEN,
        )

    trip = None
    trip_id = data.get('trip_id')
    if trip_id:
        try:
            trip = Trip.objects.filter(
                Q(user_id=request.user) |
                Q(driver_id__user_id=request.user),
                id=trip_id,
            ).first()
        except (TypeError, ValueError):
            trip = None
        if trip is None:
            return error_response(
                code='TRIP_NOT_FOUND',
                message='Trip not found for the authenticated user',
                field='trip_id',
                issue=f'No trip {trip_id!r} belongs to user {request.user.id}',
                status=status.HTTP_404_NOT_FOUND,
            )

    # A panic button gets pressed more than once, and a reconnecting app retries.
    # Each repeat used to create another SOSEvent and another fan-out: ops paged
    # repeatedly for one emergency, which is how a real alert gets tuned out.
    #
    # Collapsed only when ALL of these hold, so a genuine escalation is never
    # suppressed:
    #   * same user, same trip, same event type -- a different type is a new event;
    #   * the original is still `open` -- once ops have acknowledged or resolved it,
    #     a new SOS means something new is happening;
    #   * inside a short window -- minutes later is a new emergency, not a retry.
    #
    # Deliberately decided in PostgreSQL rather than Redis: a cache miss must never
    # be the reason a safety event is recorded twice, and a cache HIT must never be
    # the reason one is dropped.
    existing = SOSEvent.objects.filter(
        user=request.user,
        trip=trip,
        event_type=event_type if event_type in dict(SOSEvent.EVENT_TYPE_CHOICES) else 'panic',
        status='open',
        created_at__gte=timezone.now() - timedelta(seconds=SOS_DEDUPE_WINDOW_SECONDS),
    ).order_by('-created_at').first()
    if existing is not None:
        logger.warning('sos_repeat_collapsed', extra={
            'event': 'sos_repeat_collapsed',
            'sos_id': existing.id,
            'user_id': request.user.id,
            'trip_id': trip.id if trip else None,
            'event_type': existing.event_type,
        })
        return success_response(
            {
                'sos_id': existing.id,
                'status': existing.status,
                'repeat_of': existing.id,
                'message': 'SOS already recorded. Help is on the way.',
            },
            status.HTTP_200_OK,
        )

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

    # Deliberately no coordinates. SOS is where location is most sensitive, and a
    # log line is retained, shipped and searchable; the event id is enough for
    # anyone entitled to the position to read it from the database.
    logger.warning('sos_raised', extra={
        'event': 'sos_raised',
        'sos_id': event.id,
        'initiated_by': initiated_by,
        'user_id': request.user.id,
        'trip_id': trip.id if trip else None,
        'event_type': event.event_type,
        'has_location': event.latitude is not None and event.longitude is not None,
        # Routing, not position: the node code is a city, never a coordinate.
        'anchor_source': event.anchor_source,
        'anchor_node': event.anchor_node.code if event.anchor_node_id else None,
    })

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


#: Statuses in which somebody still needs reaching.
ACTIVE_STATUSES = ('open', 'acknowledged')

#: The audit action written by every contact reveal. Grep for it.
CONTACT_REVEALED_ACTION = 'sos_contact_revealed'

SOS_ADMIN_QUERYSET = SOSEvent.objects.select_related(
    'user', 'trip', 'trip__driver_id', 'trip__zone', 'trip__zone__geo_node',
    'anchor_node',
)


def _not_found(sos_id):
    """The one answer for "does not exist" AND "not in your scope".

    Two different messages would let an out-of-scope operator tell which
    ids are real. See `scoping.scoped_or_none`.
    """
    return error_response(
        code='NOT_FOUND',
        message='SOS event not found',
        field='sos_id',
        issue=f'No SOS event with id {sos_id}',
        status=status.HTTP_404_NOT_FOUND,
    )


def _scoped_event_or_none(request, sos_id):
    """The event, if it exists AND sits inside the caller's scope."""
    return scoped_or_none(request.user, SOS_ADMIN_QUERYSET, id=sos_id)


def _serialize_event(ev):
    """One queue row. Contact details masked -- for everybody.

    `user_phone` was the raiser's real number, sent to any operator who
    could open the queue. Now it keeps its country code and last four, the
    same shape as every other masked number in the console, and the only
    way to the real value is `reveal_sos_contact`, which is logged.
    """
    trip = ev.trip
    return {
        'id': ev.id,
        'status': ev.status,
        'event_type': ev.event_type,
        'initiated_by': ev.initiated_by,
        'user_label': mask_contact(ev.user_label),
        'user_phone': mask_phone(getattr(ev.user, 'phone_number', None)),
        'contact_masked': True,
        'trip_id': trip.id if trip else None,
        'driver_label': (
            mask_contact(str(trip.driver_id)) if trip and trip.driver_id else None
        ),
        'latitude': str(ev.latitude) if ev.latitude is not None else None,
        'longitude': str(ev.longitude) if ev.longitude is not None else None,
        # Where the event was routed, and how. `unanchored` is the one the
        # console must shout about: nobody's city, everybody's queue.
        'anchor': {
            'source': ev.anchor_source,
            'node': ev.anchor_node.code if ev.anchor_node_id else None,
            'node_name': ev.anchor_node.name if ev.anchor_node_id else None,
        },
        'note': ev.note,
        'created_at': ev.created_at.isoformat(),
        'updates': [
            {
                'actor': u.actor_label,
                'new_status': u.new_status,
                'note': u.note,
                'created_at': u.created_at.isoformat(),
            }
            for u in ev.updates.all()
        ],
    }


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('safety.respond')])
def acknowledge_sos(request, sos_id):
    """Mark an SOS event acknowledged by ops."""
    return _admin_transition(request, sos_id, target='acknowledged')


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('safety.respond')])
def resolve_sos(request, sos_id):
    """Mark an SOS event resolved. Accepts a resolution note."""
    return _admin_transition(request, sos_id, target='resolved')


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('safety.respond')])
def false_alarm_sos(request, sos_id):
    """Mark an SOS event as false alarm. Accepts a note explaining why."""
    return _admin_transition(request, sos_id, target='false_alarm')


def _admin_transition(request, sos_id, target):
    note = (request.data.get('note') or '')[:10000] if hasattr(request, 'data') else ''

    # Scope is answered BEFORE the row lock, through the scoped queryset,
    # so the lock itself stays a plain `get(id=...)`: a scope filter joins
    # through nullable columns, and Postgres refuses FOR UPDATE on the
    # nullable side of an outer join (see `bridge.in_scope`). The window
    # between the two reads can only end in a refusal, never in an action.
    if _scoped_event_or_none(request, sos_id) is None:
        return _not_found(sos_id)

    try:
        with transaction.atomic():
            try:
                event = SOSEvent.objects.select_for_update().get(id=sos_id)
            except SOSEvent.DoesNotExist:
                return _not_found(sos_id)

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


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('safety.respond')])
def admin_list_sos(request):
    """Ops SOS queue.

    Query params:
      status  -- open | acknowledged | resolved | false_alarm  (default: all)
      limit   -- page size, max 200 (default 50)
      offset  -- pagination offset

    The acknowledge / resolve endpoints existed but there was no way to
    SEE the queue: ops could only act on an SOS whose id they already had,
    which in practice meant reading it out of a push notification. SOS is
    the one SLO with a zero error budget, so it needs a list an on-call
    person can keep open.

    Ordered open-first, then newest-first — an unacknowledged panic from
    ten minutes ago must never sit below a resolved one from ten seconds
    ago.
    """
    from django.db.models import Case, IntegerField, Value, When

    # Scoped to the caller's cities by the trip's zone, else the raiser's
    # home city. Without `scoped()` the permission check above would pass
    # and the queue would still hold every event in both countries.
    try:
        qs = scoped_to_request(request, SOS_ADMIN_QUERYSET)
    except ScopeNotHeld as exc:
        return error_response(
            code='SCOPE_NOT_HELD', message=str(exc), field='scope',
            issue='Requested scope is outside your assignments',
            status=status.HTTP_403_FORBIDDEN,
        )

    status_filter = (request.query_params.get('status') or '').strip()
    if status_filter:
        valid = {c[0] for c in SOSEvent.STATUS_CHOICES}
        if status_filter not in valid:
            return error_response(
                code='INVALID_STATUS',
                message=f'status must be one of: {", ".join(sorted(valid))}',
                field='status',
                issue=f'Got status={status_filter!r}',
                status=status.HTTP_400_BAD_REQUEST,
            )
        qs = qs.filter(status=status_filter)

    try:
        limit = min(int(request.query_params.get('limit', 50)), 200)
        offset = max(int(request.query_params.get('offset', 0)), 0)
    except (TypeError, ValueError):
        limit, offset = 50, 0

    qs = qs.annotate(
        triage_rank=Case(
            When(status='open', then=Value(0)),
            When(status='acknowledged', then=Value(1)),
            default=Value(2),
            output_field=IntegerField(),
        )
    ).order_by('triage_rank', '-created_at')

    total = qs.count()
    # The open count is of the SCOPED queue, with the status filter
    # removed: the badge must count what this operator can act on, not the
    # other country's open alarms.
    open_count = scoped_to_request(request, SOSEvent.objects.all()).filter(
        status='open',
    ).count()

    results = [_serialize_event(ev) for ev in qs[offset:offset + limit]]

    return success_response(
        {
            'count': total,
            'open_count': open_count,
            'limit': limit,
            'offset': offset,
            'results': results,
        },
        status.HTTP_200_OK,
    )


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('safety.respond')])
def admin_sos_detail(request, sos_id):
    """One event, masked, if it is in the caller's scope. Else NOT FOUND."""
    event = _scoped_event_or_none(request, sos_id)
    if event is None:
        return _not_found(sos_id)
    return success_response(_serialize_event(event), status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('safety.respond')])
def reveal_sos_contact(request, sos_id):
    """The unmasked contact of the person who raised an ACTIVE incident.

    Body:
        reason: string   optional -- lands on the audit row

    This is the reveal-and-log half of the masking design (see
    `admin_access.masking`): `safety.respond` in the event's scope, the
    event still `open` or `acknowledged`, and an `sos_contact_revealed`
    audit row naming the operator, the event and the fields handed over.
    If the audit write fails the reveal still returns -- a person in danger
    outranks a log row -- but it is logged at ERROR so the gap is visible.

    A resolved or false-alarm event answers 409: there is nobody left to
    reach, and "I needed the number after the fact" is exactly the access
    this endpoint exists to refuse. Super Admin goes through the same door
    and is logged the same way.
    """
    event = _scoped_event_or_none(request, sos_id)
    if event is None:
        return _not_found(sos_id)

    if event.status not in ACTIVE_STATUSES:
        return error_response(
            code='SOS_NOT_ACTIVE',
            message='Contact details can only be revealed while the incident is open or acknowledged.',
            field='status',
            issue=f'SOS event {event.id} is {event.status}',
            status=status.HTTP_409_CONFLICT,
        )

    raiser = event.user
    contact = {
        'user_label': event.user_label,
        'full_name': getattr(raiser, 'full_name', None) if raiser else None,
        'phone_number': getattr(raiser, 'phone_number', None) if raiser else None,
        'emergency_contact': getattr(raiser, 'emergency_contact', None) if raiser else None,
    }
    reason = (request.data.get('reason') or '')[:10000] if hasattr(request, 'data') else ''

    # The row names WHICH fields were handed over, never their values: the
    # audit log is read by more people than may see a phone number.
    row = record_admin_action(
        request,
        action=CONTACT_REVEALED_ACTION,
        target_type='sos_event',
        target_id=event.id,
        before={'contact_masked': True},
        after={
            'contact_masked': False,
            'fields': sorted(k for k, v in contact.items() if v),
            'event_status': event.status,
            'raiser_user_id': getattr(raiser, 'id', None),
        },
        reason=reason,
    )
    if row is None:
        logger.error(
            'sos_contact_reveal_unaudited sos_id=%s actor_id=%s',
            event.id, getattr(request.user, 'id', None),
        )

    return success_response(
        {
            'sos_id': event.id,
            'status': event.status,
            'contact': contact,
            'audit_id': getattr(row, 'id', None),
        },
        status.HTTP_200_OK,
    )
