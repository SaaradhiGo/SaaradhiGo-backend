"""Self-service email registration, and the verification that makes it real.

WHAT THIS COVERS
----------------
`/auth/email/register/` is the half that did not exist: every account came
from phone+OTP (which sets an unusable password) or from an operator invite,
so the public website had nothing to call for "create an account".

The important properties are not "it creates a row":

  * a self-registered account CANNOT sign in until the address is verified,
    or verification would be decorative and anyone could hold an account on a
    stranger's address;
  * it cannot be used to discover who already has an account;
  * it cannot mint an operator.
"""

import pytest
from django.contrib.auth import authenticate, get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db

User = get_user_model()

REGISTER = '/api/v1/auth/email/register/'
EMAIL_LOGIN = '/api/v1/auth/email/login/'

GOOD = {
    'full_name': 'Asha Rao',
    'phone_number': '+919870000001',
    'email': 'asha@deeptrics.com',
    'password': 'Monsoon-Ferry-2026!',
    'confirm_password': 'Monsoon-Ferry-2026!',
    'role': 'rider',
}


def _register(**overrides):
    payload = {**GOOD, **overrides}
    return APIClient().post(REGISTER, payload, format='json')


def _login(email, password):
    return APIClient().post(
        EMAIL_LOGIN, {'email': email, 'password': password}, format='json')


class TestItCreatesAnAccount:
    def test_a_rider_account_is_created(self):
        r = _register()

        assert r.status_code == 201, r.content.decode()
        user = User.objects.get(email__iexact=GOOD['email'])
        assert user.role == 'rider'
        assert user.phone_number == GOOD['phone_number']
        assert user.check_password(GOOD['password'])

    def test_a_rider_profile_is_created_too(self):
        from servers.rider.models import Rider
        _register()
        user = User.objects.get(email__iexact=GOOD['email'])
        assert Rider.objects.filter(user_id=user).exists()

    def test_a_driver_can_register(self):
        r = _register(role='driver', email='dev@deeptrics.com',
                      phone_number='+919870000002')
        assert r.status_code == 201
        assert User.objects.get(email__iexact='dev@deeptrics.com').role == 'driver'

    def test_a_driver_profile_is_created_too(self):
        # THE GAP THAT MADE EVERY DRIVER SCREEN RETURN 500.
        #
        # This branch created a `Rider` for a rider and nothing at all for a
        # driver, so an account created through "Apply to drive" on the
        # website had no `Driver` row. `servers/driver/views.py` reaches it
        # as `request.user.driver` with no guard, so every driver endpoint
        # raised `RelatedObjectDoesNotExist` and answered HTTP 500 -- for
        # the whole life of the account, with no way to repair it from the
        # client.
        from servers.driver.models import Driver

        _register(role='driver', email='dev2@deeptrics.com',
                  phone_number='+919870000003')
        user = User.objects.get(email__iexact='dev2@deeptrics.com')
        assert Driver.objects.filter(user_id=user).exists()

    def test_the_address_starts_unverified(self):
        _register()
        assert User.objects.get(email__iexact=GOOD['email']).email_verified_at is None


class TestVerificationIsRequiredToSignIn:
    def test_a_freshly_registered_account_cannot_sign_in(self):
        _register()

        r = _login(GOOD['email'], GOOD['password'])

        assert r.status_code == 403, (
            'an unverified address signing in makes verification decorative, '
            'and lets someone hold an account on a stranger\'s address'
        )
        assert r.json()['error']['code'] == 'AUTH_EMAIL_UNVERIFIED'

    def test_it_signs_in_once_verified(self):
        _register()
        user = User.objects.get(email__iexact=GOOD['email'])
        user.email_verified_at = timezone.now()
        user.save(update_fields=['email_verified_at'])

        r = _login(GOOD['email'], GOOD['password'])

        assert r.status_code == 200, r.content.decode()
        data = r.json().get('data', {})
        assert data.get('token') and data.get('refresh_token')

    def test_a_wrong_password_is_still_a_generic_refusal(self):
        _register()
        user = User.objects.get(email__iexact=GOOD['email'])
        user.email_verified_at = timezone.now()
        user.save(update_fields=['email_verified_at'])

        r = _login(GOOD['email'], 'not-the-password')

        assert r.status_code == 401
        assert r.json()['error']['code'] == 'AUTH_INVALID_CREDENTIALS'


class TestItDoesNotLeakWhoHasAnAccount:
    def test_registering_a_taken_address_looks_identical(self):
        first = _register()
        second = _register(phone_number='+919870009999')

        assert first.status_code == 201
        assert second.status_code == 202
        # The message a caller sees must not distinguish the two.
        assert (first.json()['data']['message']
                == second.json()['data']['message'])
        assert User.objects.filter(email__iexact=GOOD['email']).count() == 1

    def test_registering_a_taken_phone_looks_identical(self):
        _register()
        second = _register(email='someone.else@deeptrics.com')

        assert second.status_code == 202
        assert not User.objects.filter(
            email__iexact='someone.else@deeptrics.com').exists()


class TestItCannotMintAnOperator:
    @pytest.mark.parametrize('role', ['admin', 'operator', 'staff', 'ADMIN'])
    def test_a_privileged_role_is_refused(self, role):
        r = _register(role=role)

        assert r.status_code == 400
        assert r.json()['error']['code'] == 'AUTH_INVALID_ROLE'
        assert not User.objects.filter(email__iexact=GOOD['email']).exists()

    def test_it_never_creates_staff_or_superusers(self):
        _register()
        user = User.objects.get(email__iexact=GOOD['email'])
        assert not user.is_staff
        assert not user.is_superuser


class TestInputValidation:
    def test_mismatched_passwords_are_refused(self):
        r = _register(confirm_password='something-else')
        assert r.status_code == 400
        assert r.json()['error']['code'] == 'AUTH_PASSWORD_MISMATCH'

    def test_a_weak_password_is_refused_with_the_reason(self):
        r = _register(password='password', confirm_password='password')
        assert r.status_code == 400
        assert r.json()['error']['code'] == 'AUTH_WEAK_PASSWORD'
        assert r.json()['error']['message']

    def test_a_malformed_email_is_refused(self):
        r = _register(email='not-an-address')
        assert r.status_code == 400
        assert r.json()['error']['code'] == 'AUTH_INVALID_EMAIL'

    def test_a_missing_phone_is_refused(self):
        r = _register(phone_number='')
        assert r.status_code == 400
        assert r.json()['error']['code'] == 'AUTH_INVALID_PHONE'


class TestTheConsoleAcceptsEitherIdentifier:
    """`authenticate()` is what the operator console form calls."""

    def _operator(self):
        user = User.objects.create_user(
            phone_number='+919860000001', username='+919860000001',
            role='admin', email='ops.probe@deeptrics.com', is_staff=True)
        user.set_password('Harbour-Lantern-2026!')
        user.email_verified_at = timezone.now()
        user.save()
        return user

    def test_an_email_authenticates(self):
        user = self._operator()
        assert authenticate(username='ops.probe@deeptrics.com',
                            password='Harbour-Lantern-2026!') == user

    def test_the_phone_still_authenticates(self):
        user = self._operator()
        assert authenticate(username='+919860000001',
                            password='Harbour-Lantern-2026!') == user

    def test_the_email_lookup_is_case_insensitive(self):
        user = self._operator()
        assert authenticate(username='OPS.PROBE@Deeptrics.com',
                            password='Harbour-Lantern-2026!') == user

    def test_a_wrong_password_fails_on_either_identifier(self):
        self._operator()
        assert authenticate(username='ops.probe@deeptrics.com',
                            password='wrong') is None
        assert authenticate(username='+919860000001', password='wrong') is None

    def test_an_unknown_address_fails(self):
        self._operator()
        assert authenticate(username='nobody@deeptrics.com',
                            password='Harbour-Lantern-2026!') is None

    def test_an_inactive_account_is_refused(self):
        user = self._operator()
        user.is_active = False
        user.save(update_fields=['is_active'])
        assert authenticate(username='ops.probe@deeptrics.com',
                            password='Harbour-Lantern-2026!') is None
