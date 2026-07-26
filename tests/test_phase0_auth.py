"""Regression-locks for the Phase-0 auth hardening (PRs #3, #10, #21).

These exist so that if a future change reopens any of the doors we closed
(OTP echoed in response, OTP echoed in logs, no rotation, no blacklist, no
logout endpoint), CI catches it before the deploy.
"""
import pytest
from unittest.mock import patch

from django.core.cache import cache
from rest_framework_simplejwt.tokens import RefreshToken


# -------------------------------------------------------------------------
# OTP issue endpoint (PR #3)
# -------------------------------------------------------------------------

@patch('servers.auth_user.views.send_otp_via_sns.delay')
def test_otp_response_does_not_echo_otp(mock_send, api_client):
    """The OTP must reach the user only via SMS — never the response body."""
    mock_send.return_value = 'dummy-task-id'
    resp = api_client.post(
        '/api/v1/auth/otp/',
        {'phone_number': '+917396918001'},
        format='json',
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data['status'] == 'success'
    assert 'otp' not in data['data']  # CRITICAL regression guard
    assert 'task_id' in data['data']
    assert 'expires_in' in data['data']


# -------------------------------------------------------------------------
# JWT rotation + blacklist + logout (PR #21)
# -------------------------------------------------------------------------

@pytest.fixture
def fresh_user(db):
    from django.contrib.auth import get_user_model
    User = get_user_model()
    user = User.objects.create_user(phone_number='+917000000001', role='rider')
    from servers.rider.models import Rider
    Rider.objects.create(user_id=user)
    return user


def _login_and_get_tokens(api_client, user):
    """Helper: seed an OTP, hit /auth/login/, return (access, refresh)."""
    phone = user.phone_number
    cache.set(
        f'otp_role_{phone}',
        {'otp': '123456', 'role': 'rider', 'attempts': 0},
        600,
    )
    resp = api_client.post(
        '/api/v1/auth/login/',
        {'phone_number': phone, 'otp': '123456'},
        format='json',
    )
    assert resp.status_code == 200, resp.content
    data = resp.json()['data']
    return data['token'], data['refresh_token']


def test_refresh_rotates_and_blacklists_old(api_client, fresh_user):
    """A used refresh token cannot be redeemed twice."""
    _, refresh = _login_and_get_tokens(api_client, fresh_user)

    # First refresh: should succeed and return a new pair
    resp1 = api_client.post(
        '/api/v1/auth/refresh/',
        {'refresh_token': refresh},
        format='json',
    )
    assert resp1.status_code == 200, resp1.content
    new_access = resp1.json()['data']['token']
    new_refresh = resp1.json()['data']['refresh_token']
    assert new_access and new_refresh
    assert new_refresh != refresh

    # Second refresh with the SAME old refresh token: must be rejected
    # because rotation blacklisted it.
    resp2 = api_client.post(
        '/api/v1/auth/refresh/',
        {'refresh_token': refresh},
        format='json',
    )
    assert resp2.status_code == 400, resp2.content


def test_logout_blacklists_refresh(api_client, fresh_user):
    """POST /auth/logout/ blacklists the supplied refresh token."""
    access, refresh = _login_and_get_tokens(api_client, fresh_user)

    api_client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
    resp = api_client.post(
        '/api/v1/auth/logout/',
        {'refresh_token': refresh},
        format='json',
    )
    assert resp.status_code == 200, resp.content

    # Refresh with the now-blacklisted refresh must fail
    api_client.credentials()  # drop auth
    resp_refresh = api_client.post(
        '/api/v1/auth/refresh/',
        {'refresh_token': refresh},
        format='json',
    )
    assert resp_refresh.status_code == 400


def test_logout_rejects_other_users_refresh(api_client, fresh_user, db):
    """A user cannot log another user out by submitting that user's refresh."""
    from django.contrib.auth import get_user_model
    User = get_user_model()
    other = User.objects.create_user(phone_number='+917000000002', role='rider')
    from servers.rider.models import Rider
    Rider.objects.create(user_id=other)

    # fresh_user's tokens
    access, _ = _login_and_get_tokens(api_client, fresh_user)
    # other user's refresh
    other_refresh = str(RefreshToken.for_user(other))

    api_client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
    resp = api_client.post(
        '/api/v1/auth/logout/',
        {'refresh_token': other_refresh},
        format='json',
    )
    assert resp.status_code == 403, resp.content


def test_access_token_lifetime_is_15_min():
    """ACCESS_TOKEN_LIFETIME must be the Phase-0 default (15 min)."""
    from django.conf import settings
    from datetime import timedelta
    lifetime = settings.SIMPLE_JWT['ACCESS_TOKEN_LIFETIME']
    assert lifetime == timedelta(minutes=15)


def test_refresh_token_rotation_is_enabled():
    """ROTATE_REFRESH_TOKENS + BLACKLIST_AFTER_ROTATION must both be True."""
    from django.conf import settings
    sj = settings.SIMPLE_JWT
    assert sj.get('ROTATE_REFRESH_TOKENS') is True
    assert sj.get('BLACKLIST_AFTER_ROTATION') is True
