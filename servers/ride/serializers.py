from rest_framework import serializers
from servers.ride.models import Trip, FarePricing, Rating
from servers.driver.models import Driver, Vehicle


class FarePricingSerializer(serializers.ModelSerializer):
    class Meta:
        model = FarePricing
        fields = ['base_fare', 'distance_fare', 'time_fare', 'surge_multiplier', 'total_fare']


class RatingSerializer(serializers.ModelSerializer):
    rater_name = serializers.SerializerMethodField()

    class Meta:
        model = Rating
        fields = ['id', 'rater_id', 'rater_name', 'score', 'comments', 'created_at']

    def get_rater_name(self, obj):
        return obj.rater_id.full_name or obj.rater_id.phone_number


class TripListSerializer(serializers.ModelSerializer):
    """Compact serializer for trip lists (ride history)."""
    status = serializers.SerializerMethodField()
    driver_name = serializers.SerializerMethodField()
    vehicle_info = serializers.SerializerMethodField()

    class Meta:
        model = Trip
        fields = [
            'id', 'pickup_address', 'destination_address',
            'pickup_lat', 'pickup_long', 'destination_lat', 'destination_long',
            'estimated_fare', 'final_fare', 'surge_multiplier',
            'estimated_distance_km', 'actual_distance_km',
            'payment_method', 'payment_status',
            'status', 'driver_name', 'vehicle_info',
            'requested_at', 'completed_at', 'cancelled_at',
        ]

    def get_status(self, obj):
        return obj.status_id.status_code if obj.status_id else 'pending'

    def get_driver_name(self, obj):
        if obj.driver_id:
            return str(obj.driver_id)
        return None

    def get_vehicle_info(self, obj):
        if obj.vehicle_id:
            v = obj.vehicle_id
            return {
                'vehicle_number': v.vehicle_number,
                'brand': v.brand,
                'model': v.model,
                'color': v.color,
                'type': str(v.vehicle_type_id) if v.vehicle_type_id else None,
            }
        return None


class TripDetailSerializer(TripListSerializer):
    """Full serializer for single trip detail with fare breakdown and ratings."""
    fare_breakdown = serializers.SerializerMethodField()
    ratings = RatingSerializer(many=True, read_only=True)

    class Meta(TripListSerializer.Meta):
        fields = TripListSerializer.Meta.fields + [
            'accepted_at', 'started_at',
            'fare_breakdown', 'ratings','otp'
        ]

    def get_fare_breakdown(self, obj):
        # Uses prefetched items rather than hitting the DB
        pricing_list = obj.fare_pricing.all()
        if pricing_list:
            return FarePricingSerializer(pricing_list[0]).data
        return None
