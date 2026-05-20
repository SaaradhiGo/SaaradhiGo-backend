from rest_framework import serializers

from servers.driver.models import VehicleType
from servers.pricing.models import RateCard, ServiceZone


class ServiceZoneSerializer(serializers.ModelSerializer):
    class Meta:
        model = ServiceZone
        fields = (
            'id', 'code', 'name', 'country', 'state_code', 'city',
            'zone_type', 'parent', 'polygon_geojson', 'priority',
            'currency', 'timezone_name', 'is_active', 'metadata',
            'created_at', 'updated_at',
        )
        read_only_fields = ('id', 'created_at', 'updated_at')

    def validate_polygon_geojson(self, value):
        # Accept either {"type":"Polygon","coordinates":[[...]]} or a raw
        # [[lon,lat], ...] list. Reject anything that can't form a closed
        # polygon -- otherwise zone lookups would silently never match.
        if isinstance(value, list):
            ring = value
        elif isinstance(value, dict):
            if value.get('type') != 'Polygon':
                raise serializers.ValidationError(
                    "Only GeoJSON 'Polygon' supported."
                )
            coords = value.get('coordinates') or []
            if not coords:
                raise serializers.ValidationError(
                    "Polygon coordinates missing."
                )
            ring = coords[0]
        else:
            raise serializers.ValidationError(
                "polygon_geojson must be a GeoJSON Polygon or list of [lon,lat] pairs."
            )

        if not isinstance(ring, list) or len(ring) < 4:
            raise serializers.ValidationError(
                "Polygon must have at least 4 points (incl. closing point)."
            )
        for p in ring:
            if not (isinstance(p, list) and len(p) == 2):
                raise serializers.ValidationError(
                    "Each polygon point must be [lon, lat]."
                )
            try:
                lon, lat = float(p[0]), float(p[1])
            except (TypeError, ValueError) as exc:
                raise serializers.ValidationError(
                    f"Invalid coordinate {p}: {exc}"
                )
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                raise serializers.ValidationError(
                    f"Coordinate {p} out of range."
                )
        return value


class RateCardSerializer(serializers.ModelSerializer):
    zone_code = serializers.CharField(source='zone.code', read_only=True)
    vehicle_type_name = serializers.CharField(source='vehicle_type.type', read_only=True)

    class Meta:
        model = RateCard
        fields = (
            'id', 'zone', 'zone_code', 'vehicle_type', 'vehicle_type_name',
            'base_fare', 'per_km_fare', 'per_min_fare', 'min_fare',
            'night_surge_multiplier',
            'night_surge_start_hour', 'night_surge_end_hour',
            'surge_cap_multiplier', 'commission_percent', 'gst_percent',
            'effective_from', 'effective_to', 'version',
            'is_active', 'notes', 'created_at', 'updated_at',
        )
        read_only_fields = (
            'id', 'created_at', 'updated_at', 'zone_code', 'vehicle_type_name',
        )

    def validate(self, attrs):
        eff_from = attrs.get('effective_from') or getattr(self.instance, 'effective_from', None)
        eff_to = attrs.get('effective_to', getattr(self.instance, 'effective_to', None))
        if eff_from and eff_to and eff_to <= eff_from:
            raise serializers.ValidationError(
                {'effective_to': 'Must be after effective_from.'}
            )
        for fld in ('base_fare', 'per_km_fare', 'per_min_fare', 'min_fare'):
            v = attrs.get(fld)
            if v is not None and v < 0:
                raise serializers.ValidationError({fld: 'Must be non-negative.'})
        cap = attrs.get('surge_cap_multiplier')
        if cap is not None and cap < 1:
            raise serializers.ValidationError(
                {'surge_cap_multiplier': 'Must be >= 1.00 (surge caps cannot reduce base fare).'}
            )
        return attrs
