"""The Command Centre's operator API.

  GET  /api/v1/command-centre/admin/overview/?scope=&window=   report.read, scoped
  GET  /api/v1/command-centre/admin/alerts/?city=&kind=&open=1 report.read, city rows scoped
  POST /api/v1/command-centre/admin/alerts/<id>/acknowledge/   safety.respond | trip.write | finance.read, then the kind's own
  GET  /api/v1/command-centre/admin/thresholds/?city=          report.read, scoped
  POST /api/v1/command-centre/admin/thresholds/change/         platform.settings -> 202, a checker decides

All under `admin/` so the structural guard (`tests/test_admin_namespace_is_gated`)
finds them by path.

SCOPE
-----
The overview lists the cities the caller holds (`scoping.scoped_to_request`
over `GeoNode`), narrowed further by `?scope=`; a Super Admin sees every
active city plus any paused city that has data. Money (`collected`,
pending payout sums) is withheld unless the caller holds `finance.read`,
as the shared dashboard already does: the keys are removed, not zeroed.

Alerts anchored to a city are narrowed to the caller's scope; an alert
with no city (an SOS nobody could place) stays visible to every
`report.read` holder, which is the point of it.

ACKNOWLEDGING
-------------
The route admits anyone holding one of the acknowledging capabilities
(`safety.respond`, `trip.write`, `finance.read`); the kind's OWN permission
(`ACK_PERMISSION`) is asked inside, at the alert's city when it has one. So a City Ops Manager (`trip.write`) takes a
rate alert for their city, Finance (`finance.read`) takes a payout-age
alert, Safety (`safety.respond`) takes an SOS alert, and an Analyst who may
only list takes nothing. Every acknowledgement is audited.
"""

import hashlib
import logging

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.utils import error_response, success_response
from servers.admin_access.access import scope_paths
from servers.admin_access.bridge import AnyOfScopedPermission, ScopedPermission, granted
from servers.admin_access.models import ACTIVE, CITY, GeoNode
from servers.admin_access.scoping import ScopeNotHeld, requested_node, scoped_to_request
from servers.admin_audit.services import record_admin_action
from servers.approvals.service import ApprovalError

from .metrics import METRICS, OVERVIEW_WINDOWS, describe
from .models import (
    ACK_ALSO, ACK_PERMISSION, FINANCE_ALERT_KINDS, FRAUD_ALERT_KINDS, PLATFORM_ALERT_KINDS,
    OperatorAlert,
)
from .overview import build_overview, has_data
from .services import acknowledge
from .thresholds import active_thresholds, propose_threshold_change

logger = logging.getLogger(__name__)

STATUSES = ('open', 'acknowledged', 'all')


ACK_GATE = AnyOfScopedPermission(*sorted(
    set(ACK_PERMISSION.values()) | {p for extra in ACK_ALSO.values() for p in extra}))


def _serialize(alert):
    return {
        'id': alert.id,
        'kind': alert.kind,
        'severity': alert.severity,
        'title': alert.title,
        'payload': alert.payload,
        'source_type': alert.source_type,
        'source_id': alert.source_id,
        'city': alert.city.code if alert.city_id else None,
        'city_name': alert.city.name if alert.city_id else None,
        'created_at': alert.created_at.isoformat(),
        'acknowledged_at': alert.acknowledged_at.isoformat() if alert.acknowledged_at else None,
        'acknowledged_by': alert.acknowledged_by_id,
        'acknowledged_note': alert.acknowledged_note,
        'ack_permission': ACK_PERMISSION.get(alert.kind),
    }


def _scope_refused(exc):
    return error_response(
        code='SCOPE_NOT_HELD', message=str(exc), field='scope', issue=str(exc),
        status=status.HTTP_403_FORBIDDEN,
    )


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

def _cities_for(request):
    """The city nodes this caller may see, narrowed by `?scope=`."""
    cities = scoped_to_request(request, GeoNode.objects.filter(kind=CITY)).order_by('path')
    return list(cities), requested_node(request)


def _cache_key(request, window, include_money, node, include_sos=True, finance_codes=()):
    paths = scope_paths(request.user)
    digest = hashlib.sha256(repr((
        sorted(paths) if paths is not None else '*',
        node.code if node is not None else '',
        window, include_money, include_sos, tuple(finance_codes),
    )).encode()).hexdigest()[:32]
    return f'command_centre:overview:{digest}'


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('report.read')])
def admin_overview(request):
    """Per-city rows with last-week deltas. `?window=today|24h|7d` (default
    `today`), `?scope=<node code>` to narrow. Cached for
    `COMMAND_CENTRE_OVERVIEW_CACHE_SECONDS` per (caller's scope, node,
    window, money visibility)."""
    window = (request.query_params.get('window') or 'today').strip()
    if window not in OVERVIEW_WINDOWS:
        return error_response(
            code='INVALID_WINDOW', message=f'window must be one of: {", ".join(OVERVIEW_WINDOWS)}',
            field='window', issue=f'Got {window!r}', status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        cities, node = _cities_for(request)
    except ScopeNotHeld as exc:
        return _scope_refused(exc)

    include_money, _how = granted(request.user, 'finance.read')
    include_sos = sees_live_sos(request.user)
    # Aggregate money tiles (finance.aggregate): city totals only, in the places
    # that capability is held. Never a per-driver figure.
    from servers.payments.aggregates import cities_with_aggregate, finance_aggregates

    finance_cities = cities_with_aggregate(request.user, cities)
    ttl = int(getattr(settings, 'COMMAND_CENTRE_OVERVIEW_CACHE_SECONDS', 30) or 0)
    key = _cache_key(request, window, include_money, node, include_sos,
                     [c.code for c in finance_cities] + [f'recon:{include_money}'])
    if ttl:
        cached = cache.get(key)
        if cached is not None:
            return success_response({**cached, 'cached': True}, status.HTTP_200_OK)

    payload = build_overview(cities, window, include_money=include_money,
                             include_sos=include_sos, scope_code=node.code if node is not None else None)
    payload['finance'] = {
        'visible': bool(finance_cities),
        'tiles': list(finance_aggregates(finance_cities, window).values()) if finance_cities else [],
        # The reconciliation page needs finance.read; the tile shows the count
        # to every holder but links only for those who can open the page.
        'reconciliation_link': bool(include_money),
    }
    # On an UNRESTRICTED board (Super Admin) a paused city with nothing in
    # it is noise, while a paused city with trips (Austin on QA) is a row
    # the owner asked to see. An operator scoped to a city always gets that
    # city's row, paused or not: an empty board that says "no requests in
    # this window" is their truth, and "no cities in your scope" is not.
    if scope_paths(request.user) is None:
        by_code = {c.code: c for c in cities}
        payload['cities'] = [
            row for row in payload['cities']
            if by_code[row['code']].status == ACTIVE or has_data(row)
        ]
    payload['cached'] = False
    if ttl:
        cache.set(key, payload, ttl)
    return success_response(payload, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

#: Who sees LIVE SOS (owner ruling 2026-10-30): the responders
#: (`safety.respond`: Safety, Super Admin) and City Ops (`trip.write`),
#: each within their scope. Marketing and Analyst hold `report.read` and see
#: the rest of the board; SOS history for analysis belongs in Reports.
LIVE_SOS_CAPABILITIES = ('safety.respond', 'trip.write')
#: Alert kinds that are live SOS: the ones Safety acknowledges.
SOS_ALERT_KINDS = frozenset(k for k, cap in ACK_PERMISSION.items() if cap == 'safety.respond')


def sees_live_sos(user):
    return any(granted(user, cap)[0] for cap in LIVE_SOS_CAPABILITIES)


def _visible_alerts(user):
    """City-anchored alerts within the caller's scope, plus every alert
    that has no city at all; live SOS alerts only for `sees_live_sos`."""
    from django.db.models import Q

    from servers.admin_access.bridge import is_migrated, strict_mode

    everything = OperatorAlert.objects.select_related('city')
    qs = everything.exclude(kind__in=FINANCE_ALERT_KINDS | FRAUD_ALERT_KINDS)
    if not granted(user, 'platform.settings')[0]:
        qs = qs.exclude(kind__in=PLATFORM_ALERT_KINDS)
    if not sees_live_sos(user):
        qs = qs.exclude(kind__in=SOS_ALERT_KINDS)
    finance = _visible_finance_alerts(user, everything) | _visible_fraud_alerts(user, everything)
    if not strict_mode() and not is_migrated(user):
        return qs | finance  # unmigrated accounts are unscoped everywhere, until strict mode
    paths = scope_paths(user)
    if paths is None:
        return qs | finance
    covered = Q(city__isnull=True)
    for path in paths:
        covered |= Q(city__path__startswith=path)
    return qs.filter(covered) | finance


def _visible_fraud_alerts(user, everything):
    """Fraud alerts (`FRAUD_ALERT_KINDS`): only holders of `fraud.read`, and only for the cities
    in their scope. An alert with no city is for unrestricted callers only."""
    from servers.admin_access.access import scope_paths as _paths

    qs = everything.filter(kind__in=FRAUD_ALERT_KINDS)
    if not granted(user, 'fraud.read')[0]:
        return qs.none()
    paths = _paths(user)
    if paths is None:
        return qs
    covered = Q(pk__in=[])
    for path in paths:
        covered |= Q(city__path__startswith=path)
    return qs.filter(covered)


def _visible_finance_alerts(user, everything):
    """Money alerts (`FINANCE_ALERT_KINDS`): only holders of `finance.read`, and only for
    the cities where THAT capability is held. An alert with no city is for unrestricted
    callers only (the finance rule: what cannot be placed is not shown to a city)."""
    from servers.payments.scope import paths_for

    qs = everything.filter(kind__in=FINANCE_ALERT_KINDS)
    if not granted(user, 'finance.read')[0]:
        return qs.none()
    paths = paths_for(user, 'finance.read')
    if paths is None:
        return qs
    covered = Q(pk__in=[])
    for path in paths:
        covered |= Q(city__path__startswith=path)
    return qs.filter(covered)


@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('report.read')])
def admin_list_alerts(request):
    """Query params: status=open|acknowledged|all (default open), open=1
    (same as status=open), city=<node code> (that node and everything under
    it), kind=<alert kind>, limit, offset."""
    wanted = (request.query_params.get('status') or 'open').strip()
    if request.query_params.get('open') in ('1', 'true'):
        wanted = 'open'
    if wanted not in STATUSES:
        return error_response(
            code='INVALID_STATUS', message=f'status must be one of: {", ".join(STATUSES)}',
            field='status', issue=f'Got {wanted!r}', status=status.HTTP_400_BAD_REQUEST,
        )
    qs = _visible_alerts(request.user)
    if wanted == 'open':
        qs = qs.filter(acknowledged_at__isnull=True)
    elif wanted == 'acknowledged':
        qs = qs.filter(acknowledged_at__isnull=False)

    city_code = (request.query_params.get('city') or '').strip()
    if city_code:
        node = GeoNode.objects.filter(code=city_code).first()
        if node is None:
            return error_response(
                code='NOT_FOUND', message='No such city', field='city',
                issue=f'No node {city_code!r}', status=status.HTTP_404_NOT_FOUND,
            )
        qs = qs.filter(city__path__startswith=node.path)

    kind = (request.query_params.get('kind') or '').strip()
    if kind:
        qs = qs.filter(kind=kind)

    try:
        limit = min(int(request.query_params.get('limit', 50)), 200)
        offset = max(int(request.query_params.get('offset', 0)), 0)
    except (TypeError, ValueError):
        limit, offset = 50, 0

    return success_response(
        {
            'count': qs.count(),
            'open_count': _visible_alerts(request.user).filter(acknowledged_at__isnull=True).count(),
            'limit': limit,
            'offset': offset,
            'results': [_serialize(a) for a in qs[offset:offset + limit]],
        },
        status.HTTP_200_OK,
    )


@api_view(['POST'])
@permission_classes([IsAuthenticated, ACK_GATE])
def acknowledge_alert(request, alert_id):
    """Body: {"note": "..."} optional. Idempotent: a second ack is a 200 no-op.

    Taking an alert is for the people who can act on what it points at:
    the kind's own permission (`ACK_PERMISSION`) is required, at the
    alert's city when it has one. A kind that is not listed cannot be
    acknowledged over the API at all. An alert outside the caller's scope
    answers 404, like every out-of-scope object.
    """
    alert = _visible_alerts(request.user).filter(id=alert_id).first()
    if alert is None:
        return error_response(
            code='NOT_FOUND', message='Alert not found', field='alert_id',
            issue=f'No alert {alert_id}', status=status.HTTP_404_NOT_FOUND,
        )

    needed = ACK_PERMISSION.get(alert.kind)
    allowed = needed is not None and any(
        granted(request.user, cap, alert.city)[0] for cap in (needed, *ACK_ALSO.get(alert.kind, ())))
    if not allowed:
        return error_response(
            code='FORBIDDEN',
            message='You do not hold the capability that owns this alert.',
            field='alert_id',
            issue=f'{alert.kind} is acknowledged by holders of {needed or "(no capability: not acknowledgeable)"}',
            status=status.HTTP_403_FORBIDDEN,
        )

    note = (request.data.get('note') or '') if hasattr(request, 'data') else ''
    changed = acknowledge(alert, by=request.user, note=note)
    if changed:
        record_admin_action(
            request, action='operator_alert_acknowledged',
            target_type='operator_alert', target_id=alert.id,
            before={'acknowledged': False},
            after={'acknowledged': True, 'kind': alert.kind, 'city': alert.city.code if alert.city_id else None,
                   'source_type': alert.source_type, 'source_id': alert.source_id},
            reason=note,
        )
    return success_response(_serialize(alert), status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([IsAuthenticated, ScopedPermission('report.read')])
def list_thresholds(request):
    """The active threshold of every (city, metric) in the caller's scope,
    narrowed by `?city=` / `?scope=`, plus the metric catalogue."""
    try:
        cities, _node = _cities_for(request)
    except ScopeNotHeld as exc:
        return _scope_refused(exc)
    city_code = (request.query_params.get('city') or '').strip()
    if city_code:
        cities = [c for c in cities if c.code == city_code or c.path.startswith(f'{city_code}/')
                  or f'/{city_code}/' in c.path]
    rows = active_thresholds(cities).order_by('city__path', 'metric')
    return success_response({
        'metrics': [describe(m) for m in METRICS.values()],
        'results': [t.as_dict() for t in rows],
        'count': rows.count(),
    }, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('platform.settings')])
def change_threshold(request):
    """Propose a threshold. Body: city (node code), metric, warn_value,
    critical_value (numbers or null; at least one), window (rates only),
    reason (required).

    Returns 202 with the `command_centre.threshold_change` request a
    second `platform.settings` holder over that city must approve. Nothing
    is written until then.
    """
    data = request.data or {}
    city = GeoNode.objects.filter(code=str(data.get('city') or ''), kind=CITY).first()
    if city is None:
        return error_response(
            code='NOT_FOUND', message='No such city', field='city',
            issue=f'No city {data.get("city")!r}', status=status.HTTP_404_NOT_FOUND,
        )
    try:
        outcome = propose_threshold_change(
            request.user, city=city, metric=str(data.get('metric') or ''),
            warn_value=data.get('warn_value'), critical_value=data.get('critical_value'),
            window=str(data.get('window') or ''), reason=str(data.get('reason') or ''),
            http_request=request,
        )
    except ApprovalError as exc:
        return exc.response()
    return success_response(
        {'approval_request': outcome.request.as_dict(), 'duplicate': outcome.duplicate},
        status.HTTP_202_ACCEPTED,
    )
