"""`reset_operator_mfa`: the break-glass path for a lost authenticator.

THE RULING (owner, 2026-10-28)
------------------------------
    Accepted as the break-glass path, with --actor-email and a reason
    required, audited. When ADMIN_MFA_REQUIRED=true, an account with zero
    factors can do nothing except enrol at next login. Add a test.

WHAT IS PROVED
--------------
  * `--confirm` without a reason, or with a short one, changes nothing;
    `--actor-email` is required the same way (the refusals for a bad actor
    are in `tests/test_actor_email_mandatory.py`);
  * a reset removes every confirmed factor, starts a fresh enrolment, and
    leaves two rows -- the reset and the session revocation -- both naming
    the actor and the reason, never the secret;
  * afterwards, with MFA on, the account can do nothing except enrol: a
    password alone yields `AUTH_MFA_REQUIRED` and no token, the old session
    cannot be renewed, a code from the lost device is refused, and the
    first valid code from the NEW device confirms it and signs in;
  * with `ADMIN_MFA_REQUIRED=false` (QA today) the reset still works and
    the pending device is simply ignored at sign-in.

The sign-in goes through `/api/v1/auth/admin/login/`, the only door.
"""

from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from base import ops_mfa
from servers.admin_access.breakglass import MIN_REASON_LENGTH
from servers.admin_audit.models import AdminAuditLog

User = get_user_model()

pytestmark = pytest.mark.django_db

ADMIN_LOGIN = '/api/v1/auth/admin/login/'
REFRESH = '/api/v1/auth/refresh/'

PHONE = '+919741000501'
EMAIL = 'lost.phone@saaradhigo.test'
PASSWORD = 'Harbour-Lantern-7731-chosen'
ACTOR_EMAIL = 'ops.lead@saaradhigo.test'
ACTOR_PHONE = '+919741000502'
REASON = 'lost handset, identity verified by voice call'


@pytest.fixture(autouse=True)
def _mfa_on_and_guard_quiet(settings):
    settings.ADMIN_MFA_REQUIRED = True
    settings.OPS_LOGIN_MAX_ATTEMPTS = 50
    settings.OPS_LOGIN_MAX_ATTEMPTS_PER_IP = 500
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def actor(db):
    return User.objects.create_user(
        phone_number=ACTOR_PHONE, username=ACTOR_PHONE, email=ACTOR_EMAIL,
        full_name='Ops Lead', role='admin', is_staff=True,
    )


@pytest.fixture
def enrolled():
    """An operator with a confirmed authenticator and recovery codes: the
    state a lost phone leaves behind."""
    user = User.objects.create_user(
        phone_number=PHONE, username=PHONE, email=EMAIL, role='admin',
        is_staff=True, password=PASSWORD,
    )
    device, _uri = ops_mfa.enroll_totp(user)
    device.confirmed = True
    device.save(update_fields=['confirmed'])
    ops_mfa.issue_recovery_codes(user, count=2)
    return user, device


def _code(device):
    """A currently valid TOTP code from django-otp's own generator."""
    import time as _time

    from django_otp.oath import TOTP
    totp = TOTP(device.bin_key, device.step, device.t0, device.digits,
                device.drift)
    totp.time = _time.time()
    return totp.token()


def _run(**kwargs):
    out = StringIO()
    kwargs.setdefault('phone', PHONE)
    call_command('reset_operator_mfa', stdout=out, stderr=out, **kwargs)
    return out.getvalue()


def _reset(**kwargs):
    kwargs.setdefault('confirm', True)
    kwargs.setdefault('actor_email', ACTOR_EMAIL)
    kwargs.setdefault('reason', REASON)
    return _run(**kwargs)


def _login(**payload):
    payload.setdefault('email', EMAIL)
    payload.setdefault('password', PASSWORD)
    return APIClient().post(ADMIN_LOGIN, payload, format='json')


def _uri_lines(output):
    return [line for line in output.splitlines() if 'otpauth://' in line]


def _state(user):
    return (tuple(sorted(ops_mfa._totp_model().objects.filter(user=user)
                         .values_list('pk', 'confirmed'))),
            ops_mfa._static_models()[0].objects.filter(user=user).count())


# ---------------------------------------------------------------------------
# The gates: reason, actor, confirm
# ---------------------------------------------------------------------------

class TestTheGates:
    @pytest.mark.parametrize('reason', ['', '   ', 'lost phone', 'x' * (MIN_REASON_LENGTH - 1)])
    def test_without_a_real_reason_nothing_changes(self, enrolled, reason):
        user, _device = enrolled
        before = _state(user)
        rows = AdminAuditLog.objects.count()

        with pytest.raises(CommandError, match='--reason') as exc:
            _reset(reason=reason)

        assert str(MIN_REASON_LENGTH) in str(exc.value)
        assert 'Nothing was changed' in str(exc.value)
        assert _state(user) == before
        assert AdminAuditLog.objects.count() == rows
        assert ops_mfa.has_device(user)

    def test_a_reason_of_exactly_the_minimum_is_accepted(self, enrolled):
        _reset(reason='x' * MIN_REASON_LENGTH)

        assert AdminAuditLog.objects.filter(action='ops_mfa_reset').exists()

    def test_without_an_actor_nothing_changes(self, enrolled):
        user, _device = enrolled
        before = _state(user)

        with pytest.raises(CommandError, match='--actor-email'):
            _reset(actor_email='')

        assert _state(user) == before

    def test_without_confirm_it_only_reports_and_needs_neither(self, enrolled):
        user, _device = enrolled
        before = _state(user)
        rows = AdminAuditLog.objects.count()

        output = _run()

        assert 'Would remove' in output
        assert '--confirm' in output and '--reason' in output
        assert _state(user) == before
        assert AdminAuditLog.objects.count() == rows
        assert _uri_lines(output) == []

    def test_an_account_with_no_confirmed_factor_has_nothing_to_reset(self):
        User.objects.create_user(phone_number=PHONE, username=PHONE, email=EMAIL,
                                 role='admin', is_staff=True, password=PASSWORD)
        rows = AdminAuditLog.objects.count()

        output = _reset()

        assert 'no confirmed factor' in output
        assert 'enroll_operator_mfa' in output
        assert AdminAuditLog.objects.count() == rows

    def test_an_unknown_phone_is_refused(self):
        with pytest.raises(CommandError, match='No account'):
            _reset(phone='+919741000599')


# ---------------------------------------------------------------------------
# What a reset does
# ---------------------------------------------------------------------------

class TestTheReset:
    def test_every_confirmed_factor_goes_and_a_fresh_enrolment_is_pending(self, enrolled):
        user, old = enrolled
        assert ops_mfa.has_device(user)

        output = _reset()

        assert not ops_mfa.has_device(user)
        assert not ops_mfa._totp_model().objects.filter(pk=old.pk).exists()
        assert ops_mfa._static_models()[0].objects.filter(user=user).count() == 0
        pending = ops_mfa.pending_totp_device(user)
        assert pending is not None and pending.confirmed is False
        assert 'Removed 2 factor(s)' in output

    def test_the_provisioning_uri_is_printed_once_and_is_the_pending_device(self, enrolled):
        user, _old = enrolled

        output = _reset()

        uris = _uri_lines(output)
        assert len(uris) == 1
        assert uris[0].startswith('Authenticator setup: otpauth://')
        pending = ops_mfa.pending_totp_device(user)
        assert pending.config_url.split('?')[0] in uris[0]

    def test_the_two_rows_name_the_actor_and_the_reason_and_not_the_secret(self, enrolled, actor):
        user, _old = enrolled

        output = _reset()

        reset = AdminAuditLog.objects.get(action='ops_mfa_reset')
        assert reset.target_id == str(user.pk)
        assert reset.before == {'devices': 2}
        assert reset.after == {
            'devices': 0, 'factors_removed': 2, 'recovery_codes_removed': 2,
            'pending_totp_device_created': True, 'sessions_revoked': True,
        }
        assert reset.reason == REASON
        revoked = AdminAuditLog.objects.get(action='operator_credential_changed')
        assert revoked.target_id == str(user.pk)
        assert 'reset_operator_mfa' in revoked.reason
        for row in (reset, revoked):
            assert row.actor_id == actor.pk
            assert row.actor_email == ACTOR_EMAIL
            assert row.actor_label == f'{ACTOR_EMAIL} via reset_operator_mfa'
            blob = f'{row.before}{row.after}{row.reason}{row.actor_label}'
            assert 'otpauth' not in blob
            assert PHONE not in blob
        secret = _uri_lines(output)[0].split('secret=')[1].split('&')[0]
        assert secret not in f'{reset.after}{reset.reason}{revoked.after}'


# ---------------------------------------------------------------------------
# Afterwards: nothing but enrolment
# ---------------------------------------------------------------------------

class TestAfterwardsTheAccountCanOnlyEnrol:
    def test_a_password_alone_yields_no_token(self, enrolled):
        _reset()

        r = _login()

        assert r.status_code == 401
        body = r.json()
        assert body['error']['code'] == 'AUTH_MFA_REQUIRED'
        assert 'token' not in str(body.get('data'))

    def test_the_old_session_cannot_be_renewed(self, enrolled):
        user, _old = enrolled
        refresh = RefreshToken.for_user(user)
        assert APIClient().post(REFRESH, {'refresh_token': str(refresh)},
                                format='json').status_code == 200

        _reset()

        r = APIClient().post(REFRESH, {'refresh_token': str(refresh)}, format='json')
        assert r.status_code != 200
        assert 'token' not in str(r.json().get('data'))

    def test_a_code_from_the_lost_device_is_refused(self, enrolled):
        _user, old = enrolled
        code = _code(old)

        _reset()

        r = _login(mfa_code=code)
        assert r.status_code == 401
        assert r.json()['error']['code'] == 'AUTH_INVALID_CREDENTIALS'

    def test_a_wrong_code_does_not_confirm_the_new_device(self, enrolled):
        user, _old = enrolled
        _reset()

        r = _login(mfa_code='000000')

        assert r.status_code == 401
        assert ops_mfa.pending_totp_device(user).confirmed is False

    def test_the_first_valid_code_from_the_new_device_confirms_it_and_signs_in(self, enrolled):
        user, _old = enrolled
        _reset()
        pending = ops_mfa.pending_totp_device(user)

        r = _login(mfa_code=_code(pending))

        assert r.status_code == 200, r.content.decode()
        assert r.json()['data']['token']
        pending.refresh_from_db()
        assert pending.confirmed is True
        assert ops_mfa.has_device(user)
        assert AdminAuditLog.objects.filter(
            action='ops_mfa_enrolled', after__confirmed_at='first_sign_in').exists()

    def test_from_then_on_the_new_device_is_required(self, enrolled):
        user, _old = enrolled
        _reset()
        pending = ops_mfa.pending_totp_device(user)
        assert _login(mfa_code=_code(pending)).status_code == 200

        r = _login()

        assert r.status_code == 401
        assert r.json()['error']['code'] == 'AUTH_MFA_REQUIRED'

    def test_in_an_enforced_environment_the_same_path_applies(self, enrolled, settings):
        settings.OPS_MFA_ENFORCED = True
        user, _old = enrolled
        _reset()

        assert _login().json()['error']['code'] == 'AUTH_MFA_REQUIRED'
        pending = ops_mfa.pending_totp_device(user)
        assert _login(mfa_code=_code(pending)).status_code == 200


class TestWithTheSwitchOff:
    """QA today: ADMIN_MFA_REQUIRED=false. The reset is the same reset; the
    pending device waits for the switch."""

    @pytest.fixture(autouse=True)
    def _off(self, settings):
        settings.ADMIN_MFA_REQUIRED = False

    def test_the_reset_still_removes_the_factors_and_leaves_a_pending_device(self, enrolled):
        user, _old = enrolled

        _reset()

        assert not ops_mfa.has_device(user)
        assert ops_mfa.pending_totp_device(user) is not None
        assert AdminAuditLog.objects.filter(action='ops_mfa_reset').exists()

    def test_the_password_signs_in_and_the_pending_device_is_ignored(self, enrolled):
        user, _old = enrolled
        _reset()

        r = _login()

        assert r.status_code == 200, r.content.decode()
        assert ops_mfa.pending_totp_device(user).confirmed is False

    def test_the_old_session_is_still_revoked(self, enrolled):
        user, _old = enrolled
        refresh = RefreshToken.for_user(user)

        _reset()

        assert APIClient().post(REFRESH, {'refresh_token': str(refresh)},
                                format='json').status_code != 200
