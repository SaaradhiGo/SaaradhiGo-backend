from django.db import models, transaction
from django.db.models.functions import Lower

from .usermanager import customUserManager
from django.contrib.auth.models import AbstractUser
from base.media import PrefixedUUIDPath, validate_file_size, validate_image_file
from base.storage_backends import public_media_storage


class customUser(AbstractUser):
    full_name = models.CharField(max_length=256, blank=True, null=True)
    phone_number = models.CharField(max_length=20, unique=True)
    # An EmailField, and unique when present -- see the constraint in Meta.
    #
    # This was `CharField(max_length=256)` with no validation and no
    # uniqueness, which was adequate while email was decoration (a receipt
    # address) and is not adequate now that it is an identity you can sign in
    # with. Two accounts sharing an address means two wallets and two trip
    # histories for one person, and a password reset that could reach either.
    #
    # 254 is the RFC 5321 maximum; the old 256 allowed addresses no mail
    # system would accept.
    email = models.EmailField(max_length=254, blank=True, null=True)

    # When this account proved it controls its address, or NULL.
    #
    # Password reset requires it. Without this gate, anyone who could set an
    # unverified address on an account could request a reset to an inbox they
    # own -- which is account takeover with extra steps.
    email_verified_at = models.DateTimeField(blank=True, null=True)
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

    # A SHARED EMERGENCY ACCOUNT, NOT A PERSON.
    #
    # Super Admins must be named individuals, so an account that several
    # people can reach cannot be one. This flag marks the one account that
    # is kept for the case where no named Super Admin can sign in -- its
    # credentials live offline, and it is expected to be unused.
    #
    # Because nobody owns it, its use is made loud rather than prevented:
    # signing in requires a typed reason, every Super Admin is notified,
    # and the reason is written to the audit log. See
    # `servers.admin_access.breakglass`.
    #
    # It grants NO authority by itself. Authority still comes from an
    # assignment, which is why the flag can be set on an account before
    # anybody decides what it may do.
    is_break_glass = models.BooleanField(
        default=False,
        db_index=True,
        help_text=(
            'Shared emergency account. Sign-in demands a reason, alerts '
            'every Super Admin and is audited. Never a named individual.'
        ),
    )

    # THE PASSWORD IS TEMPORARY UNTIL ITS OWNER HAS CHOSEN ONE.
    #
    # `create_operator` hands a new operator a generated password through
    # the terminal of whoever ran it. That person has seen it, so it is not
    # yet a secret the operator alone holds. While this is set,
    # `/auth/admin/login/` accepts the temporary password only together
    # with a new one (`AUTH_PASSWORD_CHANGE_REQUIRED` otherwise) and issues
    # no token until the change has happened. Cleared by that change or by
    # `/auth/password/change/`.
    must_change_password = models.BooleanField(
        default=False,
        help_text=(
            'Set on an account whose password was issued by someone else. '
            'Sign-in demands a new password first and issues no token until '
            'it is set.'
        ),
    )

    updated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_updated = models.BooleanField(default=False)
    USERNAME_FIELD = 'phone_number'
    REQUIRED_FIELDS = ['email']

    objects = customUserManager()

    class Meta:
        constraints = [
            # Unique on the LOWER-CASED address, and only for rows that have
            # one.
            #
            # A plain `unique=True` would not do: it is case-sensitive, so
            # A@b.com and a@b.com would be two accounts, and it would forbid
            # more than one NULL on some backends. The condition keeps the
            # thousands of phone-only accounts out of the constraint
            # entirely.
            models.UniqueConstraint(
                Lower('email'),
                condition=~models.Q(email=None) & ~models.Q(email=''),
                name='uniq_user_email_ci',
            ),
        ]
    
    def __str__(self) -> str:
        return self.full_name if self.full_name else self.phone_number

    def save(self, *args, **kwargs):
        # A change to this account's authority (see `is_break_glass` above
        # and `servers.admin_audit.access_audit`) appends an audit row from
        # a post_save receiver. Inside one transaction, so the flag cannot
        # end up flipped with the row missing -- a savepoint when the
        # caller is already in one, and a few microseconds otherwise.
        with transaction.atomic():
            super().save(*args, **kwargs)
