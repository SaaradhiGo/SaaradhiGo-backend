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
  C  a STARTED trip (in_progress): refused, `started_trip_needs_policy`. Ending a
     started ride decides what the rider owes and what the driver earns, and
     nobody has decided that. This module does not terminate started trips.
  D  not stale: refused, `trip_active`. Nothing changes.

NOTHING HERE TOUCHES MONEY. No fare, wallet, settlement, commission, ledger or
payout row is created or changed. (Case B's only money-adjacent step is the one
every cancellation already takes: an online payment the rider had already made
for a trip that will not happen goes back to the rider.)
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
from .models import Trip

logger = logging.getLogger(__name__)

PERMISSION = 'trip.stale_resolve'
REASON_MIN, REASON_MAX = 5, 500
PRE_PICKUP = ('accepted', 'reached')
FINISHED = ('completed', 'cancelled')
CANCEL_CODE = 'operator_released_stale'

STARTED_POLICY_MESSAGE = (
    'Ending a ride that has already started needs the fare policy: what the rider '
    'owes and what the driver earns. That has not been decided, so it cannot be '
    'done from here. Use the Safety or Support route for this trip.'
)
STARTED_ROUTES = ('safety', 'support')


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
    trip the policy refusal text and the support routes are carried instead.
    """
    allowed = granted(user, PERMISSION, node_of(trip))[0]
    stale = reason if reason is not None else stale_reason(trip)
    out = {
        'can_resolve': bool(allowed),
        'is_stale': bool(stale),
        'mark_reviewed': False,
        'release_driver': False,
        'release_case': None,
        'refusal': None,
        'routes': [],
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
            out.update(release_case='C',
                       refusal={'code': 'started_trip_needs_policy',
                                'message': STARTED_POLICY_MESSAGE},
                       routes=list(STARTED_ROUTES))
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
    return (Trip.objects.select_for_update(of=('self',))
            .select_related('status_id', 'driver_id').filter(id=trip_id).first())


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
            # Case C: refused until the fare policy exists.
            return _refuse(request, trip, action=RELEASE_ACTION, case='C',
                           code='started_trip_needs_policy', message=STARTED_POLICY_MESSAGE,
                           extra={'routes': list(STARTED_ROUTES)})

        if code not in PRE_PICKUP:
            return _refuse(request, trip, action=RELEASE_ACTION, code='not_releasable',
                           message=f'A {code} trip has no driver to release.')

        # Case B: a ghost trip before pickup. The existing operator cancellation
        # with the fee waived, then a note of exactly what happened.
        result, trip = operator_cancel(
            trip.id, reason_code=CANCEL_CODE, waive_fee=True, audit_reason=reason,
            http_request=request, stale_release=True)
        if result != 'cancelled':
            return _refuse(request, trip, action=RELEASE_ACTION, case='B',
                           code='not_releasable',
                           message='The trip changed while you were looking at it. Reload it.')
        Trip.objects.filter(id=trip.id).update(stale_flagged_at=None, stale_reason='')
        cancelled = True
        pending = {'case': 'B', 'before': before}

    # Outside the lock: the cancel's after-commit work has run (marker cleared,
    # notices sent). Confirm that Redis really agrees, and repair if it did not.
    marker = reconcile_driver_active_trip(driver_id)
    extra = {'driver_marker': 'cleared' if marker in ('ok', 'repaired_cleared')
             else marker}
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
