"""Recovery codes for operators, end to end (go-live blocker #6).

THE DEFECT
----------
The console's "use a recovery code" step posted the code as `mfa_code`, and
the sign-in that confirmed a pending authenticator issued no recovery codes
at all ("there is no screen to show them"). So an operator enrolled at the
keyboard -- which is every operator the console onboards -- held nothing
to fall back on, and a lost authenticator was a lockout until somebody
with shell access ran `reset_operator_mfa`.

WHAT IS PROVED HERE
-------------------
  * the codes are issued ONCE, with the first token after the authenticator
    is confirmed, and never again -- including when that first sign-in is
    bounced by the forced password change, where a set issued on the 401
    would have been a set nobody saw;
  * a recovery code signs the operator in when the authenticator is gone,
    and the same code is refused the second time;
  * the response says how many remain and flags a low count;
  * regenerating needs a LIVE authenticator code (a recovery code is
    refused), replaces the set so the old codes are dead, and returns the
    new set once;
  * the status endpoint says what the console needs and nothing more;
  * with `ADMIN_MFA_REQUIRED=false` nothing is issued and the endpoints
    answer sensibly;
  * no audit row ever contains a code, used or unused;
  * a non-operator is refused with 403, and the structural guard in
    `test_admin_namespace_is_gated.py` stays green with the two
    self-service routes allowlisted.

As in `test_operator_mfa.py`, no cryptography is tested: codes come from
django-otp's own generator, and what is checked is policy.
"""

import json

from conftest import grant_legacy_equivalent
import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from base import ops_mfa
from servers.admin_audit.models import AdminAuditLog

User = get_user_model()

pytestmark = pytest.mark.django_db

LOGIN = '/api/v1/auth/admin/login/'
STATUS = '/api/v1/auth/admin/mfa/status/'
REGENERATE = '/api/v1/auth/admin/mfa/recovery-codes/regenerate/'
KYC_QUEUE = '/api/v1/driver/admin/'

EMAIL = 'recovery-op@saaradhigo.test'
PHONE = '+919741000701'
PASSWORD = 'Harbour-Lantern-7731-chosen'
RIDER_PHONE = '+919741000702'
RIDER_PASSWORD = 'Rider-Passphrase-9981-long'


@pytest.fixture(autouse=True)
def enforced(settings):
    """MFA on and enforced; the login guard and django-otp's own per-device
    throttle out of the way, so a wrong code followed by a right one measures
    policy rather than a back-off timer (`test_operator_mfa` covers the
    throttle on its own)."""
    settings.OPS_MFA_ENFORCED = True
    settings.ADMIN_MFA_REQUIRED = True
    settings.OPS_LOGIN_MAX_ATTEMPTS = 50
    settings.OPS_LOGIN_MAX_ATTEMPTS_PER_IP = 500
    settings.OTP_TOTP_THROTTLE_FACTOR = 0
    settings.OTP_STATIC_THROTTLE_FACTOR = 0
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def operator():
    user = User.objects.create_user(
        phone_number=PHONE, username=PHONE, email=EMAIL, role='admin',
        is_staff=True, ops_role='admin', password=PASSWORD,
    )
    grant_legacy_equivalent(user)
    return user


@pytest.fixture
def pending(operator):
    """An operator whose authenticator was created in a shell and not yet
    confirmed: the state `create_operator --enrol-mfa` leaves behind."""
    device, _uri = ops_mfa.enroll_totp(operator)
    return operator, device


@pytest.fixture
def enrolled(operator):
    """A confirmed authenticator and NO recovery codes yet."""
    device, _uri = ops_mfa.enroll_totp(operator)
    device.confirmed = True
    device.save(update_fields=['confirmed'])
    return operator, device


@pytest.fixture
def with_codes(enrolled):
    """Enrolled, holding a full set of recovery codes."""
    operator, device = enrolled
    codes = ops_mfa.issue_recovery_codes(operator)
    return operator, device, codes


@pytest.fixture
def rider():
    return User.objects.create_user(
        phone_number=RIDER_PHONE, username=RIDER_PHONE, role='rider',
        password=RIDER_PASSWORD,
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _code(device):
    """A currently-valid code from django-otp's own generator.

    `last_t` is reset first: a TOTP device refuses a code from a time step it
    has already accepted, and a test that signs in and then regenerates
    within the same 30 seconds would otherwise be measuring replay
    protection. Resetting it is "waiting for the next code" without the
    wait.
    """
    import time as _time

    from django_otp.oath import TOTP

    device.refresh_from_db()
    device.last_t = -1
    device.save(update_fields=['last_t'])
    totp = TOTP(device.bin_key, device.step, device.t0, device.digits,
                device.drift)
    totp.time = _time.time()
    return totp.token()


def _login(**extra):
    body = {'email': EMAIL, 'password': PASSWORD}
    body.update(extra)
    return APIClient().post(LOGIN, body, format='json')


def _bearer(token):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    return c


def _client_for(user):
    return _bearer(str(AccessToken.for_user(user)))


def _data(resp):
    return resp.json().get('data') or {}


def _error(resp):
    return resp.json().get('error') or {}


def _audit_text():
    """Every audit row, as text, so a code can be searched for anywhere in it."""
    rows = AdminAuditLog.objects.all().values(
        'action', 'before', 'after', 'reason', 'actor_label')
    return json.dumps(list(rows), default=str)


def _assert_no_code_in_audit(codes):
    text = _audit_text()
    for code in codes:
        assert code not in text, f'audit trail contains recovery code {code!r}'


# ===========================================================================
# 1. Issued once, with the first token
# ===========================================================================

class TestIssuedAtFirstSignIn:
    def test_confirming_the_pending_authenticator_returns_the_codes_once(self, pending):
        operator, device = pending

        first = _login(mfa_code=_code(device))

        assert first.status_code == 200, first.content[:300]
        data = _data(first)
        codes = data['recovery_codes']
        assert isinstance(codes, list) and len(codes) == ops_mfa.RECOVERY_CODE_COUNT
        assert all(isinstance(c, str) and c for c in codes)
        assert len(set(codes)) == len(codes)
        assert data['recovery_codes_notice']
        assert data['recovery_codes_remaining'] == ops_mfa.RECOVERY_CODE_COUNT
        assert data['recovery_codes_low'] is False
        assert data['token']

        # Issued: the audit says how many, and never which.
        row = AdminAuditLog.objects.get(action='ops_recovery_codes_issued')
        assert row.after['recovery_codes_issued'] == ops_mfa.RECOVERY_CODE_COUNT
        assert str(row.target_id) == str(operator.pk)
        _assert_no_code_in_audit(codes)

        # Never again.
        second = _login(mfa_code=_code(device))
        assert second.status_code == 200, second.content[:300]
        assert 'recovery_codes' not in _data(second)
        assert 'recovery_codes_notice' not in _data(second)
        assert AdminAuditLog.objects.filter(action='ops_recovery_codes_issued').count() == 1

    def test_a_first_sign_in_bounced_by_the_password_change_still_gets_its_codes(self, pending):
        """The device is confirmed on the FIRST request, which answers 401
        (choose a new password) and carries no token. If the codes were
        issued there they would be issued into a response nobody reads.
        They must arrive with the token, on the resend."""
        operator, device = pending
        operator.must_change_password = True
        operator.save(update_fields=['must_change_password'])

        bounced = _login(mfa_code=_code(device))
        assert bounced.status_code == 401
        assert _error(bounced)['code'] == 'AUTH_PASSWORD_CHANGE_REQUIRED'
        assert 'recovery_codes' not in (bounced.json().get('data') or {})
        device.refresh_from_db()
        assert device.confirmed is True
        assert not AdminAuditLog.objects.filter(action='ops_recovery_codes_issued').exists()

        new_password = 'Chosen-By-The-Operator-4410'
        done = _login(mfa_code=_code(device), new_password=new_password)
        assert done.status_code == 200, done.content[:300]
        assert len(_data(done)['recovery_codes']) == ops_mfa.RECOVERY_CODE_COUNT
        assert _data(done)['token']

    def test_an_operator_enrolled_before_this_existed_is_handed_a_set_on_the_next_sign_in(self, enrolled):
        """Confirmed at sign-in under the old code, so holding no codes at all.
        That is exactly the operator the blocker was about."""
        operator, device = enrolled
        assert ops_mfa.recovery_codes_remaining(operator) == 0

        resp = _login(mfa_code=_code(device))

        assert resp.status_code == 200
        assert len(_data(resp)['recovery_codes']) == ops_mfa.RECOVERY_CODE_COUNT
        assert 'recovery_codes' not in _data(_login(mfa_code=_code(device)))

    def test_a_set_with_every_code_spent_is_not_silently_replaced(self, enrolled):
        """Signing in must not be a way to get new codes; regenerating is,
        and it asks for the authenticator."""
        operator, device = enrolled
        ops_mfa.issue_recovery_codes(operator, count=1)
        from django_otp.plugins.otp_static.models import StaticToken
        StaticToken.objects.filter(device__user=operator).delete()

        resp = _login(mfa_code=_code(device))

        assert resp.status_code == 200
        assert 'recovery_codes' not in _data(resp)


# ===========================================================================
# 2. Signing in with a recovery code
# ===========================================================================

class TestSignInWithARecoveryCode:
    def test_a_recovery_code_signs_in_when_the_authenticator_is_lost(self, with_codes):
        operator, _device, codes = with_codes

        resp = _login(recovery_code=codes[0])

        assert resp.status_code == 200, resp.content[:300]
        data = _data(resp)
        assert data['recovery_codes_remaining'] == len(codes) - 1
        assert data['recovery_codes_low'] is False
        assert 'recovery_codes' not in data
        # The token is a real operator token.
        assert _bearer(data['token']).get(KYC_QUEUE).status_code == 200

        row = AdminAuditLog.objects.get(action='ops_recovery_code_used')
        assert row.after == {'recovery_codes_remaining': len(codes) - 1}
        login_row = AdminAuditLog.objects.get(action='ops_api_login')
        assert login_row.after['recovery_code_used'] is True
        _assert_no_code_in_audit(codes)

    def test_the_same_code_is_refused_the_second_time(self, with_codes):
        _operator, _device, codes = with_codes
        assert _login(recovery_code=codes[0]).status_code == 200

        again = _login(recovery_code=codes[0])

        assert again.status_code == 401
        assert _error(again)['code'] == 'AUTH_INVALID_CREDENTIALS'
        assert 'token' not in _data(again)
        # A different unused code still works: it was the code, not the account.
        assert _login(recovery_code=codes[1]).status_code == 200

    def test_a_wrong_recovery_code_is_the_same_refusal_as_a_wrong_password(self, with_codes):
        resp = _login(recovery_code='notacode')

        assert resp.status_code == 401
        assert _error(resp)['code'] == 'AUTH_INVALID_CREDENTIALS'

    def test_the_low_flag_appears_at_two_remaining(self, enrolled):
        operator, _device = enrolled
        codes = ops_mfa.issue_recovery_codes(operator, count=4)

        first = _data(_login(recovery_code=codes[0]))
        assert (first['recovery_codes_remaining'], first['recovery_codes_low']) == (3, False)

        second = _data(_login(recovery_code=codes[1]))
        assert (second['recovery_codes_remaining'], second['recovery_codes_low']) == (2, True)

        third = _data(_login(recovery_code=codes[2]))
        assert (third['recovery_codes_remaining'], third['recovery_codes_low']) == (1, True)

        last = _data(_login(recovery_code=codes[3]))
        assert (last['recovery_codes_remaining'], last['recovery_codes_low']) == (0, True)

    def test_pasted_formatting_does_not_matter(self, with_codes):
        """Upper-case, a hyphen in the middle, spaces around: what a code
        looks like after a text file and a clipboard have had it."""
        _operator, _device, codes = with_codes
        code = codes[0]
        pasted = f'  {code[:4].upper()}-{code[4:].upper()} '

        assert _login(recovery_code=pasted).status_code == 200

    def test_a_recovery_code_under_mfa_code_still_works_and_is_counted(self, with_codes):
        """Backward compatibility: the console sent it that way before the
        field existed. Counted the same, audited the same."""
        _operator, _device, codes = with_codes

        resp = _login(mfa_code=codes[0])

        assert resp.status_code == 200, resp.content[:300]
        assert _data(resp)['recovery_codes_remaining'] == len(codes) - 1
        assert AdminAuditLog.objects.filter(action='ops_recovery_code_used').exists()

    def test_an_authenticator_code_sent_as_a_recovery_code_is_refused(self, with_codes):
        """The field says what it carries; the recovery field is checked
        against the recovery set only."""
        _operator, device, _codes = with_codes

        resp = _login(recovery_code=_code(device))

        assert resp.status_code == 401

    def test_a_sign_in_with_the_authenticator_carries_no_count(self, with_codes):
        _operator, device, _codes = with_codes

        data = _data(_login(mfa_code=_code(device)))

        assert data['token']
        assert 'recovery_codes_remaining' not in data
        assert 'recovery_codes' not in data

    def test_a_recovery_code_cannot_confirm_a_pending_authenticator(self, pending):
        """There are no recovery codes before the authenticator is confirmed,
        so a recovery code alone is answered like no code at all -- a
        non-terminal 'send the authenticator code', not a failure."""
        resp = _login(recovery_code='abcdefgh')

        assert resp.status_code == 401
        assert _error(resp)['code'] == 'AUTH_MFA_REQUIRED'

    def test_the_login_guard_counts_recovery_failures(self, with_codes, settings):
        settings.OPS_LOGIN_MAX_ATTEMPTS = 3
        _operator, _device, codes = with_codes

        for _ in range(3):
            assert _login(recovery_code='wrongwrong').status_code == 401

        # Locked: even the right code is refused now, identically.
        locked = _login(recovery_code=codes[0])
        assert locked.status_code == 401
        assert _error(locked)['code'] == 'AUTH_INVALID_CREDENTIALS'


# ===========================================================================
# 3. Regenerate
# ===========================================================================

class TestRegenerate:
    def _signed_in(self, device):
        resp = _login(mfa_code=_code(device))
        assert resp.status_code == 200, resp.content[:300]
        return _bearer(_data(resp)['token'])

    def test_a_live_authenticator_code_replaces_the_set_and_returns_it_once(self, with_codes):
        operator, device, old = with_codes
        client = self._signed_in(device)

        resp = client.post(REGENERATE, {'mfa_code': _code(device)}, format='json')

        assert resp.status_code == 200, resp.content[:300]
        data = _data(resp)
        new = data['recovery_codes']
        assert len(new) == ops_mfa.RECOVERY_CODE_COUNT
        assert not set(new) & set(old)
        assert data['recovery_codes_notice']
        assert data['recovery_codes_remaining'] == ops_mfa.RECOVERY_CODE_COUNT
        assert data['recovery_codes_low'] is False

        # The old set is dead; the new set works.
        assert _login(recovery_code=old[0]).status_code == 401
        assert _login(recovery_code=new[0]).status_code == 200

        row = AdminAuditLog.objects.get(action='ops_recovery_codes_regenerated')
        assert row.before == {'recovery_codes_remaining': len(old)}
        assert row.after['recovery_codes_issued'] == ops_mfa.RECOVERY_CODE_COUNT
        assert str(row.target_id) == str(operator.pk)
        _assert_no_code_in_audit(old + new)

        # Once: nothing afterwards hands the set out again.
        assert 'recovery_codes' not in _data(client.get(STATUS))

    def test_it_needs_a_code(self, with_codes):
        _operator, device, _codes = with_codes
        client = self._signed_in(device)

        resp = client.post(REGENERATE, {}, format='json')

        assert resp.status_code == 400
        assert _error(resp)['code'] == 'AUTH_MFA_CODE_REQUIRED'

    def test_a_wrong_code_changes_nothing_and_does_not_end_the_session(self, with_codes):
        """403, not 401: the console reads a 401 on a request that carried
        its token as 'the session ended' and signs the operator out."""
        operator, device, codes = with_codes
        client = self._signed_in(device)

        resp = client.post(REGENERATE, {'mfa_code': '000000'}, format='json')

        assert resp.status_code == 403
        assert _error(resp)['code'] == 'AUTH_INVALID_CODE'
        assert ops_mfa.recovery_codes_remaining(operator) == len(codes)
        assert _login(recovery_code=codes[0]).status_code == 200
        assert not AdminAuditLog.objects.filter(action='ops_recovery_codes_regenerated').exists()

    def test_a_recovery_code_cannot_mint_more_recovery_codes(self, with_codes):
        """Valid at sign-in, refused here: a recovery code that regenerated
        the set would never run out."""
        operator, device, codes = with_codes
        client = self._signed_in(device)

        resp = client.post(REGENERATE, {'mfa_code': codes[0]}, format='json')

        assert resp.status_code == 403
        assert _error(resp)['code'] == 'AUTH_INVALID_CODE'
        # And it was not consumed by the attempt.
        assert ops_mfa.recovery_codes_remaining(operator) == len(codes)

    def test_the_token_alone_is_not_enough_and_guesses_are_rate_limited(self, with_codes, settings):
        settings.OPS_LOGIN_MAX_ATTEMPTS = 2
        _operator, device, _codes = with_codes
        client = self._signed_in(device)

        for _ in range(2):
            assert client.post(REGENERATE, {'mfa_code': '111111'}, format='json').status_code == 403

        # Locked for this purpose: the right code is refused the same way.
        locked = client.post(REGENERATE, {'mfa_code': _code(device)}, format='json')
        assert locked.status_code == 403
        assert _error(locked)['code'] == 'AUTH_INVALID_CODE'
        # ...and the lock is per purpose: signing in is unaffected.
        assert _login(mfa_code=_code(device)).status_code == 200

    def test_without_a_confirmed_authenticator_there_is_nothing_to_regenerate(self, pending):
        operator, _device = pending

        resp = _client_for(operator).post(REGENERATE, {'mfa_code': '123456'}, format='json')

        assert resp.status_code == 409
        assert _error(resp)['code'] == 'MFA_NOT_ENROLLED'

    def test_anonymous_is_refused(self):
        assert APIClient().post(REGENERATE, {'mfa_code': '123456'}, format='json').status_code == 401
        assert APIClient().get(STATUS).status_code == 401

    def test_a_non_operator_is_refused_with_403(self, rider):
        client = _client_for(rider)

        regen = client.post(REGENERATE, {'mfa_code': '123456'}, format='json')
        status = client.get(STATUS)

        assert regen.status_code == 403
        assert _error(regen)['code'] == 'NOT_AN_OPERATOR'
        assert status.status_code == 403
        assert _error(status)['code'] == 'NOT_AN_OPERATOR'


# ===========================================================================
# 4. Status
# ===========================================================================

class TestStatus:
    def test_enrolled_with_a_full_set(self, with_codes):
        operator, _device, codes = with_codes

        resp = _client_for(operator).get(STATUS)

        assert resp.status_code == 200
        assert _data(resp) == {
            'mfa_enabled': True,
            'enrolled': True,
            'pending': False,
            'recovery_codes_remaining': len(codes),
            'recovery_codes_low': False,
        }

    def test_pending_only(self, pending):
        operator, _device = pending

        data = _data(_client_for(operator).get(STATUS))

        assert data['enrolled'] is False
        assert data['pending'] is True
        assert data['recovery_codes_remaining'] == 0

    def test_low_after_using_codes(self, enrolled):
        operator, _device = enrolled
        codes = ops_mfa.issue_recovery_codes(operator, count=3)
        _login(recovery_code=codes[0])

        data = _data(_client_for(operator).get(STATUS))

        assert data['recovery_codes_remaining'] == 2
        assert data['recovery_codes_low'] is True

    def test_it_never_returns_a_code_or_a_secret(self, with_codes):
        operator, _device, codes = with_codes

        text = _client_for(operator).get(STATUS).content.decode()

        for code in codes:
            assert code not in text
        assert 'otpauth' not in text


# ===========================================================================
# 5. The switch off
# ===========================================================================

class TestWithTheSwitchOff:
    @pytest.fixture(autouse=True)
    def _off(self, settings):
        settings.ADMIN_MFA_REQUIRED = False

    def test_nothing_is_issued_at_sign_in(self, pending):
        resp = _login()

        assert resp.status_code == 200, resp.content[:300]
        assert 'recovery_codes' not in _data(resp)
        assert not AdminAuditLog.objects.filter(action='ops_recovery_codes_issued').exists()

    def test_status_says_the_switch_is_off(self, with_codes):
        operator, _device, codes = with_codes

        data = _data(_client_for(operator).get(STATUS))

        assert data['mfa_enabled'] is False
        assert data['enrolled'] is True
        assert data['recovery_codes_remaining'] == len(codes)

    def test_regenerate_declines_rather_than_issuing_codes_nothing_checks(self, with_codes):
        operator, device, codes = with_codes

        resp = _client_for(operator).post(REGENERATE, {'mfa_code': _code(device)}, format='json')

        assert resp.status_code == 409
        assert _error(resp)['code'] == 'MFA_DISABLED'
        assert ops_mfa.recovery_codes_remaining(operator) == len(codes)


# ===========================================================================
# 6. The break-glass reset removes the codes, and says so
# ===========================================================================

def test_reset_operator_mfa_reports_the_recovery_codes_it_removed(with_codes):
    from io import StringIO

    from django.core.management import call_command

    operator, _device, codes = with_codes
    # The actor must be a real operator account (`admin_audit.actor_option`).
    User.objects.create_user(
        phone_number='+919741000703', username='+919741000703',
        email='ops.lead@saaradhigo.test', role='admin', is_staff=True,
        password='Actor-Passphrase-2210-long',
    )
    out = StringIO()

    call_command('reset_operator_mfa', '--phone', PHONE, '--confirm',
                 '--actor-email', 'ops.lead@saaradhigo.test',
                 '--reason', 'lost handset, identity verified by voice call',
                 stdout=out)

    text = out.getvalue()
    assert f'Recovery codes removed too: {len(codes)} unused code(s)' in text
    assert ops_mfa.recovery_codes_remaining(operator) == 0
    assert _login(recovery_code=codes[0]).status_code == 401
    row = AdminAuditLog.objects.get(action='ops_mfa_reset')
    assert row.after['recovery_codes_removed'] == len(codes)
    _assert_no_code_in_audit(codes)


# ===========================================================================
# 7. The structural guard knows about the two routes
# ===========================================================================

def test_the_self_service_routes_are_allowlisted_with_a_reason():
    from test_admin_namespace_is_gated import ALLOWLIST, ungated_admin_routes

    for route in ('api/v1/auth/admin/mfa/status/',
                  'api/v1/auth/admin/mfa/recovery-codes/regenerate/'):
        assert route in ALLOWLIST
        assert 'self-service' in ALLOWLIST[route].lower()
    assert ungated_admin_routes() == {}
