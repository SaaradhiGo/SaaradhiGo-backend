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
from servers.pricing.models import RateCard, ServiceZone
from servers.pricing.permissions import IsPlatformAdmin
from servers.pricing.serializers import RateCardSerializer, ServiceZoneSerializer
from servers.pricing.services import (
    find_zone_for_point,
    get_active_rate_card,
    quote_fare,
)

logger = logging.getLogger(__name__)


class ServiceZoneViewSet(viewsets.ModelViewSet):
    queryset = ServiceZone.objects.all().order_by('-priority', 'code')
    serializer_class = ServiceZoneSerializer
    permission_classes = [IsPlatformAdmin]
    lookup_field = 'pk'


class RateCardViewSet(viewsets.ModelViewSet):
    queryset = RateCard.objects.select_related('zone', 'vehicle_type').all()
    serializer_class = RateCardSerializer
    permission_classes = [IsPlatformAdmin]

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
