"""Security review, 16 Nov 2026: sign out everywhere, and the email-login throttle is attached."""

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from base.throttles import EmailLoginThrottle
from servers.auth_user.email_auth_views import email_login

pytestmark = pytest.mark.django_db
User = get_user_model()


def _user():
    return User.objects.create_user(phone_number='+919000000333', username='+919000000333',
                                    role='rider')


def test_sign_out_everywhere_ends_every_refresh_token():
    user = _user()
    phone, laptop = RefreshToken.for_user(user), RefreshToken.for_user(user)
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f'Bearer {phone.access_token}')
    r = c.post('/api/v1/auth/logout/all/')
    assert r.status_code == 200 and r.json()['data']['signed_out_sessions'] == 2
    anon = APIClient()
    for token in (phone, laptop):
        assert anon.post('/api/v1/auth/refresh/', {'refresh_token': str(token)},
                         format='json').status_code in (400, 401)


def test_sign_out_everywhere_needs_a_session():
    assert APIClient().post('/api/v1/auth/logout/all/').status_code == 401


def test_email_login_carries_its_throttle():
    assert EmailLoginThrottle in email_login.cls.throttle_classes
