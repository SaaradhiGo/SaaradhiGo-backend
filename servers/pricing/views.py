"""Admin REST endpoints for managing service zones and rate cards.

Read-only public surface lives in /api/v1/pricing/zones/ (list of
active cities for the rider/driver app to render a "we serve these
areas" page). Everything mutating is locked behind IsPlatformAdmin.
"""

import logging

from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from base.utils import error_response, success_response
from servers.admin_audit.services import record_admin_action
from servers.pricing.models import RateCard, ServiceZone
from servers.pricing.permissions import IsPlatformAdmin
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


class ServiceZoneViewSet(_AuditedPricingMixin, viewsets.ModelViewSet):
    audit_target_type = 'service_zone'
    queryset = ServiceZone.objects.all().order_by('-priority', 'code')
    serializer_class = ServiceZoneSerializer
    permission_classes = [IsPlatformAdmin]
    lookup_field = 'pk'


class RateCardViewSet(_AuditedPricingMixin, viewsets.ModelViewSet):
    audit_target_type = 'rate_card'
    queryset = RateCard.objects.select_related('zone', 'vehicle_type').all()
    serializer_class = RateCardSerializer
    permission_classes = [IsPlatformAdmin]

    def update(self, request, *args, **kwargs):
        """Refuse an in-place pricing edit; direct the caller to versioning.

        RateCard.save() would raise ValidationError anyway -- this turns that into
        a clear 409 with the reason, rather than a 500 or an opaque validation
        error. Lifecycle-only edits (is_active, effective_to, notes) still pass
        straight through, because retiring a card must stay possible.
        """
        pricing = set(RateCard.PRICING_FIELDS) & set(request.data.keys())
        if pricing:
            return Response(
                {
                    'detail': (
                        'RateCard pricing is immutable. Create a new version '
                        'instead of editing this one.'
                    ),
                    'immutable_fields': sorted(pricing),
                    'how_to_change_pricing': (
                        'POST a new RateCard for this zone + vehicle type, or use '
                        'the fare form which now supersedes automatically.'
                    ),
                },
                status=status.HTTP_409_CONFLICT,
            )
        return super().update(request, *args, **kwargs)

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
        try:
            zone = ServiceZone.objects.get(pk=zone_id)
        except ServiceZone.DoesNotExist:
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
            'currency': z.currency,
            'timezone': z.timezone_name,
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
