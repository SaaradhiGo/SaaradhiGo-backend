"""The Promotions console API (module 12) and the rider / driver app endpoints.

Console (everything under `admin/` is gated, scoped and swept for personal data):

  GET   /promotions/admin/campaigns/                      promo.read   list (kind, state, scope, q)
  POST  /promotions/admin/campaigns/                      promo.write | incentive.write  new draft
  GET   /promotions/admin/campaigns/<id>/                 promo.read   detail + projection
  PATCH /promotions/admin/campaigns/<id>/                 write        edit a draft
  POST  /promotions/admin/campaigns/<id>/submit/          write        raise promotion.launch (202)
  POST  /promotions/admin/campaigns/<id>/pause/           write        no checker, audited
  POST  /promotions/admin/campaigns/<id>/resume/          write        no checker, audited
  POST  /promotions/admin/campaigns/<id>/cancel/          write        no checker, audited
  POST  /promotions/admin/campaigns/<id>/budget-increase/ write        raise promotion.budget_increase (202)
  GET   /promotions/admin/campaigns/<id>/activity/        promo.read   redemptions | progress | referrals
  GET   /promotions/admin/options/                        write        places, zones, vehicle classes

An object outside the caller's scope is a 404 identical to one that does not exist; inside the
scope without the capability is a 403. People appear only as masked references (`rider #123`),
never a name, phone, email or token.

The apps (a signed-in rider or driver):

  POST  /promotions/referrals/attach/     use a referral code (once, before the first trip)
  GET   /promotions/referrals/mine/       my code, the offer, the people I referred
  GET   /promotions/driver/incentives/    live incentives with my progress (drivers)
"""

import logging

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import SAFE_METHODS, IsAuthenticated
from rest_framework.views import APIView

from servers.driver.permissions import IsDriver
from base.countries import get_country
from base.utils import error_response, success_response
from servers.admin_access.bridge import ScopedPermission, granted
from servers.admin_access.models import CITY, COUNTRY, GeoNode
from servers.admin_access.scoping import ScopeNotHeld, scoped, scoped_or_none, scoped_to_request
from servers.approvals.service import ApprovalError

from . import incentives, referrals, workflow
from .models import (
    DRIVER_INCENTIVE, IncentiveProgress, KINDS, REFERRAL, RIDER_PROMO, STATE_CHOICES, Campaign,
    LedgerEntry,
)

logger = logging.getLogger(__name__)

READ = ScopedPermission('promo.read')
WRITE = workflow.PromoWrite()
DEFAULT_LIMIT, MAX_LIMIT = 50, 100
STATES = tuple(s for s, _ in STATE_CHOICES)


def _not_found(what='campaign'):
    return error_response(
        code='NOT_FOUND', message=f'No such {what}', field='id', issue=f'No such {what}',
        status=status.HTTP_404_NOT_FOUND)


def _page(request):
    try:
        limit = max(1, min(int(request.query_params.get('limit', DEFAULT_LIMIT)), MAX_LIMIT))
        offset = max(0, int(request.query_params.get('offset', 0)))
    except (TypeError, ValueError):
        limit, offset = DEFAULT_LIMIT, 0
    return limit, offset


def _body(request):
    data = request.data
    if hasattr(data, 'dict'):
        return data.dict()
    return dict(data) if isinstance(data, dict) else {}


def _pending(outcome):
    row = outcome.request
    return success_response(
        {'state': 'pending_approval' if row.status == 'requested' else row.status,
         'approval_request': row.as_dict(), 'duplicate': outcome.duplicate},
        status_code=status.HTTP_202_ACCEPTED)


def _campaign(request, promotion_id):
    return scoped_or_none(request.user, Campaign.objects.select_related(
        'geo_node', 'promo_code', 'quest'), pk=promotion_id)


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------

class CampaignCollection(APIView):
    http_method_names = ['get', 'post']

    def get_permissions(self):
        gate = READ if self.request.method in SAFE_METHODS else WRITE
        return [IsAuthenticated(), gate]

    def get(self, request):
        try:
            qs = scoped_to_request(request, Campaign.objects.select_related('geo_node'))
        except ScopeNotHeld as exc:
            return error_response(code='SCOPE_NOT_HELD', message=str(exc), field='scope',
                                  issue=str(exc), status=status.HTTP_403_FORBIDDEN)
        params = request.query_params
        kind = (params.get('kind') or '').strip()
        if kind:
            if kind not in KINDS:
                return error_response(code='INVALID_FILTER', message='Unknown kind', field='kind',
                                      issue=kind, status=status.HTTP_400_BAD_REQUEST)
            qs = qs.filter(kind=kind)
        state = (params.get('state') or '').strip()
        if state:
            if state not in STATES:
                return error_response(code='INVALID_FILTER', message='Unknown state',
                                      field='state', issue=state,
                                      status=status.HTTP_400_BAD_REQUEST)
            qs = qs.filter(state=state)
        audience = (params.get('audience') or '').strip()
        if audience in ('rider', 'driver'):
            qs = qs.filter(kind=REFERRAL, config__audience=audience)
        q = (params.get('q') or '').strip()
        if q:
            qs = qs.filter(name__icontains=q)
        limit, offset = _page(request)
        total = qs.count()
        rows = qs.order_by('-created_at', '-id')[offset:offset + limit]
        return success_response(
            {'count': total, 'limit': limit, 'offset': offset,
             'results': [workflow.campaign_dict(c) for c in rows]}, status.HTTP_200_OK)

    def post(self, request):
        try:
            campaign = workflow.create_draft(request.user, _body(request), http_request=request)
        except ApprovalError as exc:
            return exc.response()
        return success_response(workflow.campaign_dict(campaign, detail=True),
                                status.HTTP_201_CREATED)


class CampaignItem(APIView):
    http_method_names = ['get', 'patch']

    def get_permissions(self):
        gate = READ if self.request.method in SAFE_METHODS else WRITE
        return [IsAuthenticated(), gate]

    def get(self, request, promotion_id):
        campaign = _campaign(request, promotion_id)
        if campaign is None:
            return _not_found()
        out = workflow.campaign_dict(campaign, detail=True)
        out['ledger'] = _ledger_totals(campaign)
        return success_response(out, status.HTTP_200_OK)

    def patch(self, request, promotion_id):
        campaign = _campaign(request, promotion_id)
        if campaign is None:
            return _not_found()
        try:
            campaign = workflow.update_draft(request.user, campaign, _body(request),
                                             http_request=request)
        except ApprovalError as exc:
            return exc.response()
        return success_response(workflow.campaign_dict(campaign, detail=True), status.HTTP_200_OK)


def _ledger_totals(campaign):
    from django.db.models import Count, Sum

    rows = (LedgerEntry.objects.filter(campaign=campaign).values('kind')
            .annotate(total=Sum('amount_minor'), n=Count('id')))
    return [{'kind': r['kind'], 'total_minor': r['total'] or 0, 'entries': r['n'],
             'currency': campaign.currency} for r in rows]


def _simple(fn):
    def view(request, promotion_id):
        campaign = _campaign(request, promotion_id)
        if campaign is None:
            return _not_found()
        try:
            result = fn(request.user, campaign, reason=str(_body(request).get('reason') or ''),
                        http_request=request)
        except ApprovalError as exc:
            return exc.response()
        return success_response(workflow.campaign_dict(result, detail=True), status.HTTP_200_OK)
    view.__name__ = fn.__name__
    return api_view(['POST'])(permission_classes([IsAuthenticated, WRITE])(view))


campaign_pause = _simple(workflow.pause)
campaign_resume = _simple(workflow.resume)
campaign_cancel = _simple(workflow.cancel)


@api_view(['POST'])
@permission_classes([IsAuthenticated, WRITE])
def campaign_submit(request, promotion_id):
    campaign = _campaign(request, promotion_id)
    if campaign is None:
        return _not_found()
    try:
        return _pending(workflow.propose_launch(
            request.user, campaign, reason=str(_body(request).get('reason') or ''),
            http_request=request))
    except ApprovalError as exc:
        return exc.response()


@api_view(['POST'])
@permission_classes([IsAuthenticated, WRITE])
def campaign_budget_increase(request, promotion_id):
    campaign = _campaign(request, promotion_id)
    if campaign is None:
        return _not_found()
    data = _body(request)
    try:
        return _pending(workflow.propose_budget_increase(
            request.user, campaign, data.get('budget_minor'),
            reason=str(data.get('reason') or ''), http_request=request))
    except ApprovalError as exc:
        return exc.response()


# ---------------------------------------------------------------------------
# What a campaign did: redemptions, progress, referrals (masked people)
# ---------------------------------------------------------------------------

def _ref(role, user_id):
    return f'{role} #{user_id}'


@api_view(['GET'])
@permission_classes([IsAuthenticated, READ])
def campaign_activity(request, promotion_id):
    campaign = _campaign(request, promotion_id)
    if campaign is None:
        return _not_found()
    limit, offset = _page(request)
    if campaign.kind == RIDER_PROMO:
        from servers.ride.models import PromoRedemption

        qs = PromoRedemption.objects.filter(campaign=campaign).order_by('-created_at', '-id')
        total = qs.count()
        rows = [{'id': r.id, 'who': _ref('rider', r.user_id), 'trip_id': r.trip_id,
                 'discount_minor': r.discount_amount_minor, 'currency': r.currency,
                 'state': r.state, 'created_at': r.created_at.isoformat(),
                 'settled_at': r.settled_at.isoformat() if r.settled_at else None}
                for r in qs[offset:offset + limit]]
        activity = 'redemptions'
    elif campaign.kind == DRIVER_INCENTIVE:
        qs = IncentiveProgress.objects.filter(campaign=campaign).order_by(
            '-trips_counted', 'id')
        total = qs.count()
        rows = [{'id': p.id, 'who': _ref('driver', p.driver.user_id_id),
                 'trips_counted': p.trips_counted, 'status': p.status,
                 'reward_minor': p.reward_minor, 'currency': campaign.currency,
                 'completed_at': p.completed_at.isoformat() if p.completed_at else None}
                for p in qs.select_related('driver')[offset:offset + limit]]
        activity = 'progress'
    else:
        from .models import Referral

        qs = Referral.objects.filter(campaign=campaign).order_by('-created_at', '-id')
        total = qs.count()
        role = (campaign.config or {}).get('audience') or 'rider'
        rows = [{'id': r.id, 'referrer': _ref(role, r.referrer_id),
                 'referee': _ref(role, r.referee_id), 'status': r.status,
                 'trips_completed': r.trips_completed,
                 'required_trips': int((campaign.config or {}).get('required_trips') or 0),
                 'blocked_signals': r.blocked_signals,
                 'referrer_reward_minor': r.referrer_reward_minor,
                 'referee_reward_minor': r.referee_reward_minor, 'currency': campaign.currency,
                 'created_at': r.created_at.isoformat(),
                 'rewarded_at': r.rewarded_at.isoformat() if r.rewarded_at else None,
                 'blocked_at': r.blocked_at.isoformat() if r.blocked_at else None}
                for r in qs[offset:offset + limit]]
        activity = 'referrals'
    return success_response(
        {'activity': activity, 'count': total, 'limit': limit, 'offset': offset,
         'results': rows}, status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated, WRITE])
def options(request):
    """What the create form offers: the places the caller may write in (per kind), zones and
    vehicle classes inside them."""
    from servers.driver.models import VehicleType
    from servers.pricing.models import ServiceZone

    nodes = scoped(request.user, GeoNode.objects.filter(kind__in=(COUNTRY, CITY))
                   .select_related('parent'))
    places, zones = [], []
    for node in nodes.order_by('path'):
        can = [cap for cap in (workflow.PROMO_WRITE, workflow.INCENTIVE_WRITE)
               if granted(request.user, cap, node)[0]]
        if can:
            country = workflow.country_of_node(node)
            places.append({'code': node.code, 'name': node.name, 'kind': node.kind,
                           'country': country, 'currency': get_country(country).currency,
                           'can_write': can})
    place_codes = {p['code'] for p in places}
    for zone in (ServiceZone.objects.filter(is_active=True, geo_node__isnull=False)
                 .select_related('geo_node').order_by('code')):
        if any(str(zone.geo_node.path).startswith(n.path)
               for n in nodes if n.code in place_codes):
            zones.append({'code': zone.code, 'name': zone.name, 'node': zone.geo_node.code,
                          'country': zone.country})
    return success_response(
        {'places': places, 'zones': zones,
         'vehicle_types': sorted(VehicleType.objects.values_list('type', flat=True))},
        status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# The apps
# ---------------------------------------------------------------------------

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def referral_attach(request):
    data = _body(request)
    try:
        referral = referrals.attach(request.user, data.get('code'),
                                    device_id=str(data.get('device_id') or '')[:128])
    except referrals.AttachRefused as exc:
        return error_response(code=exc.code, message=exc.message, field='code', issue=exc.message,
                              status=exc.status)
    cfg = referral.campaign.config or {}
    return success_response({
        'status': referral.status, 'required_trips': int(cfg.get('required_trips') or 0),
        'trips_completed': referral.trips_completed,
        'reward_minor': int(cfg.get('referee_reward_minor') or 0),
        'currency': referral.campaign.currency}, status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def referral_mine(request):
    if request.user.role not in ('rider', 'driver'):
        return error_response(code='NOT_ELIGIBLE', message='Riders and drivers only.',
                              field='user', issue='Not a rider or driver',
                              status=status.HTTP_403_FORBIDDEN)
    return success_response(referrals.mine(request.user), status.HTTP_200_OK)


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsDriver])
def driver_incentives(request):
    return success_response({'incentives': incentives.driver_view(request.user.driver),
                             'tax': 'not configured'}, status.HTTP_200_OK)
