from rest_framework.serializers import ModelSerializer
from django.contrib.auth import get_user_model
from base.serializer_fields import S3UploadKeyField

class UserModelSerializer(ModelSerializer):
    avatar = S3UploadKeyField(kind="avatar", required=False, allow_null=True)

    class Meta:
        model = get_user_model()
        fields = [
            'id', 'username', 'full_name', 'phone_number', 'email', 
            'gender', 'dob', 'house_no', 'street', 'city', 'zip_code',
            'emergency_contact', 'role', 'avatar', 'fcm_token', 'updated_at', 'created_at','is_updated'
        ]
        # phone_number is the user's identity for OTP-based auth; allowing
        # /auth/update/ to overwrite it lets a stolen access token hijack
        # the account by replacing the phone with the attacker's. To
        # change phone, a future flow must re-verify via OTP on the new
        # number — out of scope for Phase-0.
        read_only_fields = [
            'id', 'username', 'role', 'updated_at', 'created_at',
            'phone_number',
        ]
        extra_kwargs = {
            'password': {'write_only': True}
        }
