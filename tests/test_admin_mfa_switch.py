"""ADMIN_MFA_REQUIRED is the master switch for operator MFA, and off means off.

Off: admin login is email + password only. No code is asked of an enrolled
account, a pending authenticator is left alone (not confirmed, not
required), and the console session needs no factor. Break-glass still needs
a reason: that is a different control and the switch does not reach it.

On (the default): everything behaves as before; `tests/test_operator_mfa.py`
and `tests/test_create_operator.py` cover that and keep passing.

Production cannot be off: `tests/test_production_boot_guards.py` proves the
boot refuses it.

Also here: `set_operator_password`, the audited shell path that makes an
account usable with a password alone (new generated password, pending
authenticator removed, forced change cleared).
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
EMAIL = 'switch-op@saaradhigo.test'
PHONE = '+919741000301'
PASSWORD = 'Granite-Orchard-5520-chosen'


@pytest.fixture(autouse=True)
def _quiet(settings):
    settings.OPS_LOGIN_MAX_ATTEMPTS = 50
    settings.OPS_LOGIN_MAX_ATTEMPTS_PER_IP = 500
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def operator():
    return User.objects.create_user(
        phone_number=PHONE, username=PHONE, email=EMAIL, role='admin',
        is_staff=True, password=PASSWORD,
    )


def _login(**payload):
    payload.setdefault('email', EMAIL)
    payload.setdefault('password', PASSWORD)
    return APIClient().post(ADMIN_LOGIN, payload, format='json')


def _enrol(user):
    device, _uri = ops_mfa.enroll_totp(user)
    device.confirmed = True
    device.save(update_fields=['confirmed'])
    return device


# ---------------------------------------------------------------------------
# Off
# ---------------------------------------------------------------------------

class TestSwitchedOff:
    @pytest.fixture(autouse=True)
    def _off(self, settings):
        settings.ADMIN_MFA_REQUIRED = False

    def test_an_enrolled_operator_signs_in_with_password_alone(self, operator):
        _enrol(operator)

        r = _login()

        assert r.status_code == 200, r.content.decode()
        assert r.json()['data']['token']
        assert AdminAuditLog.objects.filter(action='ops_api_login',
                                            after__mfa=False).exists()

    def test_even_when_the_environment_enforces_mfa(self, operator, settings):
        settings.OPS_MFA_ENFORCED = True
        _enrol(operator)

        assert _login().status_code == 200

    def test_a_pending_authenticator_is_neither_required_nor_confirmed(self, operator):
        device, _ = ops_mfa.enroll_totp(operator)

        r = _login()

        assert r.status_code == 200, r.content.decode()
        device.refresh_from_db()
        assert device.confirmed is False
        assert not AdminAuditLog.objects.filter(action='ops_mfa_enrolled').exists()

    def test_a_code_sent_anyway_is_ignored_not_rejected(self, operator):
        _enrol(operator)

        assert _login(mfa_code='000000').status_code == 200

    def test_the_console_session_needs_no_factor(self, operator, rf):
        _enrol(operator)
        request = rf.get('/')
        request.user = operator

        assert ops_mfa.mfa_required_for(operator) is False
        assert ops_mfa.console_access_allowed(request) is True

    def test_break_glass_still_requires_a_reason(self, operator):
        operator.is_break_glass = True
        operator.save(update_fields=['is_break_glass'])

        r = _login()

        assert r.status_code == 401, r.content.decode()
        assert r.json()['error']['code'] == 'AUTH_BREAK_GLASS_REASON_REQUIRED'
        ok = _login(reason='QA rehearsal of the emergency account, ticket 0')
        assert ok.status_code == 200, ok.content.decode()

    def test_a_wrong_password_is_still_refused(self, operator):
        assert _login(password='not-it').status_code == 401

    def test_a_non_operator_is_still_refused(self):
        User.objects.create_user(phone_number='+919741000302', username='+919741000302',
                                 email='rider@saaradhigo.test', role='rider',
                                 password=PASSWORD)

        assert _login(email='rider@saaradhigo.test').status_code in (401, 403)


# ---------------------------------------------------------------------------
# On (default): unchanged behaviour, spot-checked
# ---------------------------------------------------------------------------

class TestSwitchedOnByDefault:
    def test_the_default_is_on(self):
        from django.conf import settings as live

        assert getattr(live, 'ADMIN_MFA_REQUIRED', True) is True
        assert ops_mfa.mfa_enabled() is True

    def test_an_enrolled_operator_is_asked_for_a_code(self, operator):
        _enrol(operator)

        r = _login()

        assert r.status_code == 401
        assert r.json()['error']['code'] == 'AUTH_MFA_REQUIRED'

    def test_a_pending_authenticator_is_asked_for_its_code(self, operator):
        ops_mfa.enroll_totp(operator)

        r = _login()

        assert r.status_code == 401
        assert r.json()['error']['code'] == 'AUTH_MFA_REQUIRED'


# ---------------------------------------------------------------------------
# set_operator_password
# ---------------------------------------------------------------------------

def _run(**kwargs):
    out = StringIO()
    kwargs.setdefault('email', EMAIL)
    call_command('set_operator_password', stdout=out, **kwargs)
    return out.getvalue()


def _password(output):
    for line in output.splitlines():
        if line.startswith('Password: '):
            return line.split(': ', 1)[1].strip()
    raise AssertionError(f'no password line in:\n{output}')


class TestSetOperatorPassword:
    def test_a_strong_password_is_generated_printed_once_and_works(self, operator):
        output = _run()

        password = _password(output)
        assert len(password) >= 20
        assert output.count(password) == 1
        assert User.objects.get(pk=operator.pk).check_password(password)
        assert not User.objects.get(pk=operator.pk).check_password(PASSWORD)

    def test_it_signs_in_with_the_switch_off(self, operator, settings):
        settings.ADMIN_MFA_REQUIRED = False
        password = _password(_run())

        assert _login(password=password).status_code == 200

    def test_the_forced_change_is_cleared_only_when_asked(self, operator):
        operator.must_change_password = True
        operator.save(update_fields=['must_change_password'])

        _run()
        assert User.objects.get(pk=operator.pk).must_change_password is True

        _run(clear_forced_change=True)
        assert User.objects.get(pk=operator.pk).must_change_password is False

    def test_pending_authenticators_are_removed_only_when_asked(self, operator):
        ops_mfa.enroll_totp(operator)
        confirmed = _enrol(operator)

        _run()
        assert ops_mfa.pending_totp_device(operator) is not None

        _run(remove_pending_mfa=True)
        assert ops_mfa.pending_totp_device(operator) is None
        confirmed.refresh_from_db()
        assert confirmed.confirmed is True  # never touched

    def test_every_run_is_audited_without_the_password(self, operator):
        operator.must_change_password = True
        operator.save(update_fields=['must_change_password'])
        ops_mfa.enroll_totp(operator)

        password = _password(_run(remove_pending_mfa=True, clear_forced_change=True))

        row = AdminAuditLog.objects.filter(action='ops_password_set').get()
        assert row.target_id == str(operator.pk)
        assert row.before == {'must_change_password': True, 'pending_totp_devices': 1}
        assert row.after['must_change_password'] is False
        assert row.after['pending_totp_devices_removed'] == 1
        assert password not in f'{row.before}{row.after}{row.reason}{row.actor_label}'

    def test_an_unknown_address_is_refused(self):
        with pytest.raises(CommandError, match='No account'):
            _run(email='nobody@saaradhigo.test')

    def test_a_non_operator_is_refused_untouched(self):
        rider = User.objects.create_user(phone_number='+919741000303', username='+919741000303',
                                         email='rider2@saaradhigo.test', role='rider',
                                         password=PASSWORD)

        with pytest.raises(CommandError, match='not an operator'):
            _run(email='rider2@saaradhigo.test')
        assert User.objects.get(pk=rider.pk).check_password(PASSWORD)
        assert not AdminAuditLog.objects.exists()
