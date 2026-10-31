"""Admin REST endpoints for managing service zones and rate cards.

Read-only public surface lives in /api/v1/pricing/zones/ (list of
active cities for the rider/driver app to render a "we serve these
areas" page).

The admin surface is capability-checked AND scoped:

  * reads (`GET` list / retrieve / `effective`) need `zone.read`, which
    City Ops Manager, Finance, Analyst and Super Admin hold, and return
    only the zones (and the cards of the zones) under the caller's scope;
  * RATE CARD writes need `pricing.write` and ZONE writes need `zone.write` (Super
    Admin and City Ops). Both go through maker-checker (`pricing.change` since module
    8, `zone.change` since module 9; always a checker). Applied changes are audited
    by their executors.

A zone outside the caller's scope answers 404 from `get_object()`, the
same as one that does not exist.
"""

import logging

from django.db.models import Max
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import SAFE_METHODS, AllowAny, IsAuthenticated
from rest_framework.response import Response

from base.utils import error_response, success_response
from servers.admin_access.bridge import ScopedPermission
from servers.admin_access.scoping import scoped, scoped_or_none
from servers.admin_audit.services import record_admin_action
from servers.approvals import scope as approval_scope
from servers.approvals.models import REQUESTED, ApprovalRequest
from servers.approvals.service import ApprovalError, NotInScope
from servers.admin_access.models import GeoNode
from servers.pricing import fare_card_workflow as fare_cards
from servers.pricing import zone_impact
from servers.pricing import zone_workflow as zone_flow
from servers.pricing.models import RateCard, ServiceZone
from servers.pricing.serializers import RateCardSerializer, ServiceZoneSerializer
from servers.pricing.services import (
    find_zone_for_point,
    get_active_rate_card,
    quote_fare,
)

logger = logging.getLogger(__name__)


class _AuditedPricingMixin:
    """Record every pricing mutation in the admin audit log.

    A pricing change is the only operator action that alters what a rider is
    charged, and it was the one privileged action with no audit trail at all --
    KYC, payouts, driver deletion and DPDP erasure all had one, and SOS
    transitions keep their own immutable `SOSEventUpdate` trail.

    Recorded on create, update and delete, with the serialised before/after so a
    fare dispute can be answered with "the card in force at that moment was this,
    and this person changed it at this time". Reads are not recorded: an operator
    looking at a rate card is not an event, and logging it would bury the changes.

    `record_admin_action` swallows its own failures by design, so an audit problem
    cannot block a pricing correction.
    """

    audit_target_type = 'pricing'

    def _audit(self, request, action, instance, before=None, after=None):
        # Guarded here as well as inside `record_admin_action`.
        #
        # The recorder swallows its own exceptions, so this looks redundant -- but
        # a test that replaced the recorder with a raising stub produced a 500 on
        # the pricing change, which is exactly the outcome the docstring above
        # promises cannot happen. The promise has to be kept by the caller, because
        # the caller is what a future refactor, a failed import or a serialisation
        # error inside this method would break.
        try:
            record_admin_action(
                request,
                action=action,
                target_type=self.audit_target_type,
                target_id=getattr(instance, 'pk', None),
                before=before or {},
                after=after or {},
                reason=(request.data.get('reason', '')
                        if hasattr(request, 'data') and hasattr(request.data, 'get')
                        else ''),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                'pricing audit write failed action=%s target=%s#%s detail=%s',
                action, self.audit_target_type, getattr(instance, 'pk', None), exc,
            )

    def _snapshot(self, instance):
        if instance is None:
            return {}
        try:
            return self.get_serializer(instance).data
        except Exception:      # noqa: BLE001 -- a snapshot must never block the write
            return {'pk': getattr(instance, 'pk', None)}

    def perform_create(self, serializer):
        super().perform_create(serializer)
        self._audit(self.request, f'{self.audit_target_type}_created',
                    serializer.instance, after=self._snapshot(serializer.instance))

    def perform_update(self, serializer):
        before = self._snapshot(serializer.instance)
        super().perform_update(serializer)
        self._audit(self.request, f'{self.audit_target_type}_updated',
                    serializer.instance, before=before,
                    after=self._snapshot(serializer.instance))

    def perform_destroy(self, instance):
        before = self._snapshot(instance)
        pk = instance.pk
        super().perform_destroy(instance)
        instance.pk = pk           # keep the id readable for the audit row
        self._audit(self.request, f'{self.audit_target_type}_deleted',
                    instance, before=before)


#: The two gates on pricing configuration. Reads and writes are different
#: authorities held by different people, so they are different strings.
PRICING_READ = ScopedPermission('zone.read')
PRICING_WRITE = ScopedPermission('pricing.write')


class _ScopedPricingMixin:
    """`zone.read` to look, `pricing.write` to change, and only in scope.

    `permission_classes` names the read gate so a static reader (and the
    structural guard) sees a `ScopedPermission` on the class; `get_permissions`
    swaps in the write gate for anything that is not a safe method. Scope is
    enforced twice, as everywhere: `get_queryset` filters the list (and so the
    detail lookup, which 404s out of scope), and `ScopedPermission.
    has_object_permission` re-checks the capability at the object's node.
    """

    permission_classes = [IsAuthenticated, PRICING_READ]

    def get_permissions(self):
        gate = PRICING_READ if self.request.method in SAFE_METHODS else PRICING_WRITE
        return [IsAuthenticated(), gate]

    def get_queryset(self):
        return scoped(self.request.user, super().get_queryset())


ZONE_WRITE = ScopedPermission('zone.write')


def _zone_pending(outcome):
    row = outcome.request
    return success_response(
        {'state': 'pending_approval' if row.status == 'requested' else row.status,
         'approval_request': row.as_dict(), 'duplicate': outcome.duplicate},
        status_code=status.HTTP_202_ACCEPTED)


def _truthy(value):
    return value in (True, 'true', 'True', 1, '1')


class ServiceZoneViewSet(_ScopedPricingMixin, _AuditedPricingMixin, viewsets.ModelViewSet):
    """Service zones. Reads are direct; every change goes through maker-checker.

    Module 9: creating a zone, changing its polygon or settings, deactivating,
    reactivating and restoring a revision all raise a `zone.change` request (always a
    checker) and answer **202** with the request. URLs are unchanged.

      POST   /zones/                    a new zone                       -> 202
      PATCH  /zones/<id>/               polygon / settings change        -> 202
                                        `is_active` alone                -> (de)activation 202
      DELETE /zones/<id>/               deactivation request (never deleted) -> 202
      POST   /zones/<id>/restore/       {version, reason}                -> 202
      POST   /zones/<id>/deactivate/    {reason}                         -> 202
      POST   /zones/<id>/reactivate/    {reason}                         -> 202
      GET    /zones/<id>/history/       revisions and pending requests   zone.read
      GET    /zones/pending/            pending zone.change requests     zone.read, scoped
      GET    /zones/cities/             city nodes a zone may be drawn in  zone.read, scoped
      POST   /zones/preview/            impact preview of a draft        zone.read
      POST   /zones/point-test/         which zone / cards a point gets  zone.read

    Writes need `zone.write` (City Ops and Super Admin), in the zone's scope: a zone
    outside scope is 404, one in scope without the capability is 403.
    """
    audit_target_type = 'service_zone'
    queryset = ServiceZone.objects.select_related('geo_node', 'parent').order_by(
        '-priority', 'code')
    serializer_class = ServiceZoneSerializer
    lookup_field = 'pk'

    def get_permissions(self):
        read_only_posts = ('preview', 'point_test')
        gate = (PRICING_READ if self.request.method in SAFE_METHODS
                or getattr(self, 'action', None) in read_only_posts else ZONE_WRITE)
        return [IsAuthenticated(), gate]

    def get_queryset(self):
        return super().get_queryset().annotate(_version=Max('revisions__version'))

    def create(self, request, *args, **kwargs):
        data = dict(request.data) if hasattr(request.data, 'keys') else {}
        try:
            return _zone_pending(zone_flow.propose_create(
                request.user, data, reason=str(data.get('reason') or ''), http_request=request))
        except ApprovalError as exc:
            return exc.response()

    def update(self, request, *args, **kwargs):
        zone = self.get_object()
        data = dict(request.data) if hasattr(request.data, 'keys') else {}
        reason = str(data.get('reason') or '')
        status_change = 'is_active' in data
        fields = {k for k in data if k not in ('reason', 'is_active')}
        try:
            if status_change and fields:
                return Response(
                    {'detail': 'Change the zone or activate/deactivate it, not both.'},
                    status=status.HTTP_400_BAD_REQUEST)
            if status_change:
                wanted = _truthy(data['is_active'])
                if wanted == zone.is_active:
                    return Response({'detail': 'The zone is already in that state.'},
                                    status=status.HTTP_409_CONFLICT)
                flow = zone_flow.propose_reactivate if wanted else zone_flow.propose_deactivate
                return _zone_pending(flow(request.user, zone, reason=reason,
                                          http_request=request))
            return _zone_pending(zone_flow.propose_update(
                request.user, zone, {k: data[k] for k in fields}, reason=reason,
                http_request=request))
        except ApprovalError as exc:
            return exc.response()

    def destroy(self, request, *args, **kwargs):
        zone = self.get_object()
        try:
            return _zone_pending(zone_flow.propose_deactivate(
                request.user, zone, reason=str(request.data.get('reason') or ''),
                http_request=request))
        except ApprovalError as exc:
            return exc.response()

    @action(detail=True, methods=['post'], url_path='deactivate')
    def deactivate(self, request, pk=None):
        zone = self.get_object()
        try:
            return _zone_pending(zone_flow.propose_deactivate(
                request.user, zone, reason=str(request.data.get('reason') or ''),
                http_request=request))
        except ApprovalError as exc:
            return exc.response()

    @action(detail=True, methods=['post'], url_path='reactivate')
    def reactivate(self, request, pk=None):
        zone = self.get_object()
        try:
            return _zone_pending(zone_flow.propose_reactivate(
                request.user, zone, reason=str(request.data.get('reason') or ''),
                http_request=request))
        except ApprovalError as exc:
            return exc.response()

    @action(detail=True, methods=['post'], url_path='restore')
    def restore(self, request, pk=None):
        zone = self.get_object()
        try:
            return _zone_pending(zone_flow.propose_restore(
                request.user, zone, request.data.get('version'),
                reason=str(request.data.get('reason') or ''), http_request=request))
        except ApprovalError as exc:
            return exc.response()

    @action(detail=True, methods=['get'], url_path='history')
    def history(self, request, pk=None):
        zone = self.get_object()
        with_polygon = request.query_params.get('polygons') in ('1', 'true')
        revisions = zone.revisions.order_by('-version')[:100]
        pending = ApprovalRequest.objects.filter(
            action_key='zone.change', target_type='service_zone', target_id=str(zone.id),
            status=REQUESTED).order_by('-requested_at')
        return success_response({
            'zone': zone.code,
            'version': zone_flow.current_version(zone),
            'revisions': [r.as_dict(with_polygon=with_polygon) for r in revisions],
            'pending_requests': [r.as_dict() for r in pending[:20]],
        }, status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='pending')
    def pending(self, request):
        rows = approval_scope.visible(request.user, ApprovalRequest.objects.filter(
            action_key='zone.change', status=REQUESTED)).order_by('-requested_at')[:100]
        return success_response({'results': [r.as_dict() for r in rows]}, status.HTTP_200_OK)

    @action(detail=False, methods=['get'], url_path='cities')
    def cities(self, request):
        """The city nodes the caller may draw a zone in (zone.read, scoped)."""
        nodes = scoped(request.user, GeoNode.objects.filter(kind='city')).select_related(
            'parent').order_by('path')
        return success_response({'results': [
            {'code': n.code, 'name': n.name, 'status': n.status,
             'country': n.country.code if n.country is not None else None}
            for n in nodes]}, status.HTTP_200_OK)

    def _draft(self, request):
        """`((zone or None, values, geometry, area), None)` from a preview / tester body,
        or `(None, ApprovalError)`. A zone outside scope is 404; for a NEW zone the city
        node must be one the caller may reach."""
        d = request.data or {}
        zone = None
        try:
            if d.get('zone') not in (None, ''):
                zone = scoped_or_none(request.user, ServiceZone.objects.select_related(
                    'geo_node', 'parent'), pk=d.get('zone'))
                if zone is None:
                    raise NotInScope('No such zone.', field='zone')
            raw = dict(d.get('values') or {})
            if zone is not None:
                values, geom, area, _ = zone_flow.clean_values(
                    {**raw, 'market_override_reason': 'preview'}, zone_flow.zone_values(zone))
                values['is_active'] = _truthy(d.get('is_active', zone.is_active))
                zone_flow.check_world(values, geom, zone)
            else:
                node = scoped_or_none(request.user, GeoNode.objects.all(),
                                      code=str(raw.get('geo_node') or ''))
                if node is None or node.kind != 'city':
                    raise NotInScope('No such city.', field='geo_node')
                values, geom, area, _ = zone_flow.clean_values(
                    {**raw, 'market_override_reason': 'preview'}, None)
                zone_flow.check_world(values, geom)
        except ApprovalError as exc:
            return None, exc
        return (zone, values, geom, area), None

    @action(detail=False, methods=['post'], url_path='preview')
    def preview(self, request):
        """Impact preview of a draft. A draft that fails validation answers 200 with
        `valid: false` and the error, so the editor can show it as the maker draws."""
        drafted, refusal = self._draft(request)
        if refusal is not None:
            if refusal.status == status.HTTP_404_NOT_FOUND:
                return refusal.response()
            return success_response({'valid': False, 'error': {
                'code': refusal.code, 'message': refusal.message, 'field': refusal.field}},
                status.HTTP_200_OK)
        zone, values, geom, _area = drafted
        return success_response({'valid': True, **zone_impact.impact(zone, values, geom)},
                                status.HTTP_200_OK)

    @action(detail=False, methods=['post'], url_path='point-test')
    def point_test(self, request):
        """Which zone and which fare cards a point resolves to, now and (when a draft is
        given) under the draft. Same resolution function quoting uses."""
        d = request.data or {}
        overlay = None
        if d.get('values') or d.get('zone') not in (None, ''):
            drafted, refusal = self._draft(request)
            if refusal is not None:
                return refusal.response()
            zone, values, geom, _area = drafted
            overlay = zone_impact.draft_overlay(zone, values, geom)
        try:
            result = zone_impact.point_test(d.get('lat'), d.get('lng'), overlay=overlay)
        except ValueError as exc:
            return error_response(code='INVALID_POINT', message=str(exc), field='lat',
                                  issue=str(exc), status=status.HTTP_400_BAD_REQUEST)
        return success_response(result, status.HTTP_200_OK)


class RateCardViewSet(_ScopedPricingMixin, _AuditedPricingMixin, viewsets.ModelViewSet):
    """Fare cards. Reads are direct; every change goes through maker-checker.

    Module 8 (AUDIT 27.44): creating a card or a new version, retiring a card and
    changing a surge cap or night multiplier all raise a `pricing.change` request
    (always a checker) and answer **202** with the request. Nothing changes until a
    different holder of `approval.check` + `pricing.write` approves. URLs are
    unchanged.

      POST   /rate-cards/        a new card, or a new version if one is live  -> 202
      PATCH  /rate-cards/<id>/   pricing fields -> a NEW VERSION request       -> 202
                                 `is_active=false` / `effective_to` -> retire  -> 202
                                 `notes` only -> direct, audited               -> 200
      DELETE /rate-cards/<id>/   retire request (a card is never deleted)      -> 202
    """
    audit_target_type = 'rate_card'
    queryset = RateCard.objects.select_related('zone', 'vehicle_type', 'zone__geo_node').all()
    serializer_class = RateCardSerializer

    @staticmethod
    def _pending(outcome):
        row = outcome.request
        return success_response(
            {'state': 'pending_approval' if row.status == 'requested' else row.status,
             'approval_request': row.as_dict(), 'duplicate': outcome.duplicate},
            status_code=status.HTTP_202_ACCEPTED)

    def create(self, request, *args, **kwargs):
        from servers.approvals.service import ApprovalError

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        zone, vt = data['zone'], data['vehicle_type']
        # Scope: the zone must be one the caller can reach (404 otherwise) and
        # pricing.write must be held over it.
        if scoped_or_none(request.user, ServiceZone.objects.all(), pk=zone.pk) is None:
            return Response({'detail': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        base = fare_cards.current_card(zone, vt)
        # What the maker left out comes from the live card, else from the model's own
        # default (night multiplier 1.00, hours 23-5, surge cap 1.50, tax 5%). The four
        # fares have no default and are required for a first card.
        values = {}
        for f in fare_cards.VALUE_FIELDS:
            if f in data:
                values[f] = data[f]
            elif base is not None:
                values[f] = getattr(base, f)
            elif RateCard._meta.get_field(f).has_default():
                values[f] = RateCard._meta.get_field(f).get_default()
        missing = [f for f in fare_cards.VALUE_FIELDS if f not in values]
        if missing:
            return Response({'detail': 'A new card needs: ' + ', '.join(missing) + '.'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            outcome = fare_cards.propose_new_version(
                request.user, zone=zone, vehicle_type=vt, values=values, base_card=base,
                effective_from=data.get('effective_from'), notes=data.get('notes', ''),
                reason=str(request.data.get('reason') or ''), http_request=request)
        except ApprovalError as exc:
            return exc.response()
        return self._pending(outcome)

    def update(self, request, *args, **kwargs):
        from servers.approvals.service import ApprovalError

        card = self.get_object()
        keys = set(request.data.keys())
        pricing = (set(fare_cards.VALUE_FIELDS) | {'commission_percent'}) & keys
        retire = keys & {'is_active', 'effective_to'}
        reason = str(request.data.get('reason') or '')
        if pricing and retire:
            return Response({'detail': 'Change the price or retire the card, not both.'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            if retire:
                if request.data.get('is_active') in (True, 'true', 'True', 1, '1'):
                    return Response(
                        {'detail': 'A retired card cannot be reactivated; create a new version.'},
                        status=status.HTTP_409_CONFLICT)
                return self._pending(fare_cards.propose_retire(
                    request.user, card=card, reason=reason, http_request=request))
            if pricing:
                serializer = self.get_serializer(card, data=request.data, partial=True)
                serializer.is_valid(raise_exception=True)
                merged = fare_cards.card_values(card)
                merged.update({k: v for k, v in serializer.validated_data.items()
                               if k in fare_cards.VALUE_FIELDS})
                return self._pending(fare_cards.propose_new_version(
                    request.user, zone=card.zone, vehicle_type=card.vehicle_type, values=merged,
                    base_card=card, effective_from=serializer.validated_data.get('effective_from'),
                    notes=serializer.validated_data.get('notes', card.notes), reason=reason,
                    http_request=request))
        except ApprovalError as exc:
            return exc.response()
        # Notes only: lifecycle, direct, and audited by the mixin.
        kwargs['partial'] = True
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        from servers.approvals.service import ApprovalError

        card = self.get_object()
        try:
            return self._pending(fare_cards.propose_retire(
                request.user, card=card, reason=str(request.data.get('reason') or ''),
                http_request=request))
        except ApprovalError as exc:
            return exc.response()

    def get_queryset(self):
        qs = super().get_queryset()
        zone = self.request.query_params.get('zone')
        if zone:
            qs = qs.filter(zone_id=zone)
        vt = self.request.query_params.get('vehicle_type')
        if vt:
            qs = qs.filter(vehicle_type_id=vt)
        active = self.request.query_params.get('is_active')
        if active in ('true', '1'):
            qs = qs.filter(is_active=True)
        elif active in ('false', '0'):
            qs = qs.filter(is_active=False)
        return qs.order_by('-effective_from', '-version')

    @action(detail=False, methods=['get'], url_path='effective')
    def effective(self, request):
        """Return the currently-effective card for (zone, vehicle_type)."""
        zone_id = request.query_params.get('zone')
        vt_id = request.query_params.get('vehicle_type')
        if not (zone_id and vt_id):
            return Response(
                {'error': 'zone and vehicle_type query params required.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Through scope: a zone outside the caller's assignments is "not
        # found", the same as one that does not exist.
        zone = scoped_or_none(request.user, ServiceZone.objects.all(), pk=zone_id)
        if zone is None:
            return Response({'error': 'zone not found.'}, status=status.HTTP_404_NOT_FOUND)
        from servers.driver.models import VehicleType
        try:
            vt = VehicleType.objects.get(pk=vt_id)
        except VehicleType.DoesNotExist:
            return Response(
                {'error': 'vehicle_type not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        card = get_active_rate_card(zone, vt)
        if not card:
            return Response(
                {'detail': 'No active rate card for this zone/vehicle_type.'},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(RateCardSerializer(card).data)


@api_view(['GET'])
@permission_classes([AllowAny])
def list_active_zones(request):
    """Public list of active service zones (for "where we operate" page).

    Returns a thin payload -- code, name, city, state -- and a
    GeoJSON polygon so the rider app can shade the area on the map.
    Polygons are publicly visible by design; this is the same info the
    rider sees when they get OUT_OF_SERVICE_AREA.
    """
    zones = ServiceZone.objects.filter(is_active=True).order_by('-priority', 'code')
    payload = [
        {
            'code': z.code,
            'name': z.name,
            'city': z.city,
            'state_code': z.state_code,
            'country': z.country,
            'zone_type': z.zone_type,
            'polygon_geojson': z.polygon_geojson,
            # Resolved, not raw. `currency` and `timezone_name` are blank on a
            # zone that inherits them from its country, and a rider app that
            # read the blank would have no currency at all -- so these are the
            # effective values the server itself prices with.
            'currency': z.currency or z.country_profile.currency,
            'timezone': z.timezone_name or z.country_profile.default_timezone,
            # The market's units and tax convention. A client needs these
            # BEFORE it quotes: to label a distance, and to know whether the
            # tax it is about to be shown is already inside the fare.
            'distance_unit': z.distance_unit or z.country_profile.distance_unit,
            'tax_mode': z.tax_mode or z.country_profile.tax_mode,
            'tax_label': z.tax_label,
        }
        for z in zones
    ]
    return success_response({'zones': payload}, status_code=status.HTTP_200_OK)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def quote(request):
    """Stateless fare quote (no Trip object created).

    Useful for the rider app's "estimate price" UI before they commit
    to a request. Less restrictive than /ride/estimate-fare/ in that
    it does NOT depend on the legacy `distance_km`/`duration_min` from
    the client -- those are computed server-side from the coordinates.
    """
    p_lat = request.data.get('pickup_lat')
    p_lon = request.data.get('pickup_long')
    d_lat = request.data.get('destination_lat')
    d_lon = request.data.get('destination_long')
    vehicle_type = request.data.get('vehicle_type')

    missing = [k for k, v in {
        'pickup_lat': p_lat, 'pickup_long': p_lon,
        'destination_lat': d_lat, 'destination_long': d_lon,
        'vehicle_type': vehicle_type,
    }.items() if v in (None, '')]
    if missing:
        return error_response(
            code='MISSING_FIELDS',
            message=f'Missing required fields: {", ".join(missing)}',
            field='request_body',
            issue='Required for fare quote',
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        p_lat = float(p_lat); p_lon = float(p_lon)
        d_lat = float(d_lat); d_lon = float(d_lon)
    except (TypeError, ValueError):
        return error_response(
            code='INVALID_TYPE', message='Coordinates must be numeric',
            field='coordinates', issue='lat/long must be float',
            status=status.HTTP_400_BAD_REQUEST,
        )

    from servers.ride.utils import validate_distance
    ok, km, mins, msg = validate_distance(None, None, p_lat, p_lon, d_lat, d_lon)
    if not ok:
        return error_response(
            code='DISTANCE_INVALID', message=msg, field='coordinates',
            issue=msg, status=status.HTTP_400_BAD_REQUEST,
        )

    pickup_zone = find_zone_for_point(p_lat, p_lon)
    drop_zone = find_zone_for_point(d_lat, d_lon)
    if not pickup_zone or not drop_zone:
        return error_response(
            code='OUT_OF_SERVICE_AREA',
            message='Pickup or drop is outside our service area.',
            field='pickup / destination',
            issue='No active ServiceZone covers the coordinate',
            status=status.HTTP_400_BAD_REQUEST,
        )

    fare = quote_fare(
        distance_km=km,
        duration_min=mins,
        vehicle_type=vehicle_type,
        pickup_lat=p_lat,
        pickup_lon=p_lon,
        rider_id=getattr(request.user, 'id', None),
    )
    return success_response({
        'fare': {k: (str(v) if hasattr(v, 'as_tuple') else v) for k, v in fare.items()},
        'distance_km': km,
        'duration_min': mins,
        'pickup_zone': pickup_zone.code,
        'drop_zone': drop_zone.code,
    }, status_code=status.HTTP_200_OK)
