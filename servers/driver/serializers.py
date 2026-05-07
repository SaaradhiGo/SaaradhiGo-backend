from rest_framework import serializers
from .models import Vehicle, VehicleType, Driver, WithdrawalRequest
from servers.auth_user.serializers import UserModelSerializer
from base.serializer_fields import NullableFileField


class VehicleTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = VehicleType
        fields = ['id', 'type', 'description']

class VehicleSerializer(serializers.ModelSerializer):
    vehicle_type = VehicleTypeSerializer(source='vehicle_type_id', read_only=True)
    vehicle_type_id_val = serializers.IntegerField(
        source='vehicle_type_id.id', read_only=True
    )

    class Meta:
        model = Vehicle
        fields = [
            'id', 'vehicle_number', 'brand', 'model', 'color', 'year',
            'capacity', 'vehicle_pic', 'rc_doc', 'status',
            'vehicle_type', 'vehicle_type_id_val',
        ]
        read_only_fields = ['id', 'status']
class DriverProfileSerializer(serializers.ModelSerializer):
    license_doc = NullableFileField(required=False, allow_null=True)
    active_vehicle_details = VehicleSerializer(source='active_vehicle', read_only=True)

    class Meta:
        model = Driver
        fields = ['id', 'license_doc', 'license_expiry', 'active_vehicle_details', 'status', 'approved', 'total_trips', 'ratings']


class VehicleCreateSerializer(serializers.Serializer):
    vehicle_number = serializers.CharField(max_length=20)
    vehicle_type = serializers.CharField(max_length=50)
    brand = serializers.CharField(max_length=100, required=False, default='')
    model = serializers.CharField(max_length=100, required=False, default='')
    color = serializers.CharField(max_length=50, required=False, default='')
    year = serializers.IntegerField(required=False, default=None)
    capacity = serializers.IntegerField(required=False, default=1)
    rc_doc = NullableFileField(required=False, allow_null=True)
    vehicle_pic = NullableFileField(required=False, allow_null=True)

    def validate_vehicle_type(self, value):
        try:
            VehicleType.objects.get(type=value)
        except VehicleType.DoesNotExist:
            raise serializers.ValidationError(
                f"Vehicle type '{value}' not found. Available: "
                f"{list(VehicleType.objects.values_list('type', flat=True))}"
            )
        return value


# --- Admin Serializers ---

class DriverAdminListSerializer(serializers.ModelSerializer):
    user_details = UserModelSerializer(source='user_id', read_only=True)

    class Meta:
        model = Driver
        fields = ['id', 'user_details', 'license_doc', 'license_expiry', 'status', 'total_trips', 'ratings', 'approved', 'active_vehicle']

class KYCApprovalSerializer(serializers.ModelSerializer):
    class Meta:
        model = Driver
        fields = ['approved', 'status']

class DriverAdminDetailSerializer(serializers.ModelSerializer):
    user_details = UserModelSerializer(source='user_id', read_only=True)
    vehicles = VehicleSerializer(source='vehicle_set', many=True, read_only=True)

    class Meta:
        model = Driver
        fields = ['id', 'user_details', 'license_doc', 'license_expiry', 'status', 'total_trips', 'ratings', 'approved', 'vehicles', 'active_vehicle']


class WithdrawalRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = WithdrawalRequest
        fields = ['id', 'driver', 'amount', 'status', 'requested_at', 'processed_at', 'admin_notes', 'payout_reference_id']
        read_only_fields = ['id', 'driver', 'status', 'requested_at', 'processed_at', 'admin_notes', 'payout_reference_id']


class WithdrawalRequestCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = WithdrawalRequest
        fields = ['amount']
        extra_kwargs = {
            'amount': {'required': True, 'min_value': 500}
        }
