from rest_framework.serializers import ModelSerializer, SerializerMethodField
from django.contrib.auth import get_user_model
from base.serializer_fields import S3UploadKeyField

class UserModelSerializer(ModelSerializer):
    avatar = S3UploadKeyField(kind="avatar", required=False, allow_null=True)
    #: The app language the user picked (UserPreference), or None. Written by
    #: PATCH /auth/update/ via views._preferred_language, read here.
    preferred_language = SerializerMethodField()
    #: The app theme the user picked (light, dark or auto), the same way.
    preferred_theme = SerializerMethodField()

    #: The areas this account can use: 'rider' and/or 'driver', from its
    #: Rider and Driver rows (IsDriver checks the row, not `role`). Read-only;
    #: the web routes a signed-in account by it (owner, 2026-11-08).
    roles = SerializerMethodField()

    def get_roles(self, obj):
        roles = [r for r in ('rider', 'driver') if obj.role == r or hasattr(obj, r)]
        return sorted(set(roles), key=('rider', 'driver').index)

    def get_preferred_language(self, obj):
        pref = getattr(obj, 'preference', None)
        return getattr(pref, 'preferred_language', None)

    #: The languages offered in the account's market (10 Nov): the driver's market, else the
    #: phone number's country. India en/te/hi, USA en/es; set per market in the console.
    market_languages = SerializerMethodField()
    market_country = SerializerMethodField()

    def _market(self, obj):
        driver = getattr(obj, 'driver', None) if hasattr(obj, 'driver') else None
        if driver is not None:
            from servers.driver.requirements import market_of
            return market_of(driver)
        from base.countries import country_of_phone
        found = country_of_phone(getattr(obj, 'phone_number', None))
        return (found.code if found else None), ''

    def get_market_country(self, obj):
        return self._market(obj)[0]

    def get_market_languages(self, obj):
        from servers.driver.requirements import DEFAULT_LANGUAGES, resolve
        country, state = self._market(obj)
        if not country:
            return ['en']
        try:
            return resolve(country, state)['languages']
        except Exception:  # noqa: BLE001
            return DEFAULT_LANGUAGES.get(country, ['en'])

    #: 11 Nov: riders sign up by email; the number is proved at the first booking.
    phone_verified = SerializerMethodField()
    email_verified = SerializerMethodField()

    def get_phone_verified(self, obj):
        from .phone_identity import has_verified_phone
        return has_verified_phone(obj)

    def get_email_verified(self, obj):
        return bool(getattr(obj, 'email_verified_at', None))

    def to_representation(self, instance):
        data = super().to_representation(instance)
        from .phone_identity import is_placeholder_phone
        if is_placeholder_phone(data.get('phone_number')):
            data['phone_number'] = None
        if is_placeholder_phone(data.get('username')):
            data['username'] = None
        return data

    def get_preferred_theme(self, obj):
        pref = getattr(obj, 'preference', None)
        return getattr(pref, 'preferred_theme', None)

    class Meta:
        model = get_user_model()
        fields = [
            'id', 'username', 'full_name', 'phone_number', 'email', 
            'gender', 'dob', 'house_no', 'street', 'city', 'zip_code',
            'emergency_contact', 'role', 'avatar', 'fcm_token', 'updated_at', 'created_at','is_updated',
            'preferred_language', 'preferred_theme', 'roles', 'market_languages', 'market_country',
            'phone_verified', 'email_verified',
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
            'password': {'write_only': True},
            # An FCM token is a device credential: anyone holding one can send
            # notifications to that handset. It was being echoed back in every
            # payload this serializer renders -- including the login response and
            # any admin view of a driver's profile -- so a push credential
            # travelled further than it needed to. Write-only: clients still
            # register a token, nothing reads one back. Verified against all
            # three clients (driver, rider, ops web): none of them read the
            # field, so removing it from responses breaks nothing.
            'fcm_token': {'write_only': True},
        }
