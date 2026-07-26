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
    """KYC approval is the gate that lets a driver accept rides on the
    platform. We refuse to mark `approved=True` unless the driver has
    the credentials we're legally required to verify under the MoRTH
    Motor Vehicles Aggregator Guidelines 2020:

    - license_doc must be uploaded
    - license_expiry must be set and in the future
    - at least one Vehicle must exist with rc_doc uploaded
    - the active_vehicle must be assigned (so the driver can actually
      take rides post-approval)

    Approval can still be REJECTED (approved=False) at any time without
    these checks — withdrawal of approval doesn't depend on documents.
    """
    class Meta:
        model = Driver
        fields = ['approved', 'status']

    def validate(self, attrs):
        from django.utils import timezone
        approved = attrs.get('approved')
        if approved is not True:
            return attrs  # Rejection / status-only updates don't need docs.

        driver = self.instance
        if driver is None:
            raise serializers.ValidationError(
                'KYCApprovalSerializer requires an existing Driver instance.'
            )

        missing = []
        if not driver.license_doc:
            missing.append('license_doc')
        if not driver.license_expiry:
            missing.append('license_expiry')
        elif driver.license_expiry <= timezone.localdate():
            raise serializers.ValidationError({
                'license_expiry': (
                    f"License has already expired ({driver.license_expiry}); "
                    "refusing to approve. Have the driver upload a renewed "
                    "license before re-attempting approval."
                )
            })
        if not driver.active_vehicle:
            missing.append('active_vehicle')
        elif not getattr(driver.active_vehicle, 'rc_doc', None):
            missing.append('active_vehicle.rc_doc')

        if missing:
            raise serializers.ValidationError({
                'documents': (
                    f"Cannot approve driver — missing: {', '.join(missing)}. "
                    "Phase-0 KYC gate requires license doc + expiry, an active "
                    "vehicle, and that vehicle's RC document on file."
                )
            })
        return attrs

class DriverAdminDetailSerializer(serializers.ModelSerializer):
    user_details = UserModelSerializer(source='user_id', read_only=True)
    vehicles = VehicleSerializer(source='vehicle_set', many=True, read_only=True)

    class Meta:
        model = Driver
        fields = ['id', 'user_details', 'license_doc', 'license_expiry', 'status', 'total_trips', 'ratings', 'approved', 'vehicles', 'active_vehicle']


class WithdrawalRequestSerializer(serializers.ModelSerializer):
    # Ops approving a payout needs to see WHO they are paying and WHERE the
    # money is going without opening a second screen. The list previously
    # returned a bare driver id, which made the approve queue unusable —
    # and maker-checker meaningless if the checker cannot see the payee.
    driver_name = serializers.SerializerMethodField()
    driver_phone = serializers.SerializerMethodField()
    driver_upi_id = serializers.CharField(source='driver.upi_id', read_only=True, default='')
    driver_kyc_approved = serializers.BooleanField(source='driver.approved', read_only=True, default=False)

    class Meta:
        model = WithdrawalRequest
        fields = [
            'id', 'driver', 'driver_name', 'driver_phone', 'driver_upi_id',
            'driver_kyc_approved', 'amount', 'status', 'requested_at',
            'processed_at', 'admin_notes', 'payout_reference_id',
        ]
        read_only_fields = [
            'id', 'driver', 'status', 'requested_at', 'processed_at',
            'admin_notes', 'payout_reference_id',
        ]

    def get_driver_name(self, obj):
        user = getattr(obj.driver, 'user_id', None)
        return getattr(user, 'full_name', '') or ''

    def get_driver_phone(self, obj):
        user = getattr(obj.driver, 'user_id', None)
        return getattr(user, 'phone_number', '') or ''


class WithdrawalRequestCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = WithdrawalRequest
        fields = ['amount']
        extra_kwargs = {
            'amount': {'required': True, 'min_value': 500}
        }
