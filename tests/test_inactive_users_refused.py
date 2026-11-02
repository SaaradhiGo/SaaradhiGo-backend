"""A deactivated account keeps no session and gets no new one (AUDIT 27.78, Low 3).

Before: `/auth/refresh/` minted tokens for any user id in a valid refresh
token, the WebSocket middleware accepted an inactive user's access token,
deactivation left every refresh token alive for 14 days, and the operator API
login issued a token to a deactivated operator.
"""

from types import SimpleNamespace

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework.test import APIClient
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from servers.driver.models import Driver

pytestmark = pytest.mark.django_db

User = get_user_model()

REFRESH = '/api/v1/auth/refresh/'
ADMIN_LOGIN = '/api/v1/auth/admin/login/'
PASSWORD = 'Granite-Orchard-5520-chosen'


def _driver(phone='+919741000501'):
    user = User.objects.create_user(phone_number=phone, role='driver')
    return user, Driver.objects.create(user_id=user)


def _deactivate(user):
    user.is_active = False
    user.save(update_fields=['is_active'])


class TestRefresh:
    def test_an_inactive_user_cannot_refresh(self):
        user, _ = _driver()
        token = str(RefreshToken.for_user(user))
        User.objects.filter(pk=user.pk).update(is_active=False)  # no signal: the door itself must hold

        r = APIClient().post(REFRESH, {'refresh_token': token}, format='json')

        assert r.status_code == 401
        assert r.json()['error']['code'] == 'AUTH_ACCOUNT_INACTIVE'
        assert 'token' not in (r.json().get('data') or {})

    def test_an_active_user_still_refreshes(self):
        user, _ = _driver()
        r = APIClient().post(REFRESH, {'refresh_token': str(RefreshToken.for_user(user))},
                             format='json')
        assert r.status_code == 200
        assert r.json()['data']['token']


class TestWebSocket:
    def test_an_inactive_users_access_token_is_anonymous_on_the_socket(self):
        from servers.ws_middleware import get_user_from_token

        user, _ = _driver()
        access = str(AccessToken.for_user(user))
        assert async_to_sync(get_user_from_token)(access).is_authenticated
        _deactivate(user)
        assert not async_to_sync(get_user_from_token)(access).is_authenticated


class TestDeactivationRevokesRefreshTokens:
    def test_saving_a_user_inactive_blacklists_every_outstanding_token(self):
        user, _ = _driver()
        RefreshToken.for_user(user)
        RefreshToken.for_user(user)
        assert OutstandingToken.objects.filter(user=user).count() == 2
        _deactivate(user)
        assert BlacklistedToken.objects.filter(token__user=user).count() == 2

    def test_an_unrelated_save_revokes_nothing(self):
        user, _ = _driver()
        RefreshToken.for_user(user)
        user.full_name = 'Ravi Kumar'
        user.save()
        assert not BlacklistedToken.objects.filter(token__user=user).exists()

    def test_the_driver_deactivation_workflow_revokes_them(self):
        from servers.driver.admin_drivers import execute_deactivate

        user, driver = _driver()
        token = str(RefreshToken.for_user(user))
        execute_deactivate(SimpleNamespace(payload={'driver_id': driver.id, 'note': 'test'},
                                           maker_id=None, id=1))
        assert BlacklistedToken.objects.filter(token__user=user).count() == 1
        r = APIClient().post(REFRESH, {'refresh_token': token}, format='json')
        assert r.status_code in (400, 401)


class TestOperatorApiLogin:
    @pytest.fixture(autouse=True)
    def _quiet(self, settings):
        settings.ADMIN_MFA_REQUIRED = False
        settings.OPS_LOGIN_MAX_ATTEMPTS = 50
        settings.OPS_LOGIN_MAX_ATTEMPTS_PER_IP = 500
        cache.clear()

    def _operator(self):
        return User.objects.create_user(
            phone_number='+919741000502', username='+919741000502',
            email='inactive-op@saaradhigo.test', role='admin', is_staff=True,
            password=PASSWORD,
        )

    def _login(self):
        return APIClient().post(ADMIN_LOGIN, {'email': 'inactive-op@saaradhigo.test',
                                              'password': PASSWORD}, format='json')

    def test_an_active_operator_signs_in(self):
        self._operator()
        assert self._login().status_code == 200

    def test_an_inactive_operator_is_refused_like_a_wrong_password(self):
        _deactivate(self._operator())
        r = self._login()
        assert r.status_code == 401
        assert r.json()['error']['code'] == 'AUTH_INVALID_CREDENTIALS'


class TestSmsLogin:
    def test_an_inactive_account_cannot_sign_back_in_by_sms(self):
        phone, code = '+919999000002', '100002'  # a QA test phone from settings_ci
        user = User.objects.create_user(phone_number=phone, role='rider')
        _deactivate(user)
        client = APIClient()
        client.post('/api/v1/auth/otp/', {'phone_number': phone, 'role': 'rider'}, format='json')
        r = client.post('/api/v1/auth/login/', {'phone_number': phone, 'otp': code}, format='json')
        assert r.status_code == 403
        assert r.json()['error']['code'] == 'AUTH_ACCOUNT_INACTIVE'
