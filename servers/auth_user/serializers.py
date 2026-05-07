from rest_framework.serializers import ModelSerializer
from django.contrib.auth import get_user_model
from base.serializer_fields import NullableFileField

class UserModelSerializer(ModelSerializer):
    avatar = NullableFileField(required=False, allow_null=True)

    class Meta:
        model = get_user_model()
        fields = [
            'id', 'username', 'full_name', 'phone_number', 'email', 
            'gender', 'dob', 'house_no', 'street', 'city', 'zip_code',
            'emergency_contact', 'role', 'avatar', 'fcm_token', 'updated_at', 'created_at','is_updated'
        ]
        read_only_fields = ['id', 'username', 'role', 'updated_at', 'created_at']
        extra_kwargs = {
            'password': {'write_only': True}
        }
