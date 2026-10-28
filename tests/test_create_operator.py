"""A named operator is created once, and nobody but them ever holds their
password.

THE PROPERTIES THAT MATTER
--------------------------
`create_operator` exists so `migrate_operators` has an account to assign
without inventing one. It must:

  * create an OPERATOR, never a superuser, with no authority attached;
  * hand over a temporary password that cannot be used to obtain a token --
    the first sign-in demands a new one and issues nothing until it is set;
  * never emit the credential anywhere but the terminal, once;
  * never touch an account that already exists;
  * with `--enrol-mfa`, start an enrolment that the operator completes by
    themselves at first sign-in, after which MFA applies to that account.

The sign-in tests go through `/api/v1/auth/admin/login/`, because that is
the only door, and they check the ORDER of the gates: the password change is
asked for only after every other factor has been proved.
"""

from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from rest_framework.test import APIClient

from base import ops_mfa
from servers.admin_audit.models import AdminAuditLog

User = get_user_model()

pytestmark = pytest.mark.django_db

ADMIN_LOGIN = '/api/v1/auth/admin/login/'
CHANGE_PASSWORD = '/api/v1/auth/password/change/'

EMAIL = 'owner@saaradhigo.test'
PHONE = '+919741000077'
NAME = 'Srinivas (test)'
NEW_PASSWORD = 'Harbour-Lantern-7731-chosen'


@pytest.fixture(autouse=True)
def _quiet_login_guard(settings):
    settings.OPS_LOGIN_MAX_ATTEMPTS = 50
    settings.OPS_LOGIN_MAX_ATTEMPTS_PER_IP = 500
    cache.clear()
    yield
    cache.clear()


ACTOR_EMAIL = 'ops.lead@saaradhigo.test'
ACTOR_PHONE = '+919741000001'


@pytest.fixture(autouse=True)
def actor(db):
    """The operator who runs the command. Every write names them."""
    return User.objects.create_user(
        phone_number=ACTOR_PHONE, username=ACTOR_PHONE, email=ACTOR_EMAIL,
        full_name='Ops Lead', role='admin', is_staff=True,
    )


def _run(**kwargs):
    out = StringIO()
    kwargs.setdefault('email', EMAIL)
    kwargs.setdefault('phone', PHONE)
    kwargs.setdefault('name', NAME)
    kwargs.setdefault('actor_email', ACTOR_EMAIL)
    call_command('create_operator', stdout=out, **kwargs)
    return out.getvalue()


def _temporary_password(output):
    for line in output.splitlines():
        if line.startswith('Temporary password: '):
            return line.split(': ', 1)[1].strip()
    raise AssertionError(f'no temporary password line in:\n{output}')


def _login(**payload):
    payload.setdefault('email', EMAIL)
    return APIClient().post(ADMIN_LOGIN, payload, format='json')


def _code(device):
    """A currently valid TOTP code from django-otp's own generator."""
    import time as _time

    from django_otp.oath import TOTP
    totp = TOTP(device.bin_key, device.step, device.t0, device.digits,
                device.drift)
    totp.time = _time.time()
    return totp.token()


# ---------------------------------------------------------------------------
# The account it creates
# ---------------------------------------------------------------------------

class TestTheAccount:
    def test_it_is_an_operator_and_not_a_superuser(self):
        _run()

        user = User.objects.get(email=EMAIL)
        assert user.role == 'admin'
        assert user.is_staff is True
        assert user.is_superuser is False
        assert user.full_name == NAME
        assert user.phone_number == PHONE

    def test_it_has_no_assignment(self):
        from servers.admin_access.models import AdminAssignment

        _run()

        user = User.objects.get(email=EMAIL)
        assert not AdminAssignment.objects.filter(admin=user).exists()

    def test_the_address_is_verified_and_the_password_is_temporary(self):
        _run()

        user = User.objects.get(email=EMAIL)
        assert user.email_verified_at is not None
        assert user.must_change_password is True

    def test_the_temporary_password_is_printed_once_and_is_the_real_one(self):
        output = _run()

        temporary = _temporary_password(output)
        assert len(temporary) >= 20
        assert output.count(temporary) == 1
        assert User.objects.get(email=EMAIL).check_password(temporary)

    def test_the_address_is_normalised(self):
        _run(email='  Owner@SaaradhiGo.TEST ')

        assert User.objects.filter(email=EMAIL).exists()


class TestItNeverTouchesAnExistingAccount:
    def test_an_existing_address_is_refused(self):
        _run()

        with pytest.raises(CommandError, match='already uses'):
            _run(phone='+919741000078')
        assert User.objects.filter(email__iexact=EMAIL).count() == 1

    def test_an_existing_phone_is_refused(self):
        _run()

        with pytest.raises(CommandError, match='already uses'):
            _run(email='second@saaradhigo.test')
        assert User.objects.filter(phone_number=PHONE).count() == 1

    def test_a_bad_phone_is_refused_before_anything_is_written(self):
        rows_before = AdminAuditLog.objects.count()
        with pytest.raises(CommandError, match='E.164'):
            _run(phone='12345')
        assert not User.objects.exclude(email=ACTOR_EMAIL).exists()
        assert AdminAuditLog.objects.count() == rows_before

    def test_a_bad_address_is_refused(self):
        with pytest.raises(CommandError, match='email'):
            _run(email='not-an-address')
        assert not User.objects.exclude(email=ACTOR_EMAIL).exists()


class TestTheAuditRow:
    def test_a_row_is_written_without_the_credential(self):
        output = _run()
        temporary = _temporary_password(output)

        row = AdminAuditLog.objects.get(action='operator_created')
        user = User.objects.get(email=EMAIL)
        assert row.target_id == str(user.pk)
        assert row.after['is_superuser'] is False
        assert row.after['must_change_password'] is True
        serialised = f'{row.before}{row.after}{row.reason}{row.actor_label}'
        assert temporary not in serialised
        assert PHONE not in serialised


# ---------------------------------------------------------------------------
# First sign-in: the temporary password alone obtains nothing
# ---------------------------------------------------------------------------

class TestFirstSignIn:
    def test_the_temporary_password_alone_yields_no_token(self):
        temporary = _temporary_password(_run())

        r = _login(password=temporary)

        assert r.status_code == 401
        body = r.json()
        assert body['error']['code'] == 'AUTH_PASSWORD_CHANGE_REQUIRED'
        assert 'token' not in str(body.get('data'))

    def test_a_wrong_temporary_password_is_the_generic_refusal(self):
        _run()

        r = _login(password='not-the-temporary-password-at-all')

        assert r.status_code == 401
        assert r.json()['error']['code'] == 'AUTH_INVALID_CREDENTIALS'

    def test_with_a_new_password_the_change_happens_and_a_token_is_issued(self):
        temporary = _temporary_password(_run())

        r = _login(password=temporary, new_password=NEW_PASSWORD)

        assert r.status_code == 200, r.content.decode()
        assert r.json()['data']['token']
        user = User.objects.get(email=EMAIL)
        assert user.must_change_password is False
        assert user.check_password(NEW_PASSWORD)
        assert not user.check_password(temporary)

    def test_afterwards_the_new_password_signs_in_and_the_old_does_not(self):
        temporary = _temporary_password(_run())
        _login(password=temporary, new_password=NEW_PASSWORD)

        assert _login(password=NEW_PASSWORD).status_code == 200
        assert _login(password=temporary).status_code == 401

    def test_the_new_password_must_differ_from_the_temporary_one(self):
        temporary = _temporary_password(_run())

        r = _login(password=temporary, new_password=temporary)

        assert r.status_code == 400
        assert r.json()['error']['code'] == 'AUTH_PASSWORD_WEAK'
        assert User.objects.get(email=EMAIL).must_change_password is True

    def test_a_weak_new_password_is_refused_and_nothing_changes(self):
        temporary = _temporary_password(_run())

        r = _login(password=temporary, new_password='short')

        assert r.status_code == 400
        assert r.json()['error']['code'] == 'AUTH_PASSWORD_WEAK'
        user = User.objects.get(email=EMAIL)
        assert user.must_change_password is True
        assert user.check_password(temporary)

    def test_the_change_is_audited(self):
        temporary = _temporary_password(_run())

        _login(password=temporary, new_password=NEW_PASSWORD)

        assert AdminAuditLog.objects.filter(action='ops_password_changed',
                                            after__forced=True).exists()

    def test_an_ordinary_operator_is_not_asked(self):
        user = User.objects.create_user(
            phone_number='+919741000099', username='+919741000099',
            email='ops@saaradhigo.test', role='admin', is_staff=True,
            password=NEW_PASSWORD,
        )
        assert user.must_change_password is False

        r = _login(email='ops@saaradhigo.test', password=NEW_PASSWORD)

        assert r.status_code == 200

    def test_the_change_password_endpoint_also_clears_the_flag(self):
        temporary = _temporary_password(_run())
        # A token obtained the only way it can be: by changing the password.
        token = _login(password=temporary,
                       new_password=NEW_PASSWORD).json()['data']['token']
        user = User.objects.get(email=EMAIL)
        user.must_change_password = True
        user.save(update_fields=['must_change_password'])

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
        r = client.post(CHANGE_PASSWORD, {
            'current_password': NEW_PASSWORD,
            'new_password': 'Kite-Monsoon-4410-chosen-again',
        }, format='json')

        assert r.status_code == 200, r.content.decode()
        assert User.objects.get(email=EMAIL).must_change_password is False


# ---------------------------------------------------------------------------
# --enrol-mfa: the operator completes enrolment at first sign-in
# ---------------------------------------------------------------------------

class TestEnrolMfa:
    def _device(self):
        return ops_mfa.pending_totp_device(User.objects.get(email=EMAIL))

    def test_the_uri_is_printed_once_and_the_device_is_unconfirmed(self):
        output = _run(enrol_mfa=True)

        uris = [line for line in output.splitlines() if 'otpauth://' in line]
        assert len(uris) == 1
        device = self._device()
        assert device is not None and device.confirmed is False
        assert ops_mfa.has_device(User.objects.get(email=EMAIL)) is False

    def test_without_the_flag_no_device_is_created(self):
        _run()

        assert self._device() is None

    def test_sign_in_asks_for_the_code_while_a_device_is_pending(self):
        temporary = _temporary_password(_run(enrol_mfa=True))

        r = _login(password=temporary, new_password=NEW_PASSWORD)

        assert r.status_code == 401
        assert r.json()['error']['code'] == 'AUTH_MFA_REQUIRED'
        # And the password was NOT changed: MFA comes before the change.
        assert User.objects.get(email=EMAIL).must_change_password is True

    def test_a_wrong_code_does_not_confirm_the_device(self):
        temporary = _temporary_password(_run(enrol_mfa=True))

        r = _login(password=temporary, new_password=NEW_PASSWORD,
                   mfa_code='000000')

        assert r.status_code == 401
        assert r.json()['error']['code'] == 'AUTH_INVALID_CREDENTIALS'
        assert self._device().confirmed is False

    def test_the_first_valid_code_confirms_the_device_and_signs_in(self):
        temporary = _temporary_password(_run(enrol_mfa=True))
        device = self._device()

        r = _login(password=temporary, new_password=NEW_PASSWORD,
                   mfa_code=_code(device))

        assert r.status_code == 200, r.content.decode()
        device.refresh_from_db()
        assert device.confirmed is True
        user = User.objects.get(email=EMAIL)
        assert ops_mfa.has_device(user) is True
        assert user.must_change_password is False
        assert AdminAuditLog.objects.filter(
            action='ops_mfa_enrolled', after__confirmed_at='first_sign_in',
        ).exists()

    def test_from_then_on_mfa_is_required_for_that_account(self):
        temporary = _temporary_password(_run(enrol_mfa=True))
        device = self._device()
        _login(password=temporary, new_password=NEW_PASSWORD,
               mfa_code=_code(device))

        r = _login(password=NEW_PASSWORD)

        assert r.status_code == 401
        assert r.json()['error']['code'] == 'AUTH_MFA_REQUIRED'

    def test_an_already_enrolled_operator_is_unaffected(self):
        """A confirmed device wins; a stray pending one is ignored."""
        temporary = _temporary_password(_run(enrol_mfa=True))
        pending = self._device()
        user = User.objects.get(email=EMAIL)
        confirmed, _ = ops_mfa.enroll_totp(user)
        confirmed.confirmed = True
        confirmed.save(update_fields=['confirmed'])

        r = _login(password=temporary, new_password=NEW_PASSWORD,
                   mfa_code=_code(pending))

        # The pending device's code is not accepted once a real one exists.
        assert r.status_code == 401
        pending.refresh_from_db()
        assert pending.confirmed is False
