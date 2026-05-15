"""Regression-locks for the TEST_PHONE_NUMBERS bypass.

The bypass exists so QA can sign in without configuring AWS SNS. It's
an env-driven feature; the production default (empty dict) must give
behaviour identical to a deployment that doesn't know the feature
exists. These tests pin both shapes.
"""
from unittest.mock import patch

import pytest


TEST_PHONE = '+919999000001'
TEST_OTP = '424242'


@pytest.fixture
def with_test_phone(settings):
    """Inject one test phone for the duration of a test."""
    settings.TEST_PHONE_NUMBERS = {TEST_PHONE: TEST_OTP}


@pytest.fixture
def without_test_phones(settings):
    """Production-equivalent: no bypass configured."""
    settings.TEST_PHONE_NUMBERS = {}


# -------------------------------------------------------------------------
# Bypass behaviour when enabled
# -------------------------------------------------------------------------

@patch('servers.auth_user.views.send_otp_via_sns.delay')
def test_request_otp_skips_sns_for_test_phone(mock_send, api_client, with_test_phone):
    """A configured test phone must NOT trigger an SNS send (saves AWS
    bill + lets QA work offline)."""
    resp = api_client.post(
        '/api/v1/auth/otp/',
        {'phone_number': TEST_PHONE},
        format='json',
    )
    assert resp.status_code == 200
    mock_send.assert_not_called()
    # Still returns the same shape — task_id present (sentinel value), no OTP
    data = resp.json()['data']
    assert 'otp' not in data
    assert data['task_id'] == 'test-phone-no-sms'


@pytest.mark.django_db
def test_login_with_test_phone_and_configured_otp_succeeds(api_client, with_test_phone):
    """Full round-trip: request OTP, then login with the configured OTP,
    receive a JWT pair."""
    api_client.post('/api/v1/auth/otp/', {'phone_number': TEST_PHONE}, format='json')
    resp = api_client.post(
        '/api/v1/auth/login/',
        {'phone_number': TEST_PHONE, 'otp': TEST_OTP},
        format='json',
    )
    assert resp.status_code == 200, resp.content
    data = resp.json()['data']
    assert data['token']
    assert data['refresh_token']


@pytest.mark.django_db
def test_login_with_test_phone_wrong_otp_still_fails(api_client, with_test_phone):
    """Bypass means 'use this fixed OTP', not 'accept any OTP'. A wrong
    OTP for a test phone still rejects."""
    api_client.post('/api/v1/auth/otp/', {'phone_number': TEST_PHONE}, format='json')
    resp = api_client.post(
        '/api/v1/auth/login/',
        {'phone_number': TEST_PHONE, 'otp': '000000'},
        format='json',
    )
    assert resp.status_code == 400


@patch('servers.auth_user.views.send_otp_via_sns.delay')
def test_test_phone_bypasses_otp_request_throttle(mock_send, api_client, with_test_phone):
    """QA needs to iterate fast — burst-throttle must NOT apply to test
    phones. Three OTP requests inside the 30-second burst window all
    succeed for a test phone (a real phone would 429 on the third)."""
    for _ in range(3):
        resp = api_client.post(
            '/api/v1/auth/otp/',
            {'phone_number': TEST_PHONE},
            format='json',
        )
        assert resp.status_code == 200, resp.content


# -------------------------------------------------------------------------
# Production-default behaviour (empty TEST_PHONE_NUMBERS)
# -------------------------------------------------------------------------

@patch('servers.auth_user.views.send_otp_via_sns.delay')
def test_real_phone_still_calls_sns(mock_send, api_client, without_test_phones):
    """Without the bypass enabled, a non-listed phone exercises the
    normal SNS path."""
    mock_send.return_value = 'task-id-real-otp'
    resp = api_client.post(
        '/api/v1/auth/otp/',
        {'phone_number': '+917999111222'},  # not in the test list
        format='json',
    )
    assert resp.status_code == 200
    mock_send.assert_called_once()
    # task_id is the real Celery task id, not the bypass sentinel
    assert resp.json()['data']['task_id'] != 'test-phone-no-sms'


@patch('servers.auth_user.views.send_otp_via_sns.delay')
def test_phone_not_in_list_does_not_bypass(mock_send, api_client, with_test_phone):
    """Bypass is exact-match only — a phone NOT in TEST_PHONE_NUMBERS
    must still go through SNS even when other phones are configured."""
    mock_send.return_value = 'task-id-not-listed'
    resp = api_client.post(
        '/api/v1/auth/otp/',
        {'phone_number': '+917888333444'},  # different from TEST_PHONE
        format='json',
    )
    assert resp.status_code == 200
    mock_send.assert_called_once()


def test_real_phone_subject_to_throttle(settings, api_client, without_test_phones):
    """Real phones (no bypass) hit the 2/min burst throttle by the 3rd
    attempt within 30 seconds — proves the bypass really is opt-in per
    phone, not a blanket throttle disabler."""
    # Force a small rate so we hit the limit deterministically.
    settings.REST_FRAMEWORK = {
        **settings.REST_FRAMEWORK,
        'DEFAULT_THROTTLE_RATES': {
            **settings.REST_FRAMEWORK.get('DEFAULT_THROTTLE_RATES', {}),
            'otp_request_burst': '2/min',
        },
    }
    with patch('servers.auth_user.views.send_otp_via_sns.delay') as mock_send:
        mock_send.return_value = 't'
        results = []
        for _ in range(5):
            r = api_client.post(
                '/api/v1/auth/otp/',
                {'phone_number': '+917123456789'},
                format='json',
            )
            results.append(r.status_code)
    # At least one must be 429 — we exceeded the 2/min burst.
    assert 429 in results, f'Expected a 429 in the burst, got {results}'
