"""Stale-trip actions (Trips follow-up): "Mark reviewed" and "Release driver".

Ported from the frozen Django `stale_rides` screen, scoped and audited.

WHAT AN OPERATOR CAN DO HERE
----------------------------
mark-reviewed   A person looked and judged the trip legitimate. Clears the
                detector's flag, records who and when, changes nothing else.
                The same silence is not flagged again (`liveness.
                reviewed_covers_silence`); new silence after fresh driver
                activity is.
release-driver  Frees a driver stuck on a ghost trip so they can earn again.

WHAT "RELEASED" MEANS
---------------------
Dispatch treats a driver as busy when `driver:active_trip:<driver_id>` is set in
Redis (the geo index drops them while it is) or when the database holds an
accepted / reached / in_progress trip for them (`driver_active_trip_ids`, the
accept-time guard). Released means BOTH are clear: the trip is no longer in an
active status and the Redis marker is deleted.

Four cases, decided under a row lock on the trip:

  A  the database already says completed / cancelled but the driver is still
     marked busy: the legacy cache repair (`reconcile_driver_active_trip`).
  B  a ghost trip BEFORE pickup (accepted / reached) that is stale: cancelled as
     an operator cancellation through `admin_trips.operator_cancel`, with no fee
     to the rider and nothing paid to the driver, and the marker cleared.
  C  a STARTED trip (in_progress): ended by the operator under the owner's ruling
     (`servers.ride.operator_end`). A reason code is required and decides the rule:
     the rider pays the fare for the distance driven, capped at the upfront price
     (driver_unresponsive, trip_abandoned, other), or the rider pays 0 and the
     platform pays the driver (safety_incident, platform_fault).
  D  not stale: refused, `trip_active`. Nothing changes.

MONEY. Cases A and B move none (B's only money-adjacent step is the one every
cancellation already takes: an online payment the rider had already made for a trip
that will not happen goes back to the rider). Case C moves exactly the money the
ruling names and nothing else; see `operator_end`.
"""

import logging

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.utils import error_response, success_response
from servers.admin_access.bridge import ScopedPermission, granted, node_of
from servers.admin_audit.services import record_admin_action

from .admin_trips import (
    TARGET, BadInput, _iso, _text, _trip_for_write, operator_cancel, stale_reason,
)
from . import operator_end
from .models import Trip

logger = logging.getLogger(__name__)

PERMISSION = 'trip.stale_resolve'
REASON_MIN, REASON_MAX = 5, 500
PRE_PICKUP = ('accepted', 'reached')
FINISHED = ('completed', 'cancelled')
CANCEL_CODE = 'operator_released_stale'



# ---------------------------------------------------------------------------
# What the caller may do now (the detail payload)
# ---------------------------------------------------------------------------

def _driver_marked_busy_on(trip):
    """True when Redis still marks the trip's driver busy on THIS trip."""
    if trip.driver_id_id is None:
        return False
    try:
        from servers.redis_client import get_driver_active_trip

        return str(get_driver_active_trip(trip.driver_id_id)) == str(trip.id)
    except Exception:  # noqa: BLE001 - an unreadable marker offers nothing
        return False


def stale_actions(user, trip, reason=None):
    """Which of the two actions `user` may take on `trip` right now.

    Built so the console never offers a button that will fail: `mark_reviewed` and
    `release_driver` are true only when the call would be accepted. For a started
    trip (case C) the reason codes the release needs are carried as well.
    """
    allowed = granted(user, PERMISSION, node_of(trip))[0]
    stale = reason if reason is not None else stale_reason(trip)
    out = {
        'can_resolve': bool(allowed),
        'is_stale': bool(stale),
        'mark_reviewed': False,
        'release_driver': False,
        'release_case': None,
        'reason_codes': [],
        'reason_min_length': REASON_MIN,
        'reason_max_length': REASON_MAX,
    }
    if not allowed:
        return out
    code = trip.status_id.status_code if trip.status_id else ''
    out['mark_reviewed'] = bool(stale)
    if code in FINISHED:
        if _driver_marked_busy_on(trip):
            out.update(release_driver=True, release_case='A')
    elif stale and trip.driver_id_id is not None:
        if code in PRE_PICKUP:
            out.update(release_driver=True, release_case='B')
        elif code == 'in_progress':
            out.update(release_driver=True, release_case='C',
                       reason_codes=operator_end.reason_code_options())
    return out


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

def _reason(data):
    value = _text(data, 'reason', required=True, limit=REASON_MAX)
    if len(value) < REASON_MIN:
        raise BadInput('REASON_TOO_SHORT',
                       f'reason must be at least {REASON_MIN} characters.', 'reason')
    return value


def _locked(trip_id):
    """The trip row under lock, with its status and driver read AFTER the lock.

    No JOIN in the locking query on purpose: a second operator waiting on the lock
    re-checks the row once the first commits, and a joined status row created in that
    first transaction (the first ever 'cancelled') is invisible to the waiter's
    snapshot, so the row would vanish from the result and the answer would be a 404.
    """
    trip = Trip.objects.select_for_update().filter(id=trip_id).first()
    if trip is not None:
        trip.status_id  # noqa: B018 - load now, inside the lock
        trip.driver_id  # noqa: B018
    return trip


def _refuse(request, trip, *, action, code, message, case=None, extra=None):
    """409 plus an audit row, so an operator's attempts are visible."""
    after = {'code': code, 'case': case,
             'status': trip.status_id.status_code if trip.status_id else None,
             'driver_id': trip.driver_id_id, **(extra or {})}
    record_admin_action(request, action=f'{action}_refused', target_type=TARGET,
                        target_id=trip.id, after=after, reason=request.data.get('reason', ''))
    return error_response(code=code, message=message,
                          field='trip_id', issue=message, status=status.HTTP_409_CONFLICT)


# ---------------------------------------------------------------------------
# Mark reviewed
# ---------------------------------------------------------------------------

MARK_ACTION = 'trip_stale_marked_reviewed'
RELEASE_ACTION = 'trip_stale_driver_released'


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission(PERMISSION)])
def admin_stale_mark_reviewed(request, trip_id):
    """POST /ride/admin/trips/<id>/stale/mark-reviewed/  {reason}"""
    trip, refusal = _trip_for_write(request, trip_id, PERMISSION)
    if refusal:
        return refusal
    try:
        reason = _reason(request.data)
    except BadInput as exc:
        return exc.response()
    with transaction.atomic():
        trip = _locked(trip.id)
        if trip is None:
            return error_response(code='NOT_FOUND', message='Trip not found.', field='trip_id',
                                  issue='gone', status=status.HTTP_404_NOT_FOUND)
        current = stale_reason(trip)
        if not current:
            return _refuse(request, trip, action=MARK_ACTION, code='not_stale',
                           message='This trip is not stale now, so there is nothing to mark '
                                   'reviewed.')
        now = timezone.now()
        before = {'stale_flagged_at': _iso(trip.stale_flagged_at), 'stale_reason': current,
                  'stale_reviewed_at': _iso(trip.stale_reviewed_at),
                  'status': trip.status_id.status_code}
        trip.stale_flagged_at = None
        trip.stale_reason = ''
        trip.stale_reviewed_at = now
        trip.stale_reviewed_by = request.user
        trip.save(update_fields=['stale_flagged_at', 'stale_reason', 'stale_reviewed_at',
                                 'stale_reviewed_by'])
        record_admin_action(
            request, action=MARK_ACTION, target_type=TARGET, target_id=trip.id, before=before,
            after={'stale_flagged_at': None, 'stale_reason': '', 'stale_reviewed_at': _iso(now),
                   'status': trip.status_id.status_code}, reason=reason)
    return success_response({'trip_id': trip.id, 'status': trip.status_id.status_code,
                             'reviewed_at': _iso(now), 'outcome': 'marked_reviewed'},
                            status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Release driver
# ---------------------------------------------------------------------------

def _release_ok(request, trip, *, case, outcome, reason, released, before, extra=None):
    after = {'case': case, 'outcome': outcome, 'driver_id': trip.driver_id_id,
             'driver_released': released, **(extra or {})}
    record_admin_action(request, action=RELEASE_ACTION, target_type=TARGET, target_id=trip.id,
                        before=before, after=after, reason=reason)
    return success_response(
        {'trip_id': trip.id, 'driver_id': trip.driver_id_id, 'case': case, 'outcome': outcome,
         'driver_released': released, 'trip_status': trip.status_id.status_code,
         'rider_charged_minor': 0, 'driver_fee_minor': 0, **(extra or {})},
        status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission(PERMISSION)])
def admin_stale_release_driver(request, trip_id):
    """POST /ride/admin/trips/<id>/stale/release-driver/  {reason}"""
    from servers.ride.liveness import reconcile_driver_active_trip

    trip, refusal = _trip_for_write(request, trip_id, PERMISSION)
    if refusal:
        return refusal
    try:
        reason = _reason(request.data)
    except BadInput as exc:
        return exc.response()

    driver_id = None
    cancelled = False
    ended = None
    with transaction.atomic():
        trip = _locked(trip.id)
        if trip is None:
            return error_response(code='NOT_FOUND', message='Trip not found.', field='trip_id',
                                  issue='gone', status=status.HTTP_404_NOT_FOUND)
        code = trip.status_id.status_code if trip.status_id else ''
        driver_id = trip.driver_id_id
        before = {'status': code, 'driver_id': driver_id,
                  'stale_reason': stale_reason(trip) or None}

        if driver_id is None:
            return _refuse(request, trip, action=RELEASE_ACTION, code='no_driver_assigned',
                           message='No driver is assigned to this trip, so there is no '
                                   'driver to release.')

        if trip.end_rule:
            return _refuse(request, trip, action=RELEASE_ACTION, case='C',
                           code='already_ended',
                           message='An operator already ended this trip. Nothing more to do.',
                           extra={'end_rule': trip.end_rule,
                                  'reason_code': trip.end_reason_code})

        if code in FINISHED:
            # Case A: the database is the truth, the Redis marker is what changes.
            outcome = reconcile_driver_active_trip(driver_id)
            if outcome == 'unavailable':
                return _refuse(request, trip, action=RELEASE_ACTION, case='A',
                               code='driver_state_unavailable',
                               message='The live driver state could not be read just now. '
                                       'Try again in a moment.')
            if outcome == 'ok':
                return _refuse(request, trip, action=RELEASE_ACTION, case='A',
                               code='nothing_to_release',
                               message='The driver is not marked busy on this finished trip. '
                                       'Nothing needed repairing.')
            return _release_ok(request, trip, case='A', outcome=outcome, reason=reason,
                               released=(outcome == 'repaired_cleared'), before=before)

        if not stale_reason(trip):
            # Case D: alive as far as anyone can tell. Nothing changes.
            return _refuse(request, trip, action=RELEASE_ACTION, case='D', code='trip_active',
                           message='This trip is not stale and is still active, so the driver '
                                   'cannot be released from it.')

        if code == 'in_progress':
            # Case C: the operator ends the started trip (the reason code decides the rule).
            refusal_response, plan, done = _end_started_trip(
                request, trip, reason=reason, before=before)
            if refusal_response is not None:
                return refusal_response
            ended = (plan, done)
            cancelled = True
        elif code not in PRE_PICKUP:
            return _refuse(request, trip, action=RELEASE_ACTION, code='not_releasable',
                           message=f'A {code} trip has no driver to release.')

        if ended is None:
            # Case B: a ghost trip before pickup. The existing operator cancellation
            # with the fee waived, then a note of exactly what happened.
            result, trip = operator_cancel(
                trip.id, reason_code=CANCEL_CODE, waive_fee=True, audit_reason=reason,
                http_request=request, stale_release=True)
            if result != 'cancelled':
                return _refuse(request, trip, action=RELEASE_ACTION, case='B',
                               code='not_releasable',
                               message='The trip changed while you were looking at it. '
                                       'Reload it.')
            Trip.objects.filter(id=trip.id).update(stale_flagged_at=None, stale_reason='')
            cancelled = True
            pending = {'case': 'B', 'before': before}

    # Outside the lock: the cancel's after-commit work has run (marker cleared,
    # notices sent). Confirm that Redis really agrees, and repair if it did not.
    marker = reconcile_driver_active_trip(driver_id)
    extra = {'driver_marker': 'cleared' if marker in ('ok', 'repaired_cleared')
             else marker}
    if ended is not None:
        plan, done = ended
        return success_response(
            {'trip_id': trip.id, 'driver_id': driver_id, 'case': 'C',
             'outcome': 'ended_by_operator', 'driver_released': True,
             'trip_status': 'completed', **plan.as_dict(), **done, **extra},
            status.HTTP_200_OK)
    record_admin_action(
        request, action=RELEASE_ACTION, target_type=TARGET, target_id=trip.id,
        before=pending['before'],
        after={'case': 'B', 'outcome': 'cancelled_and_released', 'driver_id': driver_id,
               'driver_released': True, 'cancellation_reason': CANCEL_CODE,
               'cancellation_fee_minor': 0, **extra}, reason=reason)
    return success_response(
        {'trip_id': trip.id, 'driver_id': driver_id, 'case': 'B',
         'outcome': 'cancelled_and_released', 'driver_released': cancelled,
         'trip_status': 'cancelled', 'rider_charged_minor': 0, 'driver_fee_minor': 0, **extra},
        status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Case C: end a started trip
# ---------------------------------------------------------------------------

def _reason_code(data):
    """The release reason code for a started trip: required, one of the named constants."""
    value = _text(data, 'reason_code', required=True, limit=32)
    if value not in operator_end.REASON_CODES:
        raise BadInput('INVALID_REASON_CODE',
                       f'reason_code must be one of {", ".join(operator_end.REASON_CODES)}.',
                       'reason_code')
    return value


def _end_started_trip(request, trip, *, reason, before):
    """`(refusal_response, plan, done)`. Runs inside the caller's transaction, the trip
    row locked and already known to be stale."""
    try:
        reason_code = _reason_code(request.data)
    except BadInput as exc:
        return exc.response(), None, None
    try:
        plan = operator_end.build_plan(trip, reason_code)
    except operator_end.Unavailable as exc:
        return (_refuse(request, trip, action=RELEASE_ACTION, case='C', code=exc.code,
                        message=exc.message, extra={'reason_code': reason_code}), None, None)
    done = operator_end.apply_plan(trip, plan, actor=request.user)
    record_admin_action(
        request, action=RELEASE_ACTION, target_type=TARGET, target_id=trip.id, before=before,
        after={'case': 'C', 'outcome': 'ended_by_operator', 'driver_id': trip.driver_id_id,
               'driver_released': True, **plan.as_dict(), **done}, reason=reason)
    return None, plan, done


def _preview_for(request, trip_id):
    trip, refusal = _trip_for_write(request, trip_id, PERMISSION)
    if refusal:
        return None, refusal
    code = trip.status_id.status_code if trip.status_id else ''
    if code != 'in_progress' or not stale_reason(trip) or trip.driver_id_id is None:
        return None, error_response(
            code='not_a_stale_started_trip',
            message='Only a started trip that is stale can be ended from here.',
            field='trip_id', issue=code, status=status.HTTP_400_BAD_REQUEST)
    try:
        reason_code = _reason_code(request.query_params)
        return operator_end.build_plan(trip, reason_code), None
    except BadInput as exc:
        return None, exc.response()
    except operator_end.Unavailable as exc:
        return None, error_response(code=exc.code, message=exc.message, field='trip_id',
                                    issue=exc.message, status=status.HTTP_409_CONFLICT)


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission(PERMISSION)])
def admin_stale_release_preview(request, trip_id):
    """GET /ride/admin/trips/<id>/stale/release-preview/?reason_code=

    Exactly the figures the release would apply, from the same code, so the dialog
    shows the server's numbers. Writes nothing."""
    plan, refusal = _preview_for(request, trip_id)
    if refusal:
        return refusal
    return success_response({**plan.as_dict(),
                             'reason_codes': operator_end.reason_code_options()},
                            status.HTTP_200_OK)
