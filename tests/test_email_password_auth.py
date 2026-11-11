"""Email + password authentication, and the boundaries it must not cross.

The feature is small; the risk is not. Adding a second way to name a person
creates two new attack surfaces, and most of this file is about those rather
than about the happy path:

  * **duplicate identity** — one address must never reach two accounts;
  * **privilege escalation by linking or inviting** — an email must never be
    a route to authority the account does not already hold.

Everything here runs locally with no Redis, no SMTP and no SMS: the cache is
LocMemCache (autouse fixture in conftest) and email goes to `locmem`.
"""

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import override_settings
from rest_framework.test import APIClient

from base import ops_mfa
from servers.auth_user.identity import (
    AUTHORITY_FIELDS,
    EmailAlreadyLinked,
    InvalidEmail,
    assert_email_available,
    link_email,
    normalize_email,
)
from servers.auth_user.password_views import reset_tokens, verify_tokens, _uid

pytestmark = pytest.mark.django_db

User = get_user_model()

EMAIL_LOGIN = '/api/v1/auth/email/login/'
FORGOT = '/api/v1/auth/password/forgot/'
RESET = '/api/v1/auth/password/reset/'
CHANGE = '/api/v1/auth/password/change/'
VERIFY_REQUEST = '/api/v1/auth/email/verify/request/'
VERIFY_CONFIRM = '/api/v1/auth/email/verify/confirm/'
INVITE = '/api/v1/auth/admin/invite/'
TEST_RIDER = '/api/v1/auth/admin/invite/test-rider/'
TEST_DRIVER = '/api/v1/auth/admin/invite/test-driver/'

GOOD_PASSWORD = 'Cadence-Harbour-74'
OTHER_PASSWORD = 'Lantern-Meadow-91'

# Mail goes nowhere real in tests, and `auth_email` is explicit that locmem
# is NOT delivery -- so these tests assert on behaviour, and the ones that
# care about a message being composed override the backend to locmem and read
# `mail.outbox` after forcing delivery_is_configured to pass.
LOCMEM = 'django.core.mail.backends.locmem.EmailBackend'


def _user(email='rider@example.test', phone='+919820000001', role='rider',
          password=GOOD_PASSWORD, verified=True, **kwargs):
    user = User(
        phone_number=phone,
        email=email,
        role=role,
        username=phone,
        **kwargs,
    )
    if password:
        user.set_password(password)
    else:
        user.set_unusable_password()
    user.save()
    if verified and email:
        from django.utils import timezone
        user.email_verified_at = timezone.now()
        user.save(update_fields=['email_verified_at'])
    return user


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def auth(client):
    def _login(user):
        from rest_framework_simplejwt.tokens import AccessToken
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
        return client
    return _login


# ===========================================================================
# Identity: one address, one account
# ===========================================================================

class TestIdentity:
    def test_addresses_are_normalised_before_comparison(self):
        # Case and surrounding whitespace must not create a second account.
        assert normalize_email('  Rider@Example.TEST ') == 'rider@example.test'
        assert normalize_email(None) == ''
        assert normalize_email('') == ''

    def test_unicode_lookalikes_are_normalised(self):
        # A full-width character would otherwise compare unequal to its ASCII
        # twin and slip a duplicate past a uniqueness check.
        assert normalize_email('ｒider@example.test') == 'rider@example.test'

    def test_a_non_address_is_refused(self):
        with pytest.raises(InvalidEmail):
            normalize_email('not-an-address')
        with pytest.raises(InvalidEmail):
            normalize_email('a@b')

    def test_an_address_belonging_to_someone_else_is_refused(self):
        _user(email='taken@example.test', phone='+919820000111')
        with pytest.raises(EmailAlreadyLinked):
            assert_email_available('TAKEN@example.test')

    def test_an_account_may_keep_its_own_address(self):
        owner = _user(email='mine@example.test', phone='+919820000112')
        assert_email_available('mine@example.test', for_user=owner)  # no raise

    def test_the_database_refuses_a_duplicate_even_if_code_forgets(self):
        # The constraint is the real guarantee; `assert_email_available` is
        # only the polite version of it.
        from django.db import IntegrityError, transaction
        _user(email='dup@example.test', phone='+919820000113')
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                User.objects.create(
                    phone_number='+919820000114', username='dup2',
                    email='DUP@example.test', role='rider',
                )

    def test_linking_an_email_never_moves_authority(self):
        operator = _user(email=None, phone='+919820000115', role='admin',
                         is_staff=True, verified=False)
        before = {f: getattr(operator, f) for f in AUTHORITY_FIELDS}

        link_email(operator, 'ops@example.test')

        after = {f: getattr(operator, f) for f in AUTHORITY_FIELDS}
        assert before == after

    def test_linking_does_not_mark_verified_unless_told(self):
        user = _user(email=None, phone='+919820000116', verified=False)
        link_email(user, 'unproven@example.test')
        assert user.email_verified_at is None


# ===========================================================================
# Email login
# ===========================================================================

class TestEmailLogin:
    def test_a_rider_can_sign_in_with_email_and_password(self, client):
        _user(email='rider@example.test')
        response = client.post(EMAIL_LOGIN, {
            'email': 'rider@example.test', 'password': GOOD_PASSWORD,
        }, format='json')

        assert response.status_code == 200, response.content
        data = response.json()['data']
        # The SAME envelope as /auth/login/, so a client can swap one for the
        # other without touching anything else.
        assert data['token']
        assert data['refresh_token']
        assert data['user']['phone_number'] == '+919820000001'

    def test_the_address_is_case_insensitive(self, client):
        _user(email='rider@example.test')
        response = client.post(EMAIL_LOGIN, {
            'email': 'RIDER@EXAMPLE.TEST', 'password': GOOD_PASSWORD,
        }, format='json')
        assert response.status_code == 200

    @pytest.mark.parametrize('email,password,why', [
        ('nobody@example.test', GOOD_PASSWORD, 'unknown address'),
        ('rider@example.test', 'wrong-password', 'wrong password'),
        ('rider@example.test', '', 'missing password'),
        ('', GOOD_PASSWORD, 'missing address'),
        ('not-an-address', GOOD_PASSWORD, 'malformed address'),
    ])
    def test_every_credential_failure_gives_the_identical_refusal(
            self, client, email, password, why):
        # The whole point. A different answer for "no such account" is a free
        # account enumerator, and for a ride-hailing platform, confirming that
        # an address belongs to a driver is itself a disclosure.
        _user(email='rider@example.test')

        response = client.post(EMAIL_LOGIN, {
            'email': email, 'password': password,
        }, format='json')

        assert response.status_code == 401, why
        body = response.json()['error']
        assert body['code'] == 'AUTH_INVALID_CREDENTIALS', why
        assert body['message'] == 'Invalid email or password.', why
        # The `issue` field reaches the client too, so it must not leak which
        # branch was taken.
        assert body['details']['issue'] == 'Credentials were not accepted', why

    def test_an_otp_only_account_cannot_be_signed_into_with_a_password(
            self, client):
        # A rider created by the OTP path has an unusable password. It must
        # refuse like any other failure, not 500 and not succeed on ''.
        _user(email='otponly@example.test', phone='+919820000002',
              password=None)

        response = client.post(EMAIL_LOGIN, {
            'email': 'otponly@example.test', 'password': '',
        }, format='json')
        assert response.status_code == 401

        response = client.post(EMAIL_LOGIN, {
            'email': 'otponly@example.test', 'password': 'anything',
        }, format='json')
        assert response.status_code == 401

    def test_an_inactive_account_is_refused_with_the_same_message(self, client):
        _user(email='gone@example.test', phone='+919820000003',
              is_active=False)
        response = client.post(EMAIL_LOGIN, {
            'email': 'gone@example.test', 'password': GOOD_PASSWORD,
        }, format='json')
        assert response.status_code == 401
        assert response.json()['error']['code'] == 'AUTH_INVALID_CREDENTIALS'

    def test_email_login_does_not_create_accounts(self, client):
        # /auth/otp/ creates a rider on first login because a phone number
        # proves possession. An address proves nothing until verified.
        before = User.objects.count()
        client.post(EMAIL_LOGIN, {
            'email': 'newcomer@example.test', 'password': GOOD_PASSWORD,
        }, format='json')
        assert User.objects.count() == before

    def test_repeated_failures_lock_the_identity_out(self, client):
        from django.conf import settings
        _user(email='target@example.test', phone='+919820000004')
        limit = getattr(settings, 'OPS_LOGIN_MAX_ATTEMPTS', 8)

        for _ in range(limit):
            client.post(EMAIL_LOGIN, {
                'email': 'target@example.test', 'password': 'wrong',
            }, format='json')

        # Even the CORRECT password is refused once locked.
        response = client.post(EMAIL_LOGIN, {
            'email': 'target@example.test', 'password': GOOD_PASSWORD,
        }, format='json')
        assert response.status_code == 401
        assert response.json()['error']['code'] == 'AUTH_INVALID_CREDENTIALS'

    def test_a_successful_login_clears_the_failure_count(self, client):
        _user(email='clears@example.test', phone='+919820000005')

        client.post(EMAIL_LOGIN, {'email': 'clears@example.test',
                                  'password': 'wrong'}, format='json')
        assert client.post(EMAIL_LOGIN, {
            'email': 'clears@example.test', 'password': GOOD_PASSWORD,
        }, format='json').status_code == 200

        from servers.admin_dashboard import login_guard
        from django.test import RequestFactory
        counts = login_guard.failure_count(
            RequestFactory().post('/'), 'clears@example.test')
        assert counts[0] == 0


# ===========================================================================
# MFA: operators must present a second factor
# ===========================================================================

class TestOperatorMfa:
    def _operator(self):
        return _user(email='ops@example.test', phone='+919820000006',
                     role='admin', is_staff=True, ops_role='admin')

    @override_settings(OPS_MFA_ENFORCED=True)
    def test_an_operator_cannot_sign_in_with_a_password_alone(self, client):
        self._operator()
        response = client.post(EMAIL_LOGIN, {
            'email': 'ops@example.test', 'password': GOOD_PASSWORD,
        }, format='json')

        assert response.status_code == 401
        # A DIFFERENT code from the credential refusal, and that is safe: it
        # is only reachable with a correct password, so it tells an attacker
        # nothing they did not already hold.
        assert response.json()['error']['code'] == 'AUTH_MFA_REQUIRED'

    @override_settings(OPS_MFA_ENFORCED=True)
    def test_a_wrong_second_factor_is_refused(self, client):
        self._operator()
        response = client.post(EMAIL_LOGIN, {
            'email': 'ops@example.test', 'password': GOOD_PASSWORD,
            'mfa_code': '000000',
        }, format='json')
        assert response.status_code == 401
        assert response.json()['error']['code'] == 'AUTH_MFA_INVALID'

    @override_settings(OPS_MFA_ENFORCED=True)
    def test_the_right_second_factor_signs_the_operator_in(self, client):
        operator = self._operator()
        from django_otp.plugins.otp_totp.models import TOTPDevice
        device = TOTPDevice.objects.create(user=operator, name='test',
                                           confirmed=True)

        import time
        from django_otp.oath import totp
        code = totp(device.bin_key, step=device.step, t0=device.t0,
                    digits=device.digits)

        response = client.post(EMAIL_LOGIN, {
            'email': 'ops@example.test', 'password': GOOD_PASSWORD,
            'mfa_code': f'{code:0{device.digits}d}',
        }, format='json')

        assert response.status_code == 200, response.content
        assert response.json()['data']['token']

    def test_a_rider_is_never_asked_for_a_second_factor(self, client):
        _user(email='plainrider@example.test', phone='+919820000007')
        response = client.post(EMAIL_LOGIN, {
            'email': 'plainrider@example.test', 'password': GOOD_PASSWORD,
        }, format='json')
        assert response.status_code == 200

    @override_settings(OPS_MFA_ENFORCED=True)
    def test_a_failed_mfa_attempt_counts_toward_the_lockout(self, client):
        # Six digits is brute-forceable in a way a password is not.
        self._operator()
        from django.conf import settings
        limit = getattr(settings, 'OPS_LOGIN_MAX_ATTEMPTS', 8)

        for _ in range(limit):
            client.post(EMAIL_LOGIN, {
                'email': 'ops@example.test', 'password': GOOD_PASSWORD,
                'mfa_code': '000000',
            }, format='json')

        response = client.post(EMAIL_LOGIN, {
            'email': 'ops@example.test', 'password': GOOD_PASSWORD,
            'mfa_code': '000000',
        }, format='json')
        assert response.json()['error']['code'] == 'AUTH_INVALID_CREDENTIALS'


# ===========================================================================
# Forgot / reset
# ===========================================================================

class TestPasswordReset:
    @pytest.fixture(autouse=True)
    def _mail_to_locmem(self, settings):
        """Compose mail into `mail.outbox` instead of sending it.

        A pytest class cannot take `@override_settings` -- Django refuses
        anything that is not a SimpleTestCase subclass -- so the settings
        fixture does it per test.
        """
        settings.EMAIL_BACKEND = LOCMEM
        settings.DEFAULT_FROM_EMAIL = 'no-reply@test'
        settings.EMAIL_HOST = 'smtp.test'
        mail.outbox = []


    @pytest.mark.parametrize('email', [
        'known@example.test',
        'nobody@example.test',
        'not-an-address',
        '',
    ])
    def test_forgot_always_answers_the_same(self, client, email):
        _user(email='known@example.test', phone='+919820000008')

        response = client.post(FORGOT, {'email': email}, format='json')

        assert response.status_code == 200
        assert 'If that address belongs to an account' in \
            response.json()['data']['message']

    def test_a_verified_address_receives_a_link(self, client):
        _user(email='known@example.test', phone='+919820000009')
        client.post(FORGOT, {'email': 'known@example.test'}, format='json')
        assert len(mail.outbox) == 1
        assert 'reset' in mail.outbox[0].subject.lower()

    def test_an_unverified_address_receives_nothing(self, client):
        # Otherwise whoever set an unverified address on an account could
        # take it over.
        _user(email='unproven@example.test', phone='+919820000010',
              verified=False)
        client.post(FORGOT, {'email': 'unproven@example.test'}, format='json')
        assert mail.outbox == []

    def test_an_unknown_address_receives_nothing(self, client):
        client.post(FORGOT, {'email': 'nobody@example.test'}, format='json')
        assert mail.outbox == []

    def test_a_valid_token_sets_the_password(self, client):
        user = _user(email='reset@example.test', phone='+919820000011')
        token = reset_tokens.make_token(user)

        response = client.post(RESET, {
            'uid': _uid(user), 'token': token, 'new_password': OTHER_PASSWORD,
        }, format='json')

        assert response.status_code == 200, response.content
        user.refresh_from_db()
        assert user.check_password(OTHER_PASSWORD)
        assert not user.check_password(GOOD_PASSWORD)

    def test_a_token_cannot_be_used_twice(self, client):
        # Free from the generator: the token hashes the password hash, so
        # using it changes the hash and invalidates the token.
        user = _user(email='once@example.test', phone='+919820000012')
        token = reset_tokens.make_token(user)
        payload = {'uid': _uid(user), 'token': token,
                   'new_password': OTHER_PASSWORD}

        assert client.post(RESET, payload, format='json').status_code == 200
        second = client.post(RESET, payload, format='json')
        assert second.status_code == 400
        assert second.json()['error']['code'] == 'AUTH_RESET_TOKEN_INVALID'

    def test_a_token_from_another_account_is_refused(self, client):
        victim = _user(email='victim@example.test', phone='+919820000013')
        attacker = _user(email='attacker@example.test', phone='+919820000014')

        response = client.post(RESET, {
            'uid': _uid(victim),
            'token': reset_tokens.make_token(attacker),
            'new_password': OTHER_PASSWORD,
        }, format='json')

        assert response.status_code == 400
        victim.refresh_from_db()
        assert victim.check_password(GOOD_PASSWORD)

    def test_a_verification_token_cannot_be_used_to_reset_a_password(
            self, client):
        # The reason the two generators have different key_salts. A
        # verification link goes to an address that has NOT been proven.
        user = _user(email='salt@example.test', phone='+919820000015',
                     verified=False)

        response = client.post(RESET, {
            'uid': _uid(user),
            'token': verify_tokens.make_token(user),
            'new_password': OTHER_PASSWORD,
        }, format='json')

        assert response.status_code == 400
        user.refresh_from_db()
        assert user.check_password(GOOD_PASSWORD)

    def test_an_expired_token_is_refused(self, client):
        user = _user(email='expired@example.test', phone='+919820000016')
        token = reset_tokens.make_token(user)

        with override_settings(PASSWORD_RESET_TIMEOUT=0):
            import time
            time.sleep(1.1)
            response = client.post(RESET, {
                'uid': _uid(user), 'token': token,
                'new_password': OTHER_PASSWORD,
            }, format='json')

        assert response.status_code == 400

    def test_a_weak_password_is_refused_with_a_reason(self, client):
        user = _user(email='weak@example.test', phone='+919820000017')
        response = client.post(RESET, {
            'uid': _uid(user), 'token': reset_tokens.make_token(user),
            'new_password': '123',
        }, format='json')

        assert response.status_code == 400
        assert response.json()['error']['code'] == 'AUTH_PASSWORD_WEAK'
        user.refresh_from_db()
        assert user.check_password(GOOD_PASSWORD)

    def test_completing_a_reset_verifies_the_address(self, client):
        # Receiving the link at that address is proof of control, which is
        # what makes an invited account usable without a separate step.
        user = _user(email='invited@example.test', phone='+919820000018',
                     verified=False)
        client.post(RESET, {
            'uid': _uid(user), 'token': reset_tokens.make_token(user),
            'new_password': OTHER_PASSWORD,
        }, format='json')

        user.refresh_from_db()
        assert user.email_verified_at is not None


# ===========================================================================
# Change password
# ===========================================================================

class TestChangePassword:
    def test_the_current_password_is_required(self, client, auth):
        user = _user(email='change@example.test', phone='+919820000019')
        c = auth(user)

        response = c.post(CHANGE, {
            'current_password': 'wrong', 'new_password': OTHER_PASSWORD,
        }, format='json')

        assert response.status_code == 401
        user.refresh_from_db()
        assert user.check_password(GOOD_PASSWORD)

    def test_a_valid_change_succeeds(self, client, auth):
        user = _user(email='change2@example.test', phone='+919820000020')
        c = auth(user)

        response = c.post(CHANGE, {
            'current_password': GOOD_PASSWORD, 'new_password': OTHER_PASSWORD,
        }, format='json')

        assert response.status_code == 200, response.content
        user.refresh_from_db()
        assert user.check_password(OTHER_PASSWORD)

    def test_changing_a_password_invalidates_outstanding_reset_tokens(
            self, client, auth):
        user = _user(email='change3@example.test', phone='+919820000021')
        stale = reset_tokens.make_token(user)

        c = auth(user)
        c.post(CHANGE, {'current_password': GOOD_PASSWORD,
                        'new_password': OTHER_PASSWORD}, format='json')

        client.credentials()
        response = client.post(RESET, {
            'uid': _uid(user), 'token': stale, 'new_password': 'Third-Pass-55',
        }, format='json')
        assert response.status_code == 400

    def test_an_anonymous_caller_cannot_change_a_password(self, client):
        response = client.post(CHANGE, {
            'current_password': GOOD_PASSWORD, 'new_password': OTHER_PASSWORD,
        }, format='json')
        assert response.status_code in (401, 403)

    def test_an_otp_only_account_can_adopt_a_password(self, client, auth):
        # A rider who has only ever used OTP sets a first password without a
        # "current" one, because there is none to present.
        user = _user(email='adopt@example.test', phone='+919820000022',
                     password=None)
        c = auth(user)

        response = c.post(CHANGE, {'new_password': OTHER_PASSWORD},
                          format='json')

        assert response.status_code == 200, response.content
        user.refresh_from_db()
        assert user.check_password(OTHER_PASSWORD)


# ===========================================================================
# Email verification
# ===========================================================================

class TestEmailVerification:
    @pytest.fixture(autouse=True)
    def _mail_to_locmem(self, settings):
        """Compose mail into `mail.outbox` instead of sending it.

        A pytest class cannot take `@override_settings` -- Django refuses
        anything that is not a SimpleTestCase subclass -- so the settings
        fixture does it per test.
        """
        settings.EMAIL_BACKEND = LOCMEM
        settings.DEFAULT_FROM_EMAIL = 'no-reply@test'
        settings.EMAIL_HOST = 'smtp.test'
        mail.outbox = []


    def test_a_user_can_request_and_confirm(self, client, auth):
        user = _user(email='verify@example.test', phone='+919820000023',
                     verified=False)
        c = auth(user)

        assert c.post(VERIFY_REQUEST, {}, format='json').status_code == 200
        assert len(mail.outbox) == 1

        client.credentials()
        response = client.post(VERIFY_CONFIRM, {
            'uid': _uid(user), 'token': verify_tokens.make_token(user),
        }, format='json')

        assert response.status_code == 200
        user.refresh_from_db()
        assert user.email_verified_at is not None

    def test_setting_a_new_address_drops_verification(self, client, auth):
        # Otherwise someone could swap in an address they do not control and
        # keep reset access.
        user = _user(email='old@example.test', phone='+919820000024')
        assert user.email_verified_at is not None

        auth(user).post(VERIFY_REQUEST, {'email': 'new@example.test'},
                        format='json')

        user.refresh_from_db()
        assert user.email == 'new@example.test'
        assert user.email_verified_at is None

    def test_an_address_owned_by_another_account_is_refused(self, client, auth):
        _user(email='someone@example.test', phone='+919820000025')
        user = _user(email='me@example.test', phone='+919820000026')

        response = auth(user).post(
            VERIFY_REQUEST, {'email': 'someone@example.test'}, format='json')

        assert response.status_code == 409
        assert response.json()['error']['code'] == 'AUTH_EMAIL_IN_USE'
        user.refresh_from_db()
        assert user.email == 'me@example.test'

    def test_a_reset_token_cannot_be_used_to_verify(self, client):
        user = _user(email='salt2@example.test', phone='+919820000027',
                     verified=False)
        response = client.post(VERIFY_CONFIRM, {
            'uid': _uid(user), 'token': reset_tokens.make_token(user),
        }, format='json')
        assert response.status_code == 400
        user.refresh_from_db()
        assert user.email_verified_at is None


# ===========================================================================
# Invites: the privilege-escalation surface
# ===========================================================================

class TestInvite:
    """Three routes, three capabilities (owner's ruling, 2026-10-28).

    `/auth/admin/invite/` invites OPERATORS and needs `admin.manage`;
    `/auth/admin/invite/test-rider/` and `/test-driver/` create QA test
    accounts, need `rider.write` / `driver.write`, and are routed outside
    production only. `rider.write` is held by Super Admin alone, so the
    Support Agents who created rider test accounts on QA no longer can
    unless the owner grants it to `support_agent`.
    """

    _seq = iter(range(1, 9999))

    @pytest.fixture(autouse=True)
    def _mail_to_locmem(self, settings):
        """Compose mail into `mail.outbox` instead of sending it.

        A pytest class cannot take `@override_settings` -- Django refuses
        anything that is not a SimpleTestCase subclass -- so the settings
        fixture does it per test.
        """
        settings.EMAIL_BACKEND = LOCMEM
        settings.DEFAULT_FROM_EMAIL = 'no-reply@test'
        settings.EMAIL_HOST = 'smtp.test'
        mail.outbox = []

    def _admin(self, ops_role='admin'):
        """An UNMIGRATED legacy operator: the bridge answers for it."""
        return _user(email=f'admin-{ops_role}@example.test',
                     phone=f'+91982100{abs(hash(ops_role)) % 10000:04d}',
                     role='admin', is_staff=True, ops_role=ops_role)

    def _super_admin(self):
        """A migrated Super Admin at Global: holds rider.write and driver.write."""
        from conftest import grant_legacy_equivalent
        admin = self._admin()
        grant_legacy_equivalent(admin)          # admin -> super_admin @ GLOBAL
        return admin

    def _scoped(self, role_code, scope_code):
        """A migrated operator holding `role_code` at `scope_code`."""
        from conftest import seed_admin_access
        from servers.admin_access.models import AdminAssignment, AdminRole, GeoNode

        seed_admin_access()
        i = next(self._seq)
        user = _user(email=f'{role_code}-{i}@example.test',
                     phone=f'+91982200{i:04d}', role='admin', is_staff=True)
        AdminAssignment.objects.create(
            admin=user, role=AdminRole.objects.get(code=role_code),
            scope=GeoNode.objects.get(code=scope_code),
        )
        return user

    # --- the routes themselves ------------------------------------------

    def test_the_test_account_routes_exist_outside_production_only(self):
        from servers.auth_user.urls import qa_only_urlpatterns

        with override_settings(IS_PRODUCTION=True):
            assert qa_only_urlpatterns() == []
        with override_settings(IS_PRODUCTION=False):
            assert {str(p.pattern) for p in qa_only_urlpatterns()} == {
                'admin/invite/test-rider/', 'admin/invite/test-driver/',
            }

    def test_the_test_account_views_refuse_in_production_even_if_routed(
            self, client, auth):
        """Second lock: the URLconf drops the route; the view refuses too."""
        admin = self._super_admin()
        with override_settings(IS_PRODUCTION=True):
            rider = auth(admin).post(TEST_RIDER, {'email': 'p1@example.test'}, format='json')
            driver = auth(admin).post(TEST_DRIVER, {'email': 'p2@example.test'}, format='json')

        assert rider.status_code == 404 and driver.status_code == 404
        assert not User.objects.filter(email__in=['p1@example.test', 'p2@example.test']).exists()

    def test_the_operator_route_refuses_test_account_roles(self, client, auth):
        response = auth(self._super_admin()).post(INVITE, {
            'email': 'qa-rider@example.test', 'role': 'rider',
        }, format='json')

        assert response.status_code == 400
        assert response.json()['error']['code'] == 'AUTH_INVALID_ROLE'
        assert 'test-rider' in response.json()['error']['message']
        assert not User.objects.filter(email='qa-rider@example.test').exists()

    # --- test accounts --------------------------------------------------

    def test_a_super_admin_can_create_a_rider_test_account_without_any_password(
            self, client, auth):
        response = auth(self._super_admin()).post(TEST_RIDER, {
            'email': 'qa-rider@example.test', 'full_name': 'QA Rider',
        }, format='json')

        assert response.status_code == 201, response.content
        data = response.json()['data']

        # No password VALUE and no password-bearing field in the response.
        assert not [k for k in data if 'password' in k.lower()]
        assert GOOD_PASSWORD not in str(data)
        assert OTHER_PASSWORD not in str(data)

        invited = User.objects.get(pk=data['user_id'])
        assert invited.role == 'rider'
        assert not invited.has_usable_password()
        assert not invited.is_staff
        # A rider profile row, so the account behaves like an OTP-created one.
        from servers.rider.models import Rider
        assert Rider.objects.filter(user_id=invited).exists()

    def test_the_invited_account_sets_its_own_password(self, client, auth):
        response = auth(self._super_admin()).post(TEST_DRIVER, {
            'email': 'qa-driver@example.test',
        }, format='json')
        assert response.status_code == 201, response.content
        invited = User.objects.get(pk=response.json()['data']['user_id'])

        client.credentials()
        reset = client.post(RESET, {
            'uid': _uid(invited), 'token': reset_tokens.make_token(invited),
            'new_password': GOOD_PASSWORD,
        }, format='json')
        assert reset.status_code == 200

        login = client.post(EMAIL_LOGIN, {
            'email': 'qa-driver@example.test', 'password': GOOD_PASSWORD,
        }, format='json')
        assert login.status_code == 200
        assert login.json()['data']['user']['role'] == 'driver'

    def test_a_support_agent_can_no_longer_create_test_riders(self, client, auth):
        """`support_agent` does not hold `rider.write`. Stated in the PR: on
        QA this now needs Super Admin, unless the owner grants `rider.write`
        to `support_agent`."""
        support = self._scoped('support_agent', 'IN')
        response = auth(support).post(TEST_RIDER, {'email': 'qa2@example.test'}, format='json')

        assert response.status_code == 403
        assert not User.objects.filter(email='qa2@example.test').exists()

    def test_a_city_ops_manager_creates_test_drivers_in_their_own_city_only(
            self, client, auth):
        """Scoped: `driver.write` at Hyderabad creates Hyderabad test drivers."""
        from servers.driver.models import Driver
        city_ops = self._scoped('city_ops_manager', 'IN-TG-HYD')

        own = auth(city_ops).post(TEST_DRIVER, {
            'email': 'hyd-driver@example.test', 'home_city': 'IN-TG-HYD',
        }, format='json')
        assert own.status_code == 201, own.content
        assert own.json()['data']['home_city'] == 'IN-TG-HYD'
        assert Driver.objects.get(user_id=own.json()['data']['user_id']).home_city.code == 'IN-TG-HYD'

        elsewhere = auth(city_ops).post(TEST_DRIVER, {
            'email': 'aus-driver@example.test', 'home_city': 'US-TX-AUS',
        }, format='json')
        assert elsewhere.status_code == 403
        assert elsewhere.json()['error']['code'] == 'SCOPE_NOT_HELD'

        nowhere = auth(city_ops).post(TEST_DRIVER, {'email': 'no-city@example.test'}, format='json')
        assert nowhere.status_code == 400
        assert nowhere.json()['error']['code'] == 'HOME_CITY_REQUIRED'

        unknown = auth(city_ops).post(TEST_DRIVER, {
            'email': 'x-city@example.test', 'home_city': 'XX-NOPE',
        }, format='json')
        assert unknown.status_code == 400
        assert unknown.json()['error']['code'] == 'HOME_CITY_UNKNOWN'

        # No `rider.write` on the template: a test rider is refused.
        rider = auth(city_ops).post(TEST_RIDER, {'email': 'cityops-rider@example.test'}, format='json')
        assert rider.status_code == 403

        assert not User.objects.filter(email__in=[
            'aus-driver@example.test', 'no-city@example.test',
            'x-city@example.test', 'cityops-rider@example.test',
        ]).exists()

    def test_a_rider_cannot_invite_anyone(self, client, auth):
        rider = _user(email='justarider@example.test', phone='+919821000001')
        for url, body in ((INVITE, {'email': 'x@example.test', 'role': 'admin'}),
                          (TEST_RIDER, {'email': 'y@example.test'}),
                          (TEST_DRIVER, {'email': 'z@example.test'})):
            response = auth(rider).post(url, body, format='json')
            assert response.status_code in (401, 403), url
        assert not User.objects.filter(email__in=[
            'x@example.test', 'y@example.test', 'z@example.test']).exists()

    # --- operators ------------------------------------------------------

    def test_a_support_operator_cannot_create_an_operator(self, client, auth):
        # The escalation that matters: a compromised support account must not
        # be able to mint an admin. Refused by the gate for the legacy
        # account (no `ops.admin`) and for the migrated one (no `admin.manage`).
        for support in (self._admin(ops_role='support'),
                        self._scoped('support_agent', 'IN')):
            response = auth(support).post(INVITE, {
                'email': 'newops@example.test', 'role': 'admin',
            }, format='json')

            assert response.status_code == 403
        assert not User.objects.filter(email='newops@example.test').exists()

    def test_an_existing_address_is_never_re_pointed(self, client, auth):
        # "Update the existing account's role" is the escalation path this
        # endpoint must not have.
        existing = _user(email='existing@example.test', phone='+919821000002',
                         role='rider')
        admin = self._admin()

        response = auth(admin).post(INVITE, {
            'email': 'existing@example.test', 'role': 'admin',
        }, format='json')

        assert response.status_code == 409
        existing.refresh_from_db()
        assert existing.role == 'rider'
        assert not existing.is_staff

    def test_an_existing_phone_is_refused(self, client, auth):
        _user(email='phoneowner@example.test', phone='+919821000003')

        response = auth(self._super_admin()).post(TEST_RIDER, {
            'email': 'other@example.test', 'phone_number': '+919821000003',
        }, format='json')

        assert response.status_code == 409
        assert response.json()['error']['code'] == 'AUTH_PHONE_IN_USE'

    def test_superuser_cannot_be_requested(self, client, auth):
        admin = self._admin()
        response = auth(admin).post(INVITE, {
            'email': 'sudo@example.test', 'role': 'admin',
            'is_superuser': True, 'ops_role': 'support',
        }, format='json')

        assert response.status_code == 201
        invited = User.objects.get(email='sudo@example.test')
        # The field is simply not read. A superuser is made with manage.py on
        # a machine someone already has access to.
        assert not invited.is_superuser
        assert invited.is_staff and invited.ops_role == 'support'

    def test_an_invite_without_a_phone_gets_a_non_dialable_placeholder(
            self, client, auth):
        response = auth(self._super_admin()).post(TEST_RIDER, {
            'email': 'nophone@example.test',
        }, format='json')

        invited = User.objects.get(pk=response.json()['data']['user_id'])
        # Not a real number, so it can never receive an OTP.
        assert invited.phone_number.startswith('invite-')
        assert '+' not in invited.phone_number

    def test_every_invite_is_audited(self, client, auth):
        from servers.admin_audit.models import AdminAuditLog
        admin = self._super_admin()
        auth(admin).post(TEST_RIDER, {'email': 'audited@example.test'}, format='json')

        row = AdminAuditLog.objects.filter(action='account_invited').first()
        assert row is not None
        assert row.actor_id == admin.pk
        assert row.after['role'] == 'rider'
        # The audit row must not carry a credential.
        assert 'password' not in str(row.after).lower()

    def test_an_operator_invite_is_audited_with_its_ops_role(self, client, auth):
        from servers.admin_audit.models import AdminAuditLog
        admin = self._admin()
        auth(admin).post(INVITE, {'email': 'ops-audited@example.test',
                                  'ops_role': 'finance'}, format='json')

        row = AdminAuditLog.objects.filter(action='account_invited').first()
        assert row is not None
        assert row.after['role'] == 'admin' and row.after['ops_role'] == 'finance'

    def test_a_refused_operator_invite_creates_nothing(self, client, auth):
        support = self._admin(ops_role='support')
        response = auth(support).post(INVITE, {'email': 'nope@example.test',
                                               'role': 'admin'}, format='json')

        assert response.status_code == 403
        assert not User.objects.filter(email='nope@example.test').exists()


# ===========================================================================
# The OTP path must be untouched
# ===========================================================================

class TestOtpStillWorks:
    @override_settings(TEST_PHONE_NUMBERS={'+919999000001': '100001'})
    def test_phone_and_otp_login_is_unchanged(self, client):
        assert client.post('/api/v1/auth/otp/', {
            'phone_number': '+919999000001', 'role': 'rider',
        }, format='json').status_code == 200

        response = client.post('/api/v1/auth/login/', {
            'phone_number': '+919999000001', 'otp': '100001',
        }, format='json')

        assert response.status_code == 200
        assert response.json()['data']['token']

    @override_settings(TEST_PHONE_NUMBERS={'+919999000001': '100001'})
    def test_an_otp_created_account_has_no_password_and_that_is_fine(
            self, client):
        client.post('/api/v1/auth/otp/', {'phone_number': '+919999000001',
                                          'role': 'rider'}, format='json')
        client.post('/api/v1/auth/login/', {'phone_number': '+919999000001',
                                            'otp': '100001'}, format='json')

        user = User.objects.get(phone_number='+919999000001')
        assert not user.has_usable_password()

    def test_refresh_still_works_for_an_email_login_session(self, client):
        user = _user(email='refresh@example.test', phone='+919821000004')
        login = client.post(EMAIL_LOGIN, {
            'email': 'refresh@example.test', 'password': GOOD_PASSWORD,
        }, format='json')
        refresh_token = login.json()['data']['refresh_token']

        response = client.post('/api/v1/auth/refresh/',
                               {'refresh_token': refresh_token},
                               format='json')
        assert response.status_code == 200, response.content
        assert response.json()['data']['token']
