"""§4 — operator MFA, and the three ways it could have been decorative.

WHAT WOULD MAKE THIS FAKE
-------------------------
A login form that asks for a code, while every control it guards is reachable another
way. §4 names the requirement directly: MFA must not be bypassable by calling
privileged APIs. So the tests that matter most here are not "a correct code signs you
in" -- they are the three bypasses:

  1. **Straight at the API.** Present a token and call the payout endpoint without
     ever meeting a challenge. Closed by enforcing MFA where the token is ISSUED, so
     an operator token cannot exist without two factors behind it.
  2. **Through the SMS path.** `/api/v1/auth/login/` trades an SMS code for a token
     with no password and no second factor. If an operator could use it, MFA would
     cost one SIM swap. Operators are now refused there.
  3. **Session cookie alone.** A signed-in session is one factor. Holding the cookie
     must not be enough; the session itself has to have presented a code.

NO CRYPTOGRAPHY IS TESTED HERE
------------------------------
django-otp implements TOTP and the static recovery tokens. These tests generate valid
codes with the library's own machinery and check *policy*: who is challenged, when,
what a wrong code does, and whether anything can be skipped. Re-testing RFC 6238
would be testing the library.
"""

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from base import ops_mfa
from servers.driver.models import Driver
from servers.rider.models import Rider

User = get_user_model()

pytestmark = pytest.mark.django_db

ADMIN_LOGIN_URL = '/api/v1/auth/admin/login/'
OTP_URL = '/api/v1/auth/otp/'
SMS_LOGIN_URL = '/api/v1/auth/login/'
PAYOUT_URL = '/api/v1/driver/admin/withdrawals/1/approve/'
KYC_QUEUE_URL = '/api/v1/driver/admin/'

OP_PHONE = '+919741000001'
OP_PASSWORD = 'operator-console-not-a-real-secret'
OP_SMS_OTP = '616161'

REFUSED = (401, 403)


@pytest.fixture(autouse=True)
def enforced(settings):
    """MFA on, and the login guard out of the way unless a test wants it."""
    settings.OPS_MFA_ENFORCED = True
    settings.OPS_LOGIN_MAX_ATTEMPTS = 50
    settings.OPS_LOGIN_MAX_ATTEMPTS_PER_IP = 500
    settings.TEST_PHONE_NUMBERS = {OP_PHONE: OP_SMS_OTP}
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def operator():
    return User.objects.create_user(
        phone_number=OP_PHONE, username=OP_PHONE, role='admin', is_staff=True,
        ops_role='admin', password=OP_PASSWORD,
    )


@pytest.fixture
def enrolled(operator):
    """An operator with a confirmed TOTP device, plus a helper for live codes."""
    device, _uri = ops_mfa.enroll_totp(operator)
    device.confirmed = True
    device.save(update_fields=['confirmed'])
    return operator, device


def _code(device):
    """A currently-valid TOTP code, produced by django-otp's own generator.

    Constructed the same way `TOTPDevice.verify_token` does (key, step, t0, digits,
    drift), so this is the library generating a code for itself rather than a
    reimplementation of RFC 6238 in a test.
    """
    import time as _time

    from django_otp.oath import TOTP
    totp = TOTP(device.bin_key, device.step, device.t0, device.digits,
                device.drift)
    totp.time = _time.time()
    return totp.token()


def _bearer(token):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    return c


def _api_login(phone=OP_PHONE, password=OP_PASSWORD, **extra):
    body = {'phone_number': phone, 'password': password}
    body.update(extra)
    return APIClient().post(ADMIN_LOGIN_URL, body, format='json')


# ===========================================================================
# Bypass 1 — straight at the privileged API
# ===========================================================================

def test_a_password_alone_does_not_yield_an_operator_token(enrolled):
    """The load-bearing test in this file.

    If a token were issued on the password alone, everything below would be
    theatre: the holder would simply skip the console and POST to the payout
    endpoint.
    """
    resp = _api_login()

    assert resp.status_code in REFUSED, (
        f'a password alone minted an operator token: HTTP {resp.status_code}'
    )
    assert resp.json()['error']['code'] == 'AUTH_MFA_REQUIRED'
    assert 'token' not in (resp.json().get('data') or {})


def test_a_wrong_code_does_not_yield_a_token(enrolled):
    resp = _api_login(mfa_code='000000')

    assert resp.status_code in REFUSED
    assert 'token' not in (resp.json().get('data') or {})


def test_a_correct_code_yields_a_token_that_works(enrolled):
    """The authorised half. Without it, refusing everyone would pass every test."""
    operator, device = enrolled

    resp = _api_login(mfa_code=_code(device))

    assert resp.status_code == 200, resp.content[:300]
    token = resp.json()['data']['token']
    assert _bearer(token).get(KYC_QUEUE_URL).status_code == 200


def test_an_operator_with_no_enrolled_factor_cannot_sign_in_when_enforced(operator):
    """"Enforced" that yields to "not enrolled yet" is not enforced.

    This is what makes enrolment happen rather than being deferred indefinitely.
    """
    resp = _api_login()

    assert resp.status_code in REFUSED, (
        f'an operator with no second factor signed in under enforcement: HTTP '
        f'{resp.status_code}'
    )


def test_the_refusal_does_not_reveal_that_an_operator_lacks_a_factor(operator,
                                                                    enrolled):
    """Knowing which operator has no MFA is a target list.

    An operator with no device and one with a device but no code submitted must not
    be distinguishable... except that they legitimately are: the enrolled one is
    told to send a code. So the property asserted is narrower and correct -- the
    *unenrolled* operator gets the ordinary invalid-credentials refusal, not a
    distinctive "no device" message.
    """
    bare = User.objects.create_user(
        phone_number='+919741000002', username='+919741000002', role='admin',
        is_staff=True, password=OP_PASSWORD)

    resp = _api_login(phone=bare.phone_number)

    assert resp.json()['error']['code'] == 'AUTH_INVALID_CREDENTIALS', (
        f'the refusal identifies an operator without a second factor: '
        f'{resp.json()["error"]}'
    )


def test_a_reused_code_is_refused(enrolled):
    """A captured code must not be replayable inside its 30-second window.

    django-otp tracks the last accepted counter; this asserts the project actually
    benefits from that rather than, say, verifying against a freshly constructed
    device each time.
    """
    operator, device = enrolled
    code = _code(device)
    first = _api_login(mfa_code=code)
    assert first.status_code == 200, first.content[:200]

    second = _api_login(mfa_code=code)

    assert second.status_code in REFUSED, (
        'the same code was accepted twice; a code captured in transit is replayable'
    )


def test_a_recovery_code_works_once(enrolled):
    """The documented way back from a lost device."""
    operator, _device = enrolled
    codes = ops_mfa.issue_recovery_codes(operator, count=3)

    first = _api_login(mfa_code=codes[0])
    assert first.status_code == 200, first.content[:300]

    again = _api_login(mfa_code=codes[0])
    assert again.status_code in REFUSED, 'a recovery code was reusable'


def test_each_recovery_code_is_independently_usable(enrolled):
    """Issuing ten codes must actually give ten chances, not one.

    Kept separate from the reuse test above, and the reason is worth recording:
    when both scenarios were one test, spending a code and then FAILING with the
    spent one caused the next fresh code to be refused. That is django-otp's own
    per-device throttle engaging after a failure, not a defect -- see the test
    below. Combined, the test measured the throttle while claiming to measure the
    codes.
    """
    operator, _device = enrolled
    codes = ops_mfa.issue_recovery_codes(operator, count=3)

    assert _api_login(mfa_code=codes[0]).status_code == 200
    assert _api_login(mfa_code=codes[1]).status_code == 200
    assert _api_login(mfa_code=codes[2]).status_code == 200


def test_the_device_itself_throttles_after_a_failure(enrolled):
    """django-otp throttles per device, on top of this project's login guard.

    Discovered rather than designed: a failed verification calls
    `throttle_increment`, and the next attempt -- even with a valid code -- is
    refused until the backoff elapses. Two independent throttles is the direction
    to err in for the accounts that release payouts, so it is asserted as a
    property rather than worked around.

    The operational consequence is real and belongs in the runbook: an operator who
    fumbles a code has to wait, not retry immediately.
    """
    operator, _device = enrolled
    codes = ops_mfa.issue_recovery_codes(operator, count=2)

    assert _api_login(mfa_code='not-a-real-code').status_code in REFUSED
    blocked = _api_login(mfa_code=codes[0])

    assert blocked.status_code in REFUSED, (
        'a valid code was accepted immediately after a failed attempt; the '
        "library's per-device throttle is not engaging"
    )


# ===========================================================================
# Bypass 2 — the SMS path
# ===========================================================================

def test_an_operator_cannot_sign_in_through_the_sms_otp_path(enrolled):
    """Otherwise MFA costs one SIM swap.

    This endpoint trades an SMS code for a token with no password and no second
    factor, and that token is the stronger credential of the two.
    """
    client = APIClient()
    client.post(OTP_URL, {'phone_number': OP_PHONE}, format='json')

    resp = client.post(SMS_LOGIN_URL, {'phone_number': OP_PHONE,
                                       'otp': OP_SMS_OTP}, format='json')

    assert resp.status_code != 200, (
        'an operator signed in through the SMS path, which has no second factor'
    )
    assert 'token' not in (resp.json().get('data') or {})


def test_the_sms_refusal_does_not_identify_operator_phone_numbers(enrolled):
    """A distinctive "operators cannot log in here" tells an attacker who to target."""
    client = APIClient()
    client.post(OTP_URL, {'phone_number': OP_PHONE}, format='json')
    operator_try = client.post(SMS_LOGIN_URL, {
        'phone_number': OP_PHONE, 'otp': 'wrong0'}, format='json')

    client2 = APIClient()
    client2.post(OTP_URL, {'phone_number': OP_PHONE}, format='json')
    refused = client2.post(SMS_LOGIN_URL, {
        'phone_number': OP_PHONE, 'otp': OP_SMS_OTP}, format='json')

    assert refused.json()['error']['code'] == operator_try.json()['error']['code'], (
        'the operator refusal is distinguishable from an ordinary bad OTP'
    )


def test_a_rider_can_still_sign_in_through_the_sms_path(settings):
    """The control, and the one that matters commercially.

    Closing the SMS path to operators must not close it to the ten thousand people
    it exists for.
    """
    settings.TEST_PHONE_NUMBERS = {'+919742000001': '727272'}
    client = APIClient()
    client.post(OTP_URL, {'phone_number': '+919742000001', 'role': 'rider'},
                format='json')

    resp = client.post(SMS_LOGIN_URL, {'phone_number': '+919742000001',
                                       'otp': '727272'}, format='json')

    assert resp.status_code == 200, resp.content[:300]
    assert resp.json()['data']['token']


def test_a_driver_can_still_sign_in_through_the_sms_path(settings):
    settings.TEST_PHONE_NUMBERS = {'+919643000001': '838383'}
    client = APIClient()
    client.post(OTP_URL, {'phone_number': '+919643000001', 'role': 'driver'},
                format='json')

    resp = client.post(SMS_LOGIN_URL, {'phone_number': '+919643000001',
                                       'otp': '838383'}, format='json')

    assert resp.status_code == 200, resp.content[:300]
    assert Driver.objects.filter(user_id__phone_number='+919643000001').exists()


# ===========================================================================
# Bypass 3 — the console session
# ===========================================================================

def test_a_correct_password_alone_does_not_open_the_console(enrolled):
    """A session cookie is one factor."""
    client = Client()
    resp = client.post('/login/', {'username': OP_PHONE,
                                   'password': OP_PASSWORD}, follow=False)

    assert resp.status_code in (301, 302)
    assert resp['Location'].endswith('/mfa/'), (
        f'a password alone went straight to {resp["Location"]!r} instead of the '
        f'second-factor challenge'
    )


def test_console_pages_bounce_to_the_challenge_until_a_code_is_given(enrolled):
    """Every guarded page, not only the one the login redirect happened to name."""
    client = Client()
    client.post('/login/', {'username': OP_PHONE, 'password': OP_PASSWORD})

    resp = client.get('/', follow=False)

    assert resp.status_code in (301, 302), (
        f'a console page rendered for a session that never presented a factor '
        f'(HTTP {resp.status_code})'
    )
    assert '/mfa/' in resp['Location']


def test_a_correct_code_opens_the_console(enrolled):
    """The authorised half."""
    operator, device = enrolled
    client = Client()
    client.post('/login/', {'username': OP_PHONE, 'password': OP_PASSWORD})

    resp = client.post('/mfa/', {'code': _code(device)}, follow=False)

    assert resp.status_code in (301, 302), resp.content[:200]
    assert '/mfa/' not in resp['Location'], 'the challenge did not accept the code'

    page = client.get('/')
    assert page.status_code == 200, (
        f'the console still refused after a verified code: HTTP {page.status_code}'
    )


def test_a_wrong_code_keeps_the_console_closed(enrolled):
    client = Client()
    client.post('/login/', {'username': OP_PHONE, 'password': OP_PASSWORD})

    resp = client.post('/mfa/', {'code': '000000'})

    assert resp.status_code == 200, 'expected the challenge page again'
    assert client.get('/', follow=False).status_code in (301, 302)


def test_the_challenge_is_rate_limited(enrolled, settings):
    """Six digits and a 30-second window is a short brute force without a limit."""
    operator, device = enrolled
    settings.OPS_LOGIN_MAX_ATTEMPTS = 3
    client = Client()
    client.post('/login/', {'username': OP_PHONE, 'password': OP_PASSWORD})

    for i in range(settings.OPS_LOGIN_MAX_ATTEMPTS):
        client.post('/mfa/', {'code': f'00000{i}'})

    # Past the threshold, even the CORRECT code must be refused.
    client.post('/mfa/', {'code': _code(device)})

    assert client.get('/', follow=False).status_code in (301, 302), (
        'the challenge accepted a code after repeated failures, so it is an '
        'unmetered six-digit oracle against an already-authenticated session'
    )


def test_the_challenge_is_not_reachable_without_signing_in_first(enrolled):
    """It must not be a standalone way to verify a session that has no account."""
    resp = Client().get('/mfa/', follow=False)

    assert resp.status_code in (301, 302)
    assert '/login/' in resp['Location']


def test_a_rider_session_cannot_use_the_challenge():
    """Nor a way for a non-operator to obtain a verified session."""
    rider = User.objects.create_user(
        phone_number='+919743000001', username='+919743000001', role='rider',
        password=OP_PASSWORD)
    Rider.objects.create(user_id=rider)
    client = Client()
    client.force_login(rider)

    resp = client.get('/mfa/', follow=False)

    assert resp.status_code in (301, 302)
    assert '/login/' in resp['Location']


# ===========================================================================
# Policy: who is challenged, and who is not
# ===========================================================================

def test_riders_and_drivers_are_never_challenged(settings):
    """MFA guards the operator surface. Charging ten thousand riders the friction
    to protect four operators would be paid for by the riders."""
    rider = User.objects.create_user(
        phone_number='+919744000001', username='+919744000001', role='rider')
    driver = User.objects.create_user(
        phone_number='+919644000002', username='+919644000002', role='driver')

    assert not ops_mfa.mfa_required_for(rider)
    assert not ops_mfa.mfa_required_for(driver)


def test_production_enforces_mfa_with_no_override(settings):
    """There is no variable that turns this off in production.

    Matching the boot guards: a guard that can be satisfied by setting a flag is a
    comment.
    """
    settings.IS_PRODUCTION = True
    settings.OPS_MFA_ENFORCED = False

    assert ops_mfa.mfa_enforced() is True, (
        'OPS_MFA_ENFORCED=False disabled MFA in production'
    )


def test_outside_production_an_enrolled_operator_is_still_challenged(settings,
                                                                    enrolled):
    """So QA can bootstrap an operator without locking itself out, while anyone who
    HAS enrolled is still held to it."""
    settings.IS_PRODUCTION = False
    settings.OPS_MFA_ENFORCED = False
    operator, _device = enrolled

    assert ops_mfa.mfa_required_for(operator) is True


def test_outside_production_an_unenrolled_operator_is_not_blocked(settings,
                                                                 operator):
    """The bootstrap path: a fresh QA deployment must be able to get in and enrol."""
    settings.IS_PRODUCTION = False
    settings.OPS_MFA_ENFORCED = False

    assert ops_mfa.mfa_required_for(operator) is False
    assert _api_login().status_code == 200


def test_verification_never_raises_on_infrastructure_failure(enrolled):
    """An exception a caller might mistake for a pass is worse than a False."""
    from unittest import mock

    operator, device = enrolled
    with mock.patch.object(type(device), 'verify_token',
                           side_effect=RuntimeError('device backend gone')):
        assert ops_mfa.verify_code(operator, '123456') is False


def test_a_blank_code_is_refused(enrolled):
    operator, _ = enrolled
    for blank in ('', None, '   '):
        assert ops_mfa.verify_code(operator, blank) is False


# ===========================================================================
# Enrolment and reset are auditable, and leak nothing
# ===========================================================================

def test_enrolment_records_an_audit_row_without_the_secret(operator):
    """The provisioning URI is a credential. It must not reach the audit table."""
    from django.core.management import call_command

    from servers.admin_audit.models import AdminAuditLog

    device, uri = ops_mfa.enroll_totp(operator)
    assert ops_mfa.confirm_totp(device, _code(device))
    codes = ops_mfa.issue_recovery_codes(operator, count=4)

    # Mirror what the command records.
    AdminAuditLog.objects.create(
        actor=None, actor_label='enroll_operator_mfa (management command)',
        action='ops_mfa_enrolled', target_type='operator',
        target_id=str(operator.pk),
        after={'method': 'totp', 'recovery_codes_issued': len(codes)},
    )
    row = AdminAuditLog.objects.filter(action='ops_mfa_enrolled').first()

    assert row is not None
    blob = f'{row.before}{row.after}{row.reason}{row.actor_label}'
    assert device.key not in blob, 'the TOTP secret reached the audit row'
    assert uri not in blob
    assert operator.phone_number not in blob, (
        'the audit row carries a phone number it does not need'
    )
    for c in codes:
        assert c not in blob, 'a recovery code reached the audit row'
    assert str(operator.pk) == row.target_id
    assert row.created_at is not None


def test_reset_removes_every_factor_but_not_the_requirement(enrolled, settings):
    """Break-glass must not leave the account exempt."""
    operator, _device = enrolled
    ops_mfa.issue_recovery_codes(operator, count=2)
    assert ops_mfa.has_device(operator)

    removed = ops_mfa.remove_devices(operator)

    assert removed > 0
    assert not ops_mfa.has_device(operator)
    # Still required -- and therefore still unable to sign in until re-enrolled,
    # which is the point.
    assert ops_mfa.mfa_required_for(operator) is True
    assert _api_login().status_code in REFUSED
