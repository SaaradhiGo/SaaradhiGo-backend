from decimal import Decimal

from rest_framework import serializers
from .models import Vehicle, VehicleType, Driver, WithdrawalRequest, DriverBankAccount, IncentiveQuest
from servers.auth_user.serializers import UserModelSerializer
from base.serializer_fields import MoneyField
from base.serializer_fields import S3UploadKeyField
from django.utils import timezone


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
            'capacity', 'vehicle_pic', 'rc_doc', 'permit_doc',
            'insurance_doc','fitness_doc','puc_doc','inspection_doc',
            'insurance_expiry','permit_expiry',
            'fitness_expiry','puc_expiry','status','vehicle_type', 'vehicle_type_id_val',
        ]
        read_only_fields = ['id', 'status']
class DriverProfileSerializer(serializers.ModelSerializer):
    # Accepts a multipart file or an already-uploaded S3 key
    # (license_docs/<uuid>.<ext> from /api/v1/uploads/presign/).
    license_doc = S3UploadKeyField(kind="license_doc", required=False, allow_null=True)
    license_doc_back = S3UploadKeyField(kind="license_doc_back", required=False, allow_null=True)
    active_vehicle_details = VehicleSerializer(source='active_vehicle', read_only=True)

    # MAY THIS DRIVER OPERATE RIGHT NOW -- which is not the same question as
    # "has a human approved their documents".
    #
    # The app used to decide both its routing and its go-online control from
    # `approved` alone, so a QA test driver with simulated eligibility was
    # accepted by the server's dispatch sockets and still sent to the
    # onboarding wizard by the app: eligible on the backend, invisible on the
    # device.
    #
    # `approved` keeps its meaning and is untouched. This additive field says
    # what the three WebSocket gates already decide, so the client and the
    # server stop disagreeing. Read-only, computed, and never writable.
    dispatch_eligible = serializers.SerializerMethodField()
    qa_simulated_eligibility = serializers.BooleanField(read_only=True)

    def get_dispatch_eligible(self, obj) -> bool:
        from servers.driver.qa_eligibility import may_operate

        return may_operate(obj)

    class Meta:
        model = Driver
        # `doc_status` and `doc_rejection_reason` are READ-ONLY and were
        # not exposed at all, so a driver whose documents an operator had
        # rejected saw only `approved: false` -- the same thing a driver who
        # had never uploaded anything sees. They were told "not approved"
        # and never told why, or that there was a reason to read. A review
        # outcome nobody can read is not a review.
        fields = ['id', 'license_doc', 'license_doc_back', 'license_expiry',
                  'active_vehicle', 'active_vehicle_details', 'status',
                  'approved', 'doc_status', 'doc_rejection_reason',
                  'doc_status_updated_at', 'uploaded_timestamp',
                  'dispatch_eligible', 'qa_simulated_eligibility',
                  'total_trips', 'ratings']
        # Defence in depth. This serializer is only ever bound to a DRIVER'S OWN
        # token, and `update_driver_profile` already builds its payload from a
        # four-field allow-list -- so `approved` was never reachable in practice.
        # But the allow-list was the *only* thing standing between a driver and
        # self-approval: anyone who later passes `request.data` straight in, or
        # reuses this serializer in a new view, would hand out the KYC gate with
        # no error and no test failure.
        #
        # These are lifecycle and compliance outcomes, owned by the approval
        # workflow and the trip lifecycle, never by the subject of them. Admin
        # approval is unaffected -- it uses KYCApprovalSerializer, which declares
        # `approved` and `status` writable and enforces the MVA-2020 document
        # checks before allowing approved=True.
        # DEFENCE IN DEPTH. This serializer is bound to a DRIVER'S OWN
        # profile on a PATCH they control, so anything a driver must not be
        # able to set about themselves is listed here as well as being
        # enforced elsewhere. The review outcome is decided by an operator
        # through the KYC endpoint; a driver may read it and nothing more.
        read_only_fields = ['approved', 'status', 'total_trips', 'ratings',
                            'dispatch_eligible', 'qa_simulated_eligibility',
                            'doc_status', 'doc_rejection_reason',
                            'doc_status_updated_at', 'uploaded_timestamp']


class DriverBankAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = DriverBankAccount
        fields = ['bank_name', 'account_number', 'ifsc_code']


class IncentiveQuestSerializer(serializers.ModelSerializer):
    progress_trips = serializers.IntegerField(read_only=True, default=0)
    is_completed = serializers.BooleanField(read_only=True, default=False)
    reward_amount = MoneyField()

    class Meta:
        model = IncentiveQuest
        fields = ['id', 'title', 'description', 'target_trips', 'reward_amount', 'start_date', 'end_date', 'progress_trips', 'is_completed']


class VehicleCreateSerializer(serializers.Serializer):
    vehicle_number = serializers.CharField(max_length=20)
    vehicle_type = serializers.CharField(max_length=50)
    brand = serializers.CharField(max_length=100, required=False, default='')
    model = serializers.CharField(max_length=100, required=False, default='')
    color = serializers.CharField(max_length=50, required=False, default='')
    year = serializers.IntegerField(required=False, default=None)
    capacity = serializers.IntegerField(required=False, default=1)
    rc_doc = S3UploadKeyField(kind="rc_doc", required=False, allow_null=True)
    permit_doc = S3UploadKeyField(kind="permit_doc",required=False,allow_null=True)
    insurance_doc = S3UploadKeyField(kind="insurance_doc",required=False, allow_null=True)
    fitness_doc = S3UploadKeyField(kind="fitness_doc",required=False, allow_null=True)
    puc_doc = S3UploadKeyField(kind="puc_doc",required=False,allow_null=True)
    # US safety inspection. Optional like every other document here: which
    # ones a driver actually needs is decided by their country in
    # servers/driver/compliance.py, not by this serializer.
    inspection_doc = S3UploadKeyField(kind="inspection_doc", required=False,
                                      allow_null=True)
    insurance_expiry = serializers.DateField(required=False,allow_null=True)
    permit_expiry = serializers.DateField(required=False,allow_null=True)
    fitness_expiry = serializers.DateField(required=False,allow_null=True)
    puc_expiry = serializers.DateField(required=False,allow_null=True)
    vehicle_pic = S3UploadKeyField(kind="vehicle_pic",required=False,allow_null=True)

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

def is_real_name(value):
    """A name a person could be addressed by: not blank, not a phone
    number or an email standing in for one."""
    name = (value or '').strip()
    if not name or '@' in name:
        return False
    return not name.replace('-', '').replace(' ', '').lstrip('+').isdigit()


def has_real_name(user):
    """`is_real_name` for the account's own `full_name`."""
    return is_real_name(getattr(user, 'full_name', ''))


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
    - the account must carry a real name: not blank, not a phone number
      or an email (owner ruling 2026-10-30; a nameless driver showed their
      number wherever a name is printed)

    Approval can still be REJECTED (approved=False) at any time without
    these checks — withdrawal of approval doesn't depend on documents.
    """
    class Meta:
        model = Driver
        fields = ['approved', 'status']

    def validate(self, attrs):
        approved = attrs.get('approved')
        if approved is not True:
            return attrs  # Rejection / status-only updates don't need docs.

        driver = self.instance
        if driver is None:
            raise serializers.ValidationError(
                'KYCApprovalSerializer requires an existing Driver instance.'
            )

        missing = []
        if not has_real_name(driver.user_id):
            missing.append('full_name')
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
                    "Phase-0 KYC gate requires the driver's name, license doc + "
                    "expiry, an active vehicle, and that vehicle's RC document on file."
                )
            })
        return attrs

class DriverAdminDetailSerializer(serializers.ModelSerializer):
    user_details = UserModelSerializer(source='user_id', read_only=True)
    vehicles = VehicleSerializer(source='vehicle_set', many=True, read_only=True)

    class Meta:
        model = Driver
        fields = ['id', 'user_details', 'license_doc', 'license_doc_back', 'license_expiry', 'status', 'total_trips', 'ratings', 'approved', 'vehicles', 'active_vehicle']


class WithdrawalRequestSerializer(serializers.ModelSerializer):
    # Ops approving a payout needs to see WHO they are paying and WHERE the
    # money is going without opening a second screen. The list previously
    # returned a bare driver id, which made the approve queue unusable —
    # and maker-checker meaningless if the checker cannot see the payee.
    driver_name = serializers.SerializerMethodField()
    driver_phone = serializers.SerializerMethodField()
    driver_upi_id = serializers.CharField(source='driver.upi_id', read_only=True, default='')
    driver_kyc_approved = serializers.BooleanField(source='driver.approved', read_only=True, default=False)
    amount = MoneyField()

    class Meta:
        model = WithdrawalRequest
        fields = [
            'id', 'driver', 'driver_name', 'driver_phone', 'driver_upi_id',
            # THE CURRENCY OF THE MONEY BEING RELEASED. It is stamped on the
            # row at write time from the driver's wallet (see
            # WithdrawalRequest._derive_currency) and was not exposed, so the
            # payout queue showed a bare number and the console chose a
            # symbol for it. An approver releasing money must be told which
            # money.
            'currency',
            'driver_kyc_approved', 'amount', 'status', 'requested_at',
            'processed_at', 'admin_notes', 'payout_reference_id',
        ]
        read_only_fields = [
            'id', 'driver', 'currency', 'status', 'requested_at',
            'processed_at', 'admin_notes', 'payout_reference_id',
        ]

    def get_driver_name(self, obj):
        user = getattr(obj.driver, 'user_id', None)
        return getattr(user, 'full_name', '') or ''

    def get_driver_phone(self, obj):
        user = getattr(obj.driver, 'user_id', None)
        return getattr(user, 'phone_number', '') or ''


class WithdrawalRequestCreateSerializer(serializers.ModelSerializer):
    """Shape only. The minimum belongs to the driver's market, not to this class.

    `min_value: 500` used to live here, which made it the THIRD copy of India's
    payout minimum (the view had its own `amount < 500`, and
    `validate_withdrawal_amount` had the market-aware one). It is also the copy
    that cannot be fixed in place: a serializer validating `amount` in
    isolation does not know which driver is asking, so it cannot know whether
    500 means rupees or dollars. A US driver requesting $25 was refused here
    before any market-aware code ran.

    What survives is the part that is true in every market -- an amount must be
    a positive number -- and `validate_withdrawal_amount` in the view, which
    has the driver and therefore the currency, owns the threshold.
    """

    amount = MoneyField(required=True, min_value=Decimal('0.01'))

    class Meta:
        model = WithdrawalRequest
        fields = ['amount']
