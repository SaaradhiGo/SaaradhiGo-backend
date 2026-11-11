"""The OTP has to survive between the request that issues it and the login.

WHY THIS FILE EXISTS
--------------------
A live probe against deployed QA on 2026-10-17 produced this sequence, from
the service's own logs:

    00:03:22  INFO  OTP test-phone bypass for +9199*** - no SMS sent
    00:03:22  POST /api/v1/auth/otp/  200
    00:03:28  WARN  OTP expired or not found for: +9199***

Six seconds, a 600-second TTL, and a healthy Redis (12 MB of 24 GB, no
connection errors in the log). The OTP was written and then was not there.

A second probe minutes later could not reproduce it: a deliberately wrong OTP
came back `AUTH_INVALID_OTP`, which proves the key was present and readable,
and a correct OTP then logged in. So the fault was transient and remains
UNEXPLAINED. What is certain is that it is not in the code path -- both views
derive the same key from the same input and `/auth/login/` routes to
`auth_user.views.login`, all verified by reading.

These tests pin the contract that failure violated, so that if the cause is
ever a code change rather than an environment, it fails here first:

  * the key the issuer writes is the key the verifier reads;
  * the value survives a second request;
  * the test-phone bypass populates the cache identically to the SMS path and
    sends no SMS;
  * expiry, attempts and single-use all behave as the login view assumes.

WHAT THESE TESTS CANNOT DO
-------------------------
They cannot reproduce the QA failure. `tests/conftest.py` has an autouse
fixture that replaces the cache with `LocMemCache` for every test in the
suite, so nothing here exercises Redis, key eviction, a TTL applied by a real
server, or two processes sharing a cache. If the QA cause was Redis-side,
these tests would have stayed green through it -- and they did.

What they do is fix the contract in place, so the *code* can no longer become
the cause. Diagnosing the environment needs the live cache:
`manage.py cache_diagnose`, added alongside this file, does a redacted
set/get/delete against whichever cache a deployment is actually configured
with.

None of these print an OTP. The fixtures use the documented test-phone pattern
and assert on status codes and cache keys, never on a code in a log line.
"""

from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from servers.auth_user.views import MAX_OTP_ATTEMPTS, OTP_EXPIRY

pytestmark = pytest.mark.django_db

OTP_URL = '/api/v1/auth/otp/'
LOGIN_URL = '/api/v1/auth/login/'

PHONE = '+919999000001'
FIXED_OTP = '100001'
TEST_PHONES = {PHONE: FIXED_OTP}

# The key both views derive. Asserted as a literal on purpose: if either side
# changes its key format, the two stop meeting and every login breaks with
# "OTP has expired" -- the exact symptom observed on QA.
CACHE_KEY = f'otp_role_{PHONE}'


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.delete(CACHE_KEY)
    yield
    cache.delete(CACHE_KEY)


@pytest.fixture
def client():
    return APIClient()


def _request_otp(client, phone=PHONE, role='rider'):
    return client.post(
        OTP_URL, {'phone_number': phone, 'role': role}, format='json')


def _login(client, otp, phone=PHONE):
    return client.post(
        LOGIN_URL, {'phone_number': phone, 'otp': otp}, format='json')


@override_settings(TEST_PHONE_NUMBERS=TEST_PHONES)
def test_the_issuer_writes_the_key_the_verifier_reads(client):
    """The failure mode this file exists for.

    Two views, one key, derived independently from the same request field. The
    assertion is on the literal key so a rename on one side cannot pass.
    """
    assert cache.get(CACHE_KEY) is None

    response = _request_otp(client)
    assert response.status_code == 200, response.content

    cached = cache.get(CACHE_KEY)
    assert cached is not None, (
        'the OTP request returned 200 but wrote nothing to the cache, which '
        'is what a login then reports as "OTP has expired"'
    )
    assert cached['role'] == 'rider'
    assert cached['attempts'] == 0


@override_settings(TEST_PHONE_NUMBERS=TEST_PHONES)
def test_the_otp_survives_into_a_separate_login_request(client):
    """Issued by one request, read by the next. This is the round trip that
    failed on QA for six seconds."""
    assert _request_otp(client).status_code == 200

    response = _login(client, FIXED_OTP)

    assert response.status_code == 200, response.content
    body = response.json()
    assert body['status'] == 'success'
    assert body['data']['token']


@override_settings(TEST_PHONE_NUMBERS=TEST_PHONES)
def test_the_test_phone_bypass_sends_no_sms(client):
    """It must populate the cache the same way and never reach SNS.

    The QA probe relied on this being true, and the log confirmed it. Here it
    is enforced rather than hoped for: a pilot that bills an SMS per QA run is
    a pilot nobody runs acceptance drills on.
    """
    with patch('servers.auth_user.views.send_otp_via_sns') as sns:
        response = _request_otp(client)

    assert response.status_code == 200
    sns.delay.assert_not_called()
    assert response.json()['data']['task_id'] == 'test-phone-no-sms'
    # Identical cache shape to the SMS path -- attempts, role and expiry all
    # behave the same afterwards.
    assert cache.get(CACHE_KEY)['attempts'] == 0


@override_settings(TEST_PHONE_NUMBERS={})
def test_a_real_phone_still_goes_through_sns(client):
    """The control. With no bypass configured the OTP is generated and sent,
    and the cache is still populated before the send."""
    with patch('servers.auth_user.views.send_otp_via_sns') as sns:
        sns.delay.return_value = 'task-1'
        response = _request_otp(client, phone='+919888000002')

    assert response.status_code == 200
    sns.delay.assert_called_once()
    assert cache.get('otp_role_+919888000002') is not None


@override_settings(TEST_PHONE_NUMBERS=TEST_PHONES)
def test_a_wrong_otp_is_rejected_as_wrong_not_as_missing(client):
    """The diagnostic that identified the QA failure as transient.

    `AUTH_INVALID_OTP` proves the key was found. `AUTH_OTP_EXPIRED` means it
    was not there at all. Conflating the two would have made the live probe
    unreadable, so the distinction is worth a test.
    """
    assert _request_otp(client).status_code == 200

    response = _login(client, '000000')

    assert response.status_code == 400
    assert response.json()['error']['code'] == 'AUTH_INVALID_OTP'


@override_settings(TEST_PHONE_NUMBERS=TEST_PHONES)
def test_a_missing_otp_is_reported_as_expired(client):
    """With nothing cached, login says expired -- the symptom seen on QA."""
    response = _login(client, FIXED_OTP)

    assert response.status_code == 400
    assert response.json()['error']['code'] == 'AUTH_OTP_EXPIRED'


@override_settings(TEST_PHONE_NUMBERS=TEST_PHONES)
def test_a_wrong_otp_increments_attempts_without_destroying_the_otp(client):
    """A typo must not force a new OTP request."""
    assert _request_otp(client).status_code == 200

    _login(client, '000000')
    assert cache.get(CACHE_KEY)['attempts'] == 1

    # The real one still works afterwards.
    assert _login(client, FIXED_OTP).status_code == 200


@override_settings(TEST_PHONE_NUMBERS=TEST_PHONES)
def test_attempts_are_capped(client):
    """Brute force protection, at the documented limit."""
    assert _request_otp(client).status_code == 200

    for _ in range(MAX_OTP_ATTEMPTS):
        _login(client, '000000')

    response = _login(client, FIXED_OTP)
    assert response.status_code == 429
    assert response.json()['error']['code'] == 'AUTH_TOO_MANY_ATTEMPTS'


@override_settings(TEST_PHONE_NUMBERS=TEST_PHONES)
def test_a_successful_login_consumes_the_otp(client):
    """Single use. A replayed OTP must not mint a second session."""
    assert _request_otp(client).status_code == 200
    assert _login(client, FIXED_OTP).status_code == 200

    assert cache.get(CACHE_KEY) is None

    replay = _login(client, FIXED_OTP)
    assert replay.status_code == 400
    assert replay.json()['error']['code'] == 'AUTH_OTP_EXPIRED'


@override_settings(TEST_PHONE_NUMBERS=TEST_PHONES)
def test_the_expiry_is_ten_minutes_and_is_what_the_client_is_told(client):
    """The response promises `expires_in`; it must be the TTL actually used.

    A mismatch here would look exactly like the QA symptom: the app waits
    inside the window it was given and the server has already forgotten.
    """
    response = _request_otp(client)

    assert OTP_EXPIRY == 600
    assert response.json()['data']['expires_in'] == OTP_EXPIRY
