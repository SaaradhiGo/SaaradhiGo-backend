"""Console search: find a trip, a driver, a rider or a vehicle from one box.

  GET /api/v1/search/admin/trips/?q=     trip.read
  GET /api/v1/search/admin/drivers/?q=   driver.read
  GET /api/v1/search/admin/riders/?q=    rider.read
  GET /api/v1/search/admin/vehicles/?q=  driver.read

FOUR ROUTES, NOT ONE
--------------------
`ScopedPermission` carries ONE capability, and the structural guard
(`tests/test_admin_namespace_is_gated.py`) requires every admin route to
carry exactly that. A single `/search/` answering four result types would
have to be gated on one of them and leak the other three to whoever holds
it. So each type is its own route behind its own capability, and the
console calls the four in parallel and hides a group that answers 403.

SCOPED THE SAME WAY AS THE LISTS
--------------------------------
Each queryset goes through `scoped_to_request`, the same helper the list
endpoints use, with the same `SCOPE_PATHS` anchors: a trip by its pickup
zone, a driver by their home city, a vehicle through its driver. A row the
operator could not open from the list does not appear here either -- there
is no "found but not permitted" state, because that is the enumeration
leak this module exists not to add. Riders are global by design
(`scoping.RIDERS_ARE_GLOBAL`) and are gated on `rider.read` alone.

A KYC-only operator (`driver.kyc` without `driver.write` or `trip.read`)
sees drivers still in KYC and nothing else, by the same predicate the
driver list uses (`driver.admin_views.kyc_only` / `IN_KYC`, imported, not
copied).

WHAT LEAVES THE SERVER
----------------------
Ids, status, city code, a name and a MASKED phone. The phone is masked for
every caller regardless of `pii.view`: search is for finding the record,
and the record's own page is where a reveal happens and is logged. Email
addresses and documents are never in a search result. Names follow the
project rule (`masking.mask_for`): first name plus initial unless the
operator holds `pii.view`.

Reads are not audited (a row per keystroke would bury the log), but every
search is rate-limited per operator and logged at INFO with the LENGTH of
the term and its kind -- never the term, which is somebody's phone number.
"""

import logging

from django.db.models import Max, Q
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated

from base.db_indexes import phone_tail
from base.utils import error_response, success_response
from servers.admin_access.bridge import ScopedPermission
from servers.admin_access.masking import mask_for, mask_phone
from servers.admin_access.scoping import ScopeNotHeld, scoped_to_request
from servers.driver.admin_views import IN_KYC, for_kyc_view, kyc_only
from servers.driver.models import Driver, Vehicle
from servers.driver.plates import normalised_plate_expression
from servers.ride.models import Trip
from servers.rider.models import Rider

from .terms import TermError, parse_term
from .throttles import AdminSearchThrottle

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 10
MAX_LIMIT = 25


# ---------------------------------------------------------------------------
# Plumbing shared by the four routes
# ---------------------------------------------------------------------------

def _limit(request):
    raw = request.query_params.get('limit')
    try:
        value = int(raw) if raw is not None else DEFAULT_LIMIT
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(value, MAX_LIMIT))


def _term_or_response(request):
    try:
        return parse_term(request.query_params.get('q')), None
    except TermError as exc:
        return None, error_response(
            code=exc.code, message=str(exc), field='q', issue=str(exc),
            status=status.HTTP_400_BAD_REQUEST,
        )


def _scope_refused(exc):
    return error_response(
        code='SCOPE_NOT_HELD', message=str(exc), field='scope',
        issue='Requested scope is outside your assignments',
        status=status.HTTP_403_FORBIDDEN,
    )


def _answer(request, term, what, rows):
    """The envelope every route returns. Logs length and kind, never the term."""
    logger.info(
        'admin_search: %s term_len=%d kind=%s results=%d user=%s',
        what, term.length, term.kind, len(rows), getattr(request.user, 'id', None),
    )
    body = {'kind': term.kind, 'results': mask_for(request.user, rows)}
    return success_response(body, status.HTTP_200_OK)


def _person(user):
    """The fields of a person a search row may carry. No email, ever."""
    if user is None:
        return None
    return {
        'id': user.id,
        'full_name': user.full_name or '',
        # Masked for EVERY caller; `mask_for` is not consulted for this.
        'phone': mask_phone(user.phone_number),
    }


def _phone_q(term, person='user_id'):
    """`Q` over the indexed phone lookups.

    `phone_number` is the account's unique column; `phone_tail` is the
    `RIGHT(phone_number, 10)` annotation the caller adds, matching the
    `user_phone_tail_idx` expression index.
    """
    q = Q()
    if term.phone_e164:
        q |= Q(**{f'{person}__phone_number': term.phone_e164})
    if term.phone_tail:
        q |= Q(phone_tail=term.phone_tail)
    if term.phone_prefix:
        q |= Q(phone_tail__startswith=term.phone_prefix)
    return q


def _city_code(node):
    return node.code if node is not None else None


# ---------------------------------------------------------------------------
# Trips: by id
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('trip.read')])
@throttle_classes([AdminSearchThrottle])
def admin_search_trips(request):
    term, refused = _term_or_response(request)
    if refused:
        return refused
    rows = []
    if term.trip_id is not None:
        try:
            trips = scoped_to_request(request, Trip.objects.select_related(
                'status_id', 'zone__geo_node', 'user_id', 'driver_id__user_id',
            ))
        except ScopeNotHeld as exc:
            return _scope_refused(exc)
        trip = trips.filter(pk=term.trip_id).first()
        if trip is not None:
            rows.append(_trip_row(trip))
    return _answer(request, term, 'trips', rows)


def _trip_row(trip):
    zone = trip.zone
    return {
        'type': 'trip',
        'id': trip.id,
        'status': trip.status_id.status_code if trip.status_id else 'pending',
        'city_code': _city_code(zone.geo_node if zone else None),
        'requested_at': trip.requested_at.isoformat() if trip.requested_at else None,
        'rider': _person(trip.user_id),
        'driver': _person(trip.driver_id.user_id) if trip.driver_id else None,
    }


# ---------------------------------------------------------------------------
# Drivers: by phone or name
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('driver.read')])
@throttle_classes([AdminSearchThrottle])
def admin_search_drivers(request):
    term, refused = _term_or_response(request)
    if refused:
        return refused
    rows = []
    q = Q()
    if term.searches_phones:
        q |= _phone_q(term)
    if term.name:
        q |= Q(user_id__full_name__icontains=term.name)
    if q:
        try:
            drivers = for_kyc_view(request.user, scoped_to_request(
                request, Driver.objects.select_related('user_id', 'home_city'),
            ))
        except ScopeNotHeld as exc:
            return _scope_refused(exc)
        drivers = (
            drivers.annotate(phone_tail=phone_tail('user_id__phone_number'))
            .filter(q).order_by('-id')[:_limit(request)]
        )
        rows = [_driver_row(driver) for driver in drivers]
    return _answer(request, term, 'drivers', rows)


def _driver_row(driver):
    return {
        'type': 'driver',
        'id': driver.id,
        'status': driver.status,
        'approved': driver.approved,
        'doc_status': driver.doc_status,
        'city_code': _city_code(driver.home_city),
        'user': _person(driver.user_id),
    }


# ---------------------------------------------------------------------------
# Riders: by phone or name. Global by design; see the module docstring.
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('rider.read')])
@throttle_classes([AdminSearchThrottle])
def admin_search_riders(request):
    term, refused = _term_or_response(request)
    if refused:
        return refused
    rows = []
    q = Q()
    if term.searches_phones:
        q |= _phone_q(term)
    if term.name:
        q |= Q(user_id__full_name__icontains=term.name)
    if q:
        riders = (
            Rider.objects.select_related('user_id')
            .annotate(phone_tail=phone_tail('user_id__phone_number'))
            .filter(q)
            # The console has no rider page yet; the latest trip is where a
            # rider's record can be opened from.
            .annotate(last_trip_id=Max('user_id__trips__id'))
            .order_by('-id')[:_limit(request)]
        )
        rows = [_rider_row(rider) for rider in riders]
    return _answer(request, term, 'riders', rows)


def _rider_row(rider):
    return {
        'type': 'rider',
        'id': rider.id,
        'status': 'flagged' if rider.flagged_for_review else 'active',
        'city_code': None,
        'last_trip_id': rider.last_trip_id,
        'user': _person(rider.user_id),
    }


# ---------------------------------------------------------------------------
# Vehicles: by plate. Scoped through the driver.
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('driver.read')])
@throttle_classes([AdminSearchThrottle])
def admin_search_vehicles(request):
    term, refused = _term_or_response(request)
    if refused:
        return refused
    rows = []
    if term.plate:
        try:
            vehicles = scoped_to_request(request, Vehicle.objects.select_related(
                'driver_id__user_id', 'driver_id__home_city',
            ))
        except ScopeNotHeld as exc:
            return _scope_refused(exc)
        if kyc_only(request.user):
            # The same queue the driver list shows a KYC Verifier.
            vehicles = vehicles.filter(driver_id__in=Driver.objects.filter(IN_KYC))
        vehicles = (
            vehicles.annotate(plate_norm=normalised_plate_expression())
            .filter(Q(plate_norm=term.plate) | Q(plate_norm__startswith=term.plate))
            .order_by('-id')[:_limit(request)]
        )
        rows = [_vehicle_row(vehicle) for vehicle in vehicles]
    return _answer(request, term, 'vehicles', rows)


def _vehicle_row(vehicle):
    driver = vehicle.driver_id
    return {
        'type': 'vehicle',
        'id': vehicle.id,
        'plate': vehicle.vehicle_number,
        'status': vehicle.status,
        'make': ' '.join(part for part in (vehicle.brand, vehicle.model) if part),
        'city_code': _city_code(driver.home_city) if driver else None,
        'driver_id': driver.id if driver else None,
        'driver': _person(driver.user_id) if driver else None,
    }
