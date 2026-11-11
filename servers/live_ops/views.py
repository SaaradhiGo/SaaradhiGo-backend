"""The Live Ops map's operator API.

  POST /api/v1/live-ops/admin/ws-ticket/            liveops.read | liveops.aggregate -> single-use socket ticket
  GET  /api/v1/live-ops/admin/snapshot/?city=       liveops.read | liveops.aggregate -> dots, trips, alerts, city status
  GET  /api/v1/live-ops/admin/heatmap/?city=&window= liveops.read | liveops.aggregate -> demand and supply cells

All under `admin/` so the structural guard (`tests/test_admin_namespace_is_gated`)
and the PII sweep find them by path. The capability decides WHAT the caller
may see (dots, or cells only); `LiveScope` decides WHERE. A city outside the
caller's scope is a 404, identical to a city that does not exist.
"""

import logging

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.utils import error_response, success_response
from servers.admin_access.bridge import AnyOfScopedPermission

from . import audit
from .config import TICKET_TTL_SECONDS
from .heatmap import BadWindow, build_heatmap
from .scope import AGGREGATE, READ, LiveScope, coordinates_exact
from .snapshot import CityNotVisible, build_snapshot
from .tickets import TicketStoreUnavailable, issue_ticket

logger = logging.getLogger(__name__)

#: Either capability opens the door; the scope computed from them decides the rest.
GATE = AnyOfScopedPermission(READ, AGGREGATE)

WS_PATH = '/ws/admin/live-locations/'


def _no_such_city(code):
    return error_response(
        code='NOT_FOUND', message='No such city', field='city',
        issue=f'No city {code!r} in your scope', status=status.HTTP_404_NOT_FOUND)


@api_view(['POST'])
@permission_classes([IsAuthenticated, GATE])
def ws_ticket(request):
    """Mint the one-time ticket the socket needs. No body.

    The socket re-reads the caller's capabilities when it connects, so a
    ticket confers nothing by itself: it only proves this account asked, in
    the last 30 seconds, over an authenticated request.
    """
    scope = LiveScope.of(request.user)
    if not scope:
        return error_response(
            code='FORBIDDEN', message='You do not hold the live map capability.',
            field='capability', issue='Neither liveops.read nor liveops.aggregate',
            status=status.HTTP_403_FORBIDDEN)
    try:
        ticket = issue_ticket(request.user.id)
    except TicketStoreUnavailable:
        logger.error('live_ops: ticket store unavailable')
        return error_response(
            code='UNAVAILABLE', message='The live map is unavailable. Try again shortly.',
            field='general', issue='Ticket store unavailable',
            status=status.HTTP_503_SERVICE_UNAVAILABLE)
    audit.ticket_issued(request, scope=scope)
    return success_response(
        {'ticket': ticket, 'expires_in': TICKET_TTL_SECONDS, 'path': WS_PATH, 'mode': scope.mode},
        status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([IsAuthenticated, GATE])
def snapshot(request):
    """`?city=<code>` narrows to one city; omitted = every city in scope.
    `liveops.aggregate`-only callers get city status and zone outlines, with
    no `drivers`, `trips` or `alerts` keys at all."""
    scope = LiveScope.of(request.user)
    if not scope:
        return error_response(
            code='FORBIDDEN', message='You do not hold the live map capability.',
            field='capability', issue='Neither liveops.read nor liveops.aggregate',
            status=status.HTTP_403_FORBIDDEN)
    city = (request.query_params.get('city') or '').strip()
    try:
        data = build_snapshot(request.user, scope, city_code=city,
                              exact=coordinates_exact(request.user))
    except CityNotVisible:
        return _no_such_city(city)
    audit.view_once(request, kind='snapshot', scope=scope, city=city)
    return success_response(data, status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated, GATE])
def heatmap(request):
    """`?city=<code>&window=15|60|1440` (minutes, default 15)."""
    scope = LiveScope.of(request.user)
    if not scope:
        return error_response(
            code='FORBIDDEN', message='You do not hold the live map capability.',
            field='capability', issue='Neither liveops.read nor liveops.aggregate',
            status=status.HTTP_403_FORBIDDEN)
    city = (request.query_params.get('city') or '').strip()
    try:
        data = build_heatmap(scope, city_code=city, window=request.query_params.get('window'))
    except BadWindow:
        return error_response(
            code='INVALID_WINDOW', message='window must be one of: 15, 60, 1440',
            field='window', issue='Minutes: 15, 60 or 1440', status=status.HTTP_400_BAD_REQUEST)
    except CityNotVisible:
        return _no_such_city(city)
    audit.view_once(request, kind='heatmap', scope=scope, city=city,
                    detail={'window_minutes': data['window_minutes']})
    return success_response(data, status.HTTP_200_OK)
