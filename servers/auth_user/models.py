from django.db import models
from .usermanager import customUserManager
from django.contrib.auth.models import AbstractUser
from base.media import PrefixedUUIDPath, validate_file_size, validate_image_file
from base.storage_backends import public_media_storage


class customUser(AbstractUser):
    full_name = models.CharField(max_length=256, blank=True, null=True)
    phone_number = models.CharField(max_length=20, unique=True)
    email = models.CharField(max_length=256, blank=True, null=True)
    gender = models.CharField(max_length=10, choices=[('male', 'Male'), ('female', 'Female'), ('other', 'Other')], blank=True, null=True)
    dob = models.DateField(blank=True, null=True)
    house_no = models.CharField(max_length=50, blank=True, null=True)
    street = models.CharField(max_length=256, blank=True, null=True)
    city = models.CharField(max_length=100, blank=True, null=True)
    zip_code = models.CharField(max_length=20, blank=True, null=True)
    emergency_contact = models.CharField(max_length=20, blank=True, null=True)
    role = models.CharField(max_length=20, choices=[("rider", "Rider"), ("driver", 'Driver'), ('admin', 'Admin')])
    avatar = models.FileField(
        blank=True,
        max_length=512,
        null=True,
        storage=public_media_storage,
        upload_to=PrefixedUUIDPath('avatars'),
        validators=[validate_image_file, validate_file_size],
    )
    fcm_token = models.CharField(max_length=255, blank=True, null=True)

    # Which authority an OPERATOR holds. Meaningless on a rider or driver account:
    # `base.permissions.ops_role_of` returns None unless the account is already an
    # operator (role admin AND is_staff), so this field grants nothing on its own
    # and cannot be used to escalate.
    #
    # Defaults to 'admin' deliberately. Every account this field can affect is one
    # that already has full operator authority today, so defaulting to
    # least-privilege would not be a safer default -- it would be a silent
    # behaviour change that strips authority from operators created by existing
    # code paths, dressed up as a default. The RBAC split is opt-in per account,
    # and the migration backfills 'admin' for the same reason.
    ops_role = models.CharField(
        max_length=20, blank=True, default='admin',
        choices=[
            ('support', 'Support'),
            ('driver_ops', 'Driver operations'),
            ('finance', 'Finance'),
            ('admin', 'Administrator'),
        ],
        help_text='Operator authority. Ignored unless the account is an operator.',
    )
    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_updated = models.BooleanField(default=False)
    USERNAME_FIELD = 'phone_number'
    REQUIRED_FIELDS = ['email']
    
    objects = customUserManager()
    
    def __str__(self) -> str:
        return self.full_name if self.full_name else self.phone_number
