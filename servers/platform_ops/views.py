"""Platform Settings & Integrations (module 15): the operator API and the public app-config.

  Public
    GET  /app-config/?country&city&app                  read-only, cacheable, rate limited

  Operator (`platform/admin/`)                          capability
    GET  markets/                                        platform.read     countries, cities, readiness summary
    GET  markets/<city>/readiness/                       platform.read     the checklist (404 outside scope)
    POST markets/<city>/status/                          platform.settings (launch) or platform.city_pause (pause): a request
    POST markets/nodes/                                  platform.settings add a country/state/city as a draft: a request
    GET  flags/        POST flags/                       platform.read / platform.settings
    GET  flags/<key>/  PUT  flags/<key>/                 platform.read / platform.settings (history; change)
    GET  app-versions/ PUT app-versions/<app>/<platform>/ platform.read / platform.settings
    GET  notices/ POST notices/ PUT notices/<id>/ POST notices/<id>/cancel/   platform.read / platform.settings
    GET  settings/ POST settings/ PUT settings/<key>/     platform.settings
    GET  integrations/                                   platform.integrations.read
    GET  options/                                        platform.read

A change that needs a checker answers 202 with the approval request; one that does not answers 200
with the new state. Every write is audited. Out of scope is 404, a missing capability 403.
"""

from datetime import datetime

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import SAFE_METHODS, AllowAny, IsAuthenticated
from rest_framework.throttling import SimpleRateThrottle

from base.utils import error_response, success_response
from servers.admin_access.bridge import AnyOfScopedPermission, ScopedPermission, granted
from servers.admin_access.models import CITY, GeoNode
from servers.approvals import service as approvals
from servers.approvals.actions import (
    PLATFORM_APP_VERSION, PLATFORM_CITY_STATUS, PLATFORM_FLAG_CHANGE, PLATFORM_SETTING_CHANGE,
)
from servers.approvals.models import REQUESTED, ApprovalRequest
from servers.pricing.models import PlatformSettings

from . import app_config as app_config_module
from . import flags, integrations, markets, notices, settings_editor, versions
from .errors import PlatformError
from .models import (
    APP_CHOICES, AUDIENCE_CHOICES, OWNER_AREAS, PLATFORM_CHOICES, SEVERITY_CHOICES,
    FeatureFlag, MaintenanceNotice,
)

READ = ScopedPermission('platform.read')
WRITE = ScopedPermission('platform.settings')
INTEGRATIONS = ScopedPermission('platform.integrations.read')
STATUS_CAP = AnyOfScopedPermission('platform.settings', 'platform.city_pause')


class ReadOrWrite(ScopedPermission):
    """`platform.read` for a read, `platform.settings` for anything that changes state, so a
    collection that is read and written at one URL has the right door for each verb."""

    def __init__(self):
        super().__init__('platform.read')

    def has_permission(self, request, view):
        permission = 'platform.read' if request.method in SAFE_METHODS else 'platform.settings'
        return granted(request.user, permission)[0]

    def has_object_permission(self, request, view, obj):
        return self.has_permission(request, view)


READ_OR_WRITE = ReadOrWrite()


def _refuse(exc):
    return exc.response()


def _outcome_response(outcome, applied=None):
    row = outcome.request
    body = {'approval_request': row.as_dict(), 'duplicate': outcome.duplicate}
    if row.status == REQUESTED:
        body['state'] = 'pending_approval'
        return success_response(body, status.HTTP_202_ACCEPTED)
    body['state'] = row.status
    body['result'] = row.result
    return success_response(body, status.HTTP_200_OK)


def _pending(action_key):
    rows = ApprovalRequest.objects.filter(action_key=action_key, status=REQUESTED).order_by('-id')[:50]
    out = []
    for r in rows:
        p = r.shown_payload() or {}
        p.pop('value', None)
        out.append({'id': r.id, 'target': r.target_id, 'payload': p, 'reason': r.request_reason,
                    'maker_label': r.maker_label, 'maker_id': r.maker_id,
                    'requested_at': r.requested_at.isoformat() if r.requested_at else None,
                    'expires_at': r.expires_at.isoformat() if r.expires_at else None})
    return out


def _body(request):
    data = request.data
    return data if hasattr(data, 'get') else {}


def _city_in_scope(request, code):
    node = (markets._covered(request.user, GeoNode.objects.filter(kind=CITY))
            .filter(code=str(code or '').upper()).select_related('parent__parent').first())
    return node


def _not_found(what='City'):
    return error_response(code='NOT_FOUND', message=f'{what} not found', field='id',
                          issue=f'No {what.lower()} matches', status=status.HTTP_404_NOT_FOUND)


# ---------------------------------------------------------------------------
# Public app-config
# ---------------------------------------------------------------------------

class AppConfigThrottle(SimpleRateThrottle):
    scope = 'app_config'

    def get_cache_key(self, request, view):
        xff = request.META.get('HTTP_X_FORWARDED_FOR')
        ident = xff.split(',')[0].strip() if xff else request.META.get('REMOTE_ADDR', '')
        return self.cache_format % {'scope': self.scope, 'ident': ident}


@api_view(['GET'])
@permission_classes([AllowAny])
@throttle_classes([AppConfigThrottle])
def app_config(request):
    try:
        body = app_config_module.build(
            country=request.query_params.get('country'), city=request.query_params.get('city'),
            app=request.query_params.get('app'))
    except PlatformError as exc:
        return _refuse(exc)
    response = success_response(body, status.HTTP_200_OK)
    response['Cache-Control'] = 'public, max-age=60'
    return response


# ---------------------------------------------------------------------------
# Markets
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([IsAuthenticated, READ])
def markets_list(request):
    return success_response({
        'countries': markets.overview(request.user),
        'pending': [markets.request_dict(r) for r in markets.pending_requests().values()],
    }, status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated, READ])
def city_readiness(request, city_code):
    node = _city_in_scope(request, city_code)
    if node is None:
        return _not_found()
    return success_response(
        markets.city_row(node, request.user, markets.pending_requests(), with_items=True),
        status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, STATUS_CAP])
def city_status(request, city_code):
    node = _city_in_scope(request, city_code)
    if node is None:
        return _not_found()
    body = _body(request)
    try:
        outcome = markets.propose_status(
            request.user, node, body.get('status'), body.get('reason'), http_request=request)
    except approvals.ApprovalError as exc:
        return _refuse(exc)
    return _outcome_response(outcome)


@api_view(['POST'])
@permission_classes([IsAuthenticated, WRITE])
def node_add(request):
    body = _body(request)
    try:
        outcome = markets.propose_add_node(
            request.user, kind=body.get('kind'), code=body.get('code'), name=body.get('name'),
            parent_code=body.get('parent_code'), reason=body.get('reason'), http_request=request)
    except approvals.ApprovalError as exc:
        return _refuse(exc)
    return _outcome_response(outcome)


# ---------------------------------------------------------------------------
# Feature flags
# ---------------------------------------------------------------------------

@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated, READ_OR_WRITE])
def flags_collection(request):
    if request.method == 'POST':
        body = _body(request)
        try:
            flag = flags.create_flag(
                request.user, key=body.get('key'), description=body.get('description'),
                default_enabled=bool(body.get('default_enabled', False)),
                owner_area=body.get('owner_area') or 'platform',
                money_affecting=bool(body.get('money_affecting')), safety=bool(body.get('safety')),
                reason=body.get('reason') or '', http_request=request)
        except PlatformError as exc:
            return _refuse(exc)
        return success_response(flags.flag_dict(flag), status.HTTP_201_CREATED)
    rows = FeatureFlag.objects.prefetch_related('overrides__node').order_by('key')
    return success_response({
        'results': [flags.flag_dict(f) for f in rows],
        'pending': _pending(PLATFORM_FLAG_CHANGE),
        'note': 'No flag is seeded: nothing in the platform reads a flag yet. A flag is created '
                'by the change that adds its reader.',
    }, status.HTTP_200_OK)


@api_view(['GET', 'PUT'])
@permission_classes([IsAuthenticated, READ_OR_WRITE])
def flag_item(request, key):
    flag = FeatureFlag.objects.filter(key=key).prefetch_related('overrides__node').first()
    if flag is None:
        return _not_found('Flag')
    if request.method == 'GET':
        return success_response({**flags.flag_dict(flag), 'history': flags.history(flag)},
                                status.HTTP_200_OK)
    body = _body(request)
    try:
        flag, outcome = flags.propose_change(
            request.user, key, default_enabled=body.get('default_enabled'),
            overrides=body.get('overrides') if isinstance(body.get('overrides'), dict) else None,
            reason=body.get('reason'), http_request=request)
    except approvals.ApprovalError as exc:
        return _refuse(exc)
    if outcome is not None:
        return _outcome_response(outcome)
    flag.refresh_from_db()
    return success_response({**flags.flag_dict(flag), 'state': 'applied'}, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# App versions
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([IsAuthenticated, READ])
def versions_list(request):
    return success_response({'results': versions.table(),
                             'pending': _pending(PLATFORM_APP_VERSION)}, status.HTTP_200_OK)


@api_view(['PUT'])
@permission_classes([IsAuthenticated, WRITE])
def version_update(request, app, platform):
    body = _body(request)
    try:
        row, outcome = versions.propose_change(
            request.user, app=app, platform=platform,
            minimum_version=body.get('minimum_version'), latest_version=body.get('latest_version'),
            store_url=body.get('store_url'), message=body.get('message'),
            reason=body.get('reason'), http_request=request)
    except approvals.ApprovalError as exc:
        return _refuse(exc)
    if outcome is not None:
        return _outcome_response(outcome)
    return success_response({**versions.policy_dict(row), 'state': 'applied'}, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Notices
# ---------------------------------------------------------------------------

def _when(value):
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        raise PlatformError('Use an ISO date-time.', code='INVALID_WINDOW', field='starts_at')
    return moment if timezone.is_aware(moment) else timezone.make_aware(moment)


@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated, READ_OR_WRITE])
def notices_collection(request):
    if request.method == 'POST':
        body = _body(request)
        try:
            row = notices.create(
                request.user, scope=body.get('scope') or None, audience=body.get('audience'),
                starts_at=_when(body.get('starts_at')), ends_at=_when(body.get('ends_at')),
                message=body.get('message'), severity=body.get('severity') or 'info',
                reason=body.get('reason') or '', http_request=request)
        except PlatformError as exc:
            return _refuse(exc)
        return success_response(notices.notice_dict(row), status.HTTP_201_CREATED)
    rows = MaintenanceNotice.objects.select_related('scope_node').order_by('-starts_at', '-id')[:200]
    return success_response({'results': [notices.notice_dict(n) for n in rows]},
                            status.HTTP_200_OK)


@api_view(['PUT'])
@permission_classes([IsAuthenticated, WRITE])
def notice_update(request, notice_id):
    row = MaintenanceNotice.objects.select_related('scope_node').filter(pk=notice_id).first()
    if row is None:
        return _not_found('Notice')
    body = _body(request)
    try:
        row = notices.update(
            request.user, row, starts_at=_when(body.get('starts_at')),
            ends_at=_when(body.get('ends_at')), message=body.get('message'),
            severity=body.get('severity'), reason=body.get('reason') or '', http_request=request)
    except PlatformError as exc:
        return _refuse(exc)
    return success_response(notices.notice_dict(row), status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, WRITE])
def notice_cancel(request, notice_id):
    row = MaintenanceNotice.objects.select_related('scope_node').filter(pk=notice_id).first()
    if row is None:
        return _not_found('Notice')
    try:
        row = notices.cancel(request.user, row, reason=_body(request).get('reason') or '',
                             http_request=request)
    except PlatformError as exc:
        return _refuse(exc)
    return success_response(notices.notice_dict(row), status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Platform settings
# ---------------------------------------------------------------------------

@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated, WRITE])
def settings_collection(request):
    if request.method == 'GET':
        return success_response({
            'results': settings_editor.list_rows(),
            'pending': _pending(PLATFORM_SETTING_CHANGE),
            'types': list(settings_editor.TYPES),
            'note': 'No key is read by the platform yet (the one reader has no caller), so '
                    'changing a value changes nothing today.',
        }, status.HTTP_200_OK)
    return _setting_write(request, _body(request).get('key'))


@api_view(['PUT'])
@permission_classes([IsAuthenticated, WRITE])
def setting_update(request, key):
    return _setting_write(request, key)


def _setting_write(request, key):
    body = _body(request)
    try:
        row, outcome = settings_editor.propose_change(
            request.user, key=key, value=body.get('value'), setting_type=body.get('setting_type'),
            description=body.get('description'),
            is_sensitive=body.get('is_sensitive') if 'is_sensitive' in body else None,
            reason=body.get('reason'), http_request=request)
    except approvals.ApprovalError as exc:
        return _refuse(exc)
    if outcome is not None:
        return _outcome_response(outcome)
    row = PlatformSettings.objects.select_related('updated_by').get(pk=row.pk)
    return success_response({**settings_editor.row_dict(row), 'state': 'applied'},
                            status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Integrations and options
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([IsAuthenticated, INTEGRATIONS])
def integrations_status(request):
    return success_response(integrations.status_page(), status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated, READ])
def options(request):
    return success_response({
        'owner_areas': [{'key': k, 'label': v} for k, v in OWNER_AREAS],
        'apps': [{'key': k, 'label': v} for k, v in APP_CHOICES],
        'platforms': [{'key': k, 'label': v} for k, v in PLATFORM_CHOICES],
        'audiences': [{'key': k, 'label': v} for k, v in AUDIENCE_CHOICES],
        'severities': [{'key': k, 'label': v} for k, v in SEVERITY_CHOICES],
        'setting_types': list(settings_editor.TYPES),
        'notice_message_max': 280,
    }, status.HTTP_200_OK)
