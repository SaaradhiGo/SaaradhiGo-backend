"""The Notifications console API (module 11).

  GET   /notifications/admin/templates/                   notify.read      list (filters; scoped to country)
  POST  /notifications/admin/templates/                   notify.template  new draft / new version
  GET   /notifications/admin/templates/<id>/              notify.read      detail, versions, registrations
  PATCH /notifications/admin/templates/<id>/              notify.template  edit a draft
  PUT   /notifications/admin/templates/<id>/registrations/ notify.template  DLT / sender / WhatsApp rows (draft)
  POST  /notifications/admin/templates/<id>/preview/      notify.read      render with sample values + refusals
  POST  /notifications/admin/templates/preview/           notify.read      the same for unsaved editor state
  POST  /notifications/admin/templates/<id>/submit/       notify.template  validate + raise the request (202)
  POST  /notifications/admin/templates/<id>/archive/      notify.template  retire request (202)
  GET   /notifications/admin/campaigns/                   notify.read
  POST  /notifications/admin/campaigns/                   notify.campaign  new draft
  GET   /notifications/admin/campaigns/<id>/              notify.read
  PATCH /notifications/admin/campaigns/<id>/              notify.campaign  edit a draft
  POST  /notifications/admin/campaigns/audience-preview/  notify.campaign  the audience funnel
  POST  /notifications/admin/campaigns/<id>/submit/       notify.campaign  raise the send request (202)
  POST  /notifications/admin/campaigns/<id>/cancel/       notify.campaign
  GET   /notifications/admin/cities/                      notify.campaign  cities a campaign may target
  GET   /notifications/admin/messages/                    notify.read      delivery log (scoped by recipient city)
  GET   /notifications/admin/messages/<id>/               notify.read      one message and its status timeline
  GET   /notifications/admin/providers/                   notify.read      provider state; NO secrets
  GET   /notifications/admin/users/<id>/preferences/      notify.read
  PUT   /notifications/admin/users/<id>/preferences/      notify.campaign  opt a user OUT (never in)

All under `admin/` so the structural guard and the PII sweep find them by path. An object outside
the caller's scope is a 404, identical to one that does not exist; in scope without the
capability is a 403. Recipients are masked everywhere (`recipients.masked_for`), and no response
carries a raw phone, email or token.
"""

import logging

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import SAFE_METHODS, IsAuthenticated
from rest_framework.views import APIView

from base.countries import COUNTRIES
from base.utils import error_response, success_response
from servers.admin_access.bridge import ScopedPermission, granted
from servers.admin_access.models import CITY, GeoNode
from servers.admin_access.scoping import ScopeNotHeld, scoped, scoped_or_none, scoped_to_request
from servers.admin_audit.services import record_admin_action
from servers.approvals.service import ApprovalError

from . import campaign_workflow, config, health, preferences, recipients, template_workflow
from .models import (
    CATEGORIES, CHANNELS, Campaign, NotificationMessage, NotificationTemplate,
)

logger = logging.getLogger(__name__)

READ = ScopedPermission('notify.read')
TEMPLATE = ScopedPermission('notify.template')
CAMPAIGN = ScopedPermission('notify.campaign')

DEFAULT_LIMIT, MAX_LIMIT = 50, 100


def _not_found(what='record'):
    return error_response(
        code='NOT_FOUND', message=f'No such {what}', field='id', issue=f'No such {what}',
        status=status.HTTP_404_NOT_FOUND)


def _narrowed(request, queryset):
    """The caller's scope, narrowed by `?scope=<code>` (the console's switcher) when sent."""
    return scoped_to_request(request, queryset)


def _scope_refused(exc):
    return error_response(
        code='SCOPE_NOT_HELD', message=str(exc), field='scope', issue=str(exc),
        status=status.HTTP_403_FORBIDDEN)


def _page(request):
    try:
        limit = max(1, min(int(request.query_params.get('limit', DEFAULT_LIMIT)), MAX_LIMIT))
        offset = max(0, int(request.query_params.get('offset', 0)))
    except (TypeError, ValueError):
        limit, offset = DEFAULT_LIMIT, 0
    return limit, offset


def _pending(outcome):
    row = outcome.request
    return success_response(
        {'state': 'pending_approval' if row.status == 'requested' else row.status,
         'approval_request': row.as_dict(), 'duplicate': outcome.duplicate},
        status_code=status.HTTP_202_ACCEPTED)


def _body(request):
    data = request.data
    if hasattr(data, 'dict'):          # a form post
        return data.dict()
    return dict(data) if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Messages: the delivery log
# ---------------------------------------------------------------------------

def message_dict(m, *, timeline=False):
    out = {
        'id': m.id, 'template_key': m.template_key, 'template_version': m.template_version,
        'channel': m.channel, 'category': m.category, 'country': m.country,
        'city': {'code': m.city.code, 'name': m.city.name} if m.city_id else None,
        'recipient_user_id': m.recipient_user_id, 'recipient': m.recipient_masked,
        'provider': m.provider, 'provider_message_id': m.provider_message_id,
        'status': m.status, 'suppression_reason': m.suppression_reason,
        'attempts': m.attempts, 'last_error': m.last_error, 'source': m.source,
        'campaign_id': m.campaign_id, 'subject': m.subject, 'title': m.title, 'body': m.body,
        'body_hash': m.body_hash,
        'not_before': m.not_before.isoformat() if m.not_before else None,
        'created_at': m.created_at.isoformat(),
        'sent_at': m.sent_at.isoformat() if m.sent_at else None,
    }
    if timeline:
        out['timeline'] = [
            {'status': e.status, 'at': e.at.isoformat(), 'source': e.source, 'detail': e.detail,
             'provider': e.provider, 'provider_message_id': e.provider_message_id}
            for e in m.events.all()]
    return out


@api_view(['GET'])
@permission_classes([IsAuthenticated, READ])
def message_list(request):
    try:
        qs = _narrowed(request, NotificationMessage.objects.select_related('city'))
    except ScopeNotHeld as exc:
        return _scope_refused(exc)
    params = request.query_params
    for field, column in (('status', 'status'), ('channel', 'channel'), ('provider', 'provider'),
                          ('template', 'template_key'), ('category', 'category'),
                          ('country', 'country')):
        value = (params.get(field) or '').strip()
        if value:
            qs = qs.filter(**{column: value})
    user_id = (params.get('user_id') or '').strip()
    if user_id:
        if not user_id.isdigit():
            return error_response(
                code='INVALID_FILTER', message='user_id must be a number', field='user_id',
                issue='Not a number', status=status.HTTP_400_BAD_REQUEST)
        qs = qs.filter(recipient_user_id=int(user_id))
    campaign = (params.get('campaign') or '').strip()
    if campaign.isdigit():
        qs = qs.filter(campaign_id=int(campaign))
    limit, offset = _page(request)
    total = qs.count()
    rows = qs.order_by('-created_at', '-id')[offset:offset + limit]
    return success_response(
        {'count': total, 'limit': limit, 'offset': offset,
         'results': [message_dict(m) for m in rows]}, status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated, READ])
def message_detail(request, message_id):
    message = scoped_or_none(
        request.user, NotificationMessage.objects.select_related('city')
        .prefetch_related('events'), pk=message_id)
    if message is None:
        return _not_found('message')
    return success_response(message_dict(message, timeline=True), status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def _template_detail(t):
    siblings = NotificationTemplate.objects.filter(
        key=t.key, channel=t.channel, country=t.country, locale=t.locale).order_by('-version')
    out = template_workflow.template_dict(t)
    out['versions'] = [{'id': v.id, 'version': v.version, 'status': v.status,
                        'created_at': v.created_at.isoformat(),
                        'activated_at': v.activated_at.isoformat() if v.activated_at else None}
                       for v in siblings]
    out['activation_problems'] = template_workflow.activation_problems(t) if t.status in (
        'draft', 'pending_approval') else []
    return out


class TemplateCollection(APIView):
    http_method_names = ['get', 'post']

    def get_permissions(self):
        gate = READ if self.request.method in SAFE_METHODS else TEMPLATE
        return [IsAuthenticated(), gate]

    def get(self, request):
        try:
            qs = _narrowed(request, NotificationTemplate.objects.select_related('geo_node'))
        except ScopeNotHeld as exc:
            return _scope_refused(exc)
        params = request.query_params
        for field in ('key', 'channel', 'country', 'status', 'category'):
            value = (params.get(field) or '').strip()
            if value:
                qs = qs.filter(**{field: value})
        q = (params.get('q') or '').strip()
        if q:
            qs = qs.filter(key__icontains=q)
        limit, offset = _page(request)
        total = qs.count()
        rows = qs.order_by('key', 'channel', 'country', '-version')[offset:offset + limit]
        return success_response(
            {'count': total, 'limit': limit, 'offset': offset,
             'results': [template_workflow.template_dict(t, with_registrations=False)
                         for t in rows]}, status.HTTP_200_OK)

    def post(self, request):
        data = _body(request)
        source_id = data.get('from_template_id')
        if source_id:
            source = template_workflow.lookup(request.user, source_id)
            if source is None:
                return _not_found('template')
            base = {'key': source.key, 'channel': source.channel, 'country': source.country,
                    'locale': source.locale, 'category': source.category,
                    'subject': source.subject, 'title': source.title, 'body': source.body,
                    'variables': source.variables,
                    'personal_variables': source.personal_variables,
                    'sample_values': source.sample_values}
            data = {**base, **{k: v for k, v in data.items() if k != 'from_template_id'}}
        try:
            template = template_workflow.create_draft(request.user, data, http_request=request)
        except ApprovalError as exc:
            return exc.response()
        return success_response(_template_detail(template), status.HTTP_201_CREATED)


class TemplateItem(APIView):
    http_method_names = ['get', 'patch']

    def get_permissions(self):
        gate = READ if self.request.method in SAFE_METHODS else TEMPLATE
        return [IsAuthenticated(), gate]

    def get(self, request, template_id):
        template = template_workflow.lookup(request.user, template_id)
        if template is None:
            return _not_found('template')
        return success_response(_template_detail(template), status.HTTP_200_OK)

    def patch(self, request, template_id):
        template = template_workflow.lookup(request.user, template_id)
        if template is None:
            return _not_found('template')
        try:
            template = template_workflow.update_draft(
                request.user, template, _body(request), http_request=request)
        except ApprovalError as exc:
            return exc.response()
        return success_response(_template_detail(template), status.HTTP_200_OK)


@api_view(['PUT'])
@permission_classes([IsAuthenticated, TEMPLATE])
def template_registrations(request, template_id):
    template = template_workflow.lookup(request.user, template_id)
    if template is None:
        return _not_found('template')
    rows = _body(request).get('registrations')
    try:
        template = template_workflow.set_registrations(
            request.user, template, rows, http_request=request)
    except ApprovalError as exc:
        return exc.response()
    return success_response(_template_detail(template), status.HTTP_200_OK)


class _Draft:
    """The unsaved editor state, shaped like a template for `preview` and the rules."""

    def __init__(self, data):
        self.body = str(data.get('body') or '')
        self.subject = str(data.get('subject') or '')
        self.title = str(data.get('title') or '')
        self.variables = list(data.get('variables') or [])
        self.personal_variables = list(data.get('personal_variables') or [])
        self.sample_values = dict(data.get('sample_values') or {})
        self.channel = data.get('channel') or ''
        self.country = str(data.get('country') or '').upper()


def _preview_response(draft, values, registrations):
    out = template_workflow.preview(draft, values)
    out['registration_problems'] = template_workflow.registration_problems(
        draft.channel, draft.country, draft.body, registrations)
    return success_response(out, status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, READ])
def template_preview_adhoc(request):
    data = _body(request)
    draft = _Draft(data)
    return _preview_response(draft, data.get('values') or {}, data.get('registrations') or [])


@api_view(['POST'])
@permission_classes([IsAuthenticated, READ])
def template_preview(request, template_id):
    template = template_workflow.lookup(request.user, template_id)
    if template is None:
        return _not_found('template')
    values = _body(request).get('values')
    registrations = [template_workflow.registration_dict(r) for r in template.registrations.all()]
    return _preview_response(template, values or {}, registrations)


@api_view(['POST'])
@permission_classes([IsAuthenticated, TEMPLATE])
def template_submit(request, template_id):
    template = template_workflow.lookup(request.user, template_id)
    if template is None:
        return _not_found('template')
    try:
        return _pending(template_workflow.propose_activation(
            request.user, template, reason=str(_body(request).get('reason') or ''),
            http_request=request))
    except ApprovalError as exc:
        return exc.response()


@api_view(['POST'])
@permission_classes([IsAuthenticated, TEMPLATE])
def template_archive(request, template_id):
    template = template_workflow.lookup(request.user, template_id)
    if template is None:
        return _not_found('template')
    try:
        return _pending(template_workflow.propose_archive(
            request.user, template, reason=str(_body(request).get('reason') or ''),
            http_request=request))
    except ApprovalError as exc:
        return exc.response()


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------

class CampaignCollection(APIView):
    http_method_names = ['get', 'post']

    def get_permissions(self):
        gate = READ if self.request.method in SAFE_METHODS else CAMPAIGN
        return [IsAuthenticated(), gate]

    def get(self, request):
        try:
            qs = _narrowed(request, campaign_workflow.visible(request.user))
        except ScopeNotHeld as exc:
            return _scope_refused(exc)
        wanted = (request.query_params.get('status') or '').strip()
        if wanted:
            qs = qs.filter(status=wanted)
        limit, offset = _page(request)
        total = qs.count()
        rows = qs.order_by('-created_at', '-id')[offset:offset + limit]
        return success_response(
            {'count': total, 'limit': limit, 'offset': offset,
             'results': [campaign_workflow.campaign_dict(c) for c in rows]}, status.HTTP_200_OK)

    def post(self, request):
        try:
            campaign = campaign_workflow.create_draft(
                request.user, _body(request), http_request=request)
        except ApprovalError as exc:
            return exc.response()
        return success_response(campaign_workflow.campaign_dict(campaign),
                                status.HTTP_201_CREATED)


def _campaign(request, campaign_id):
    return scoped_or_none(
        request.user, Campaign.objects.select_related('template', 'geo_node'), pk=campaign_id)


class CampaignItem(APIView):
    http_method_names = ['get', 'patch']

    def get_permissions(self):
        gate = READ if self.request.method in SAFE_METHODS else CAMPAIGN
        return [IsAuthenticated(), gate]

    def get(self, request, campaign_id):
        campaign = _campaign(request, campaign_id)
        if campaign is None:
            return _not_found('campaign')
        out = campaign_workflow.campaign_dict(campaign)
        out['funnel'] = campaign_workflow.preview(campaign.audience, campaign.template)
        from collections import Counter
        out['message_counts'] = dict(Counter(
            campaign.messages.values_list('status', flat=True)))
        return success_response(out, status.HTTP_200_OK)

    def patch(self, request, campaign_id):
        campaign = _campaign(request, campaign_id)
        if campaign is None:
            return _not_found('campaign')
        try:
            campaign = campaign_workflow.update_draft(
                request.user, campaign, _body(request), http_request=request)
        except ApprovalError as exc:
            return exc.response()
        return success_response(campaign_workflow.campaign_dict(campaign), status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CAMPAIGN])
def campaign_audience_preview(request):
    data = _body(request)
    try:
        template = campaign_workflow._template_for(request.user, data.get('template_id'))
        audience, _nodes, _anchor = campaign_workflow.clean_audience(
            request.user, data.get('audience'))
    except ApprovalError as exc:
        return exc.response()
    return success_response(
        {'audience': audience, 'funnel': campaign_workflow.preview(audience, template)},
        status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated, CAMPAIGN])
def campaign_submit(request, campaign_id):
    campaign = _campaign(request, campaign_id)
    if campaign is None:
        return _not_found('campaign')
    try:
        return _pending(campaign_workflow.propose_send(
            request.user, campaign, reason=str(_body(request).get('reason') or ''),
            http_request=request))
    except ApprovalError as exc:
        return exc.response()


@api_view(['POST'])
@permission_classes([IsAuthenticated, CAMPAIGN])
def campaign_cancel(request, campaign_id):
    campaign = _campaign(request, campaign_id)
    if campaign is None:
        return _not_found('campaign')
    try:
        campaign = campaign_workflow.cancel(
            request.user, campaign, reason=str(_body(request).get('reason') or ''),
            http_request=request)
    except ApprovalError as exc:
        return exc.response()
    return success_response(campaign_workflow.campaign_dict(campaign), status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated, CAMPAIGN])
def campaign_cities(request):
    nodes = scoped(request.user, GeoNode.objects.filter(kind=CITY).select_related('parent'))
    rows = [{'code': n.code, 'name': n.name, 'country': n.country.code if n.country else ''}
            for n in nodes.order_by('code') if granted(request.user, 'notify.campaign', n)[0]]
    return success_response({'cities': rows}, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

@api_view(['GET'])
@permission_classes([IsAuthenticated, READ])
def provider_state(request):
    """Per channel x country: the configured provider, live vs stub, whether the credentials
    are present (yes or no, never the values) and the allowlist size."""
    rates = {(r['provider'], r['channel']): r for r in health.measure()}
    rows = []
    for channel in CHANNELS:
        for country in sorted(COUNTRIES):
            configured = config.configured_provider(channel, country)
            if configured is None:
                continue
            effective, live = config.effective_provider(channel, country)
            measured = rates.get((configured, channel))
            rows.append({
                'channel': channel, 'country': country, 'provider': configured,
                'mode': 'live' if live else 'stub', 'effective_provider': effective,
                'internal': configured in config.INTERNAL_PROVIDERS,
                'credentials_present': config.credentials_present(configured),
                'credential_names': list(config.CREDENTIALS.get(configured, ())),
                'recent_total': measured['total'] if measured else 0,
                'recent_failed': measured['failed'] if measured else 0,
            })
    return success_response({
        'live_send': config.live_send_enabled(),
        'production': config.is_production(),
        'allowlist_size': len(config.test_recipients()),
        'allowlist_applies': config.live_send_enabled() and not config.is_production(),
        'quiet_hours': {c: list(config.quiet_hours(c)) for c in sorted(COUNTRIES)},
        'providers': rows,
    }, status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# A user's preferences, for support and marketing
# ---------------------------------------------------------------------------

def _user_visible(admin, user):
    node = recipients.city_of(user)
    if node is None:
        from servers.rider.scope import unrestricted
        return unrestricted(admin)
    return scoped(admin, GeoNode.objects.filter(pk=node.pk)).exists()


class UserPreferences(APIView):
    http_method_names = ['get', 'put']

    def get_permissions(self):
        gate = READ if self.request.method in SAFE_METHODS else CAMPAIGN
        return [IsAuthenticated(), gate]

    def _user(self, request, user_id):
        user = get_user_model().objects.filter(pk=user_id, role__in=('rider', 'driver')).first()
        if user is None or not _user_visible(request.user, user):
            return None
        return user

    @staticmethod
    def _payload(user):
        from .models import NotificationPreference
        rows = NotificationPreference.objects.filter(user=user).order_by('channel', 'category')
        return {'user_id': user.pk, 'effective': preferences.effective(user.pk),
                'rows': [{'channel': r.channel, 'category': r.category,
                          'opted_in': r.opted_in, 'source': r.source,
                          'updated_at': r.updated_at.isoformat()} for r in rows]}

    def get(self, request, user_id):
        user = self._user(request, user_id)
        if user is None:
            return _not_found('user')
        return success_response(self._payload(user), status.HTTP_200_OK)

    def put(self, request, user_id):
        user = self._user(request, user_id)
        if user is None:
            return _not_found('user')
        data = _body(request)
        channel, category = data.get('channel'), data.get('category')
        if channel not in CHANNELS or category not in CATEGORIES:
            return error_response(
                code='INVALID_FIELD', message='channel and category are required',
                field='channel', issue='Unknown channel or category',
                status=status.HTTP_400_BAD_REQUEST)
        if data.get('opted_in') is not False:
            return error_response(
                code='OPT_IN_NOT_ALLOWED',
                message='An admin can only opt a user out. Consent has to come from the person.',
                field='opted_in', issue='Only opted_in=false is accepted',
                status=status.HTTP_400_BAD_REQUEST)
        reason = str(data.get('reason') or '').strip()
        if not reason:
            return error_response(
                code='REASON_REQUIRED', message='A reason is required', field='reason',
                issue='Say why', status=status.HTTP_400_BAD_REQUEST)
        before = preferences.effective(user.pk)[channel][category]
        try:
            _row, changed = preferences.set_preference(
                user, channel, category, False, source='admin')
        except ValueError as exc:
            return error_response(
                code='NOT_ALLOWED', message=str(exc), field='channel', issue=str(exc),
                status=status.HTTP_400_BAD_REQUEST)
        record_admin_action(
            request, action='notification_preference_changed_by_admin',
            target_type='user_notification_preferences', target_id=user.pk,
            before={'channel': channel, 'category': category, 'opted_in': before},
            after={'channel': channel, 'category': category, 'opted_in': False,
                   'changed': changed},
            reason=reason)
        return success_response(self._payload(user), status.HTTP_200_OK)
