"""The Django console is OFF (owner ruling, AUDIT 27.62).

"Switch the Django console off entirely: remove the Django sign-in, MFA, sign-out and admin
routes from public routing. Emergency access is via the console's break-glass account and
Railway / manage.py only."

Every route the old frozen-console inventory listed is enumerated below, and every one answers
404 for an anonymous caller AND for a signed-in superuser, on GET and on POST. After this change
Django serves nothing public except the API (`/api/v1/`), the health probes and the websockets.
"""

import importlib

import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client
from django.urls import NoReverseMatch, Resolver404, resolve, reverse

User = get_user_model()

pytestmark = pytest.mark.django_db

#: The session console that was mounted at the site root (old servers/admin_dashboard/urls.py).
CONSOLE_PATHS = (
    '/login/', '/mfa/', '/', '/driver_onboarding/', '/dispute_support/', '/payment_dashboard/',
    '/executive_revenue/', '/driver_loyalty/', '/ride/', '/ride/1/', '/riders/', '/promo-codes/',
    '/emergency/', '/stale-rides/', '/transactions/', '/fare_surge/', '/predictive_heatmaps/',
    '/logout/', '/search/', '/api/search/', '/driver/1/', '/notifications/',
    '/api/notifications/', '/api/notifications/1/read/',
)

#: The Django admin site (old inventory: ADMIN_SITE_ROUTES, ADMIN_SITE_MODELS, the TOTP extras).
ADMIN_PATHS = (
    '/admin/', '/admin/login/', '/admin/logout/', '/admin/password_change/',
    '/admin/password_change/done/', '/admin/autocomplete/', '/admin/jsi18n/', '/admin/r/1/1/',
    '/admin/auth/', '/admin/auth/group/', '/admin/auth/group/add/', '/admin/auth/group/1/change/',
    '/admin/token_blacklist/outstandingtoken/', '/admin/token_blacklist/blacklistedtoken/',
    '/admin/auth_user/customuser/', '/admin/auth_user/customuser/1/change/',
    '/admin/rider/rider/', '/admin/rider/wallet/', '/admin/rider/wallettransaction/',
    '/admin/driver/driver/', '/admin/driver/driver/1/change/', '/admin/driver/vehicle/',
    '/admin/driver/withdrawalrequest/', '/admin/driver/driverbankaccount/',
    '/admin/ride/trip/', '/admin/ride/trip/add/', '/admin/ride/promocode/',
    '/admin/payments/payment/', '/admin/payments/transactionhistory/',
    '/admin/pricing/servicezone/', '/admin/pricing/ratecard/', '/admin/support/supportticket/',
    '/admin/otp_totp/totpdevice/', '/admin/otp_totp/totpdevice/1/config/',
    '/admin/otp_totp/totpdevice/1/qrcode/', '/admin/otp_static/staticdevice/',
    '/admin/anything/at/all/',
)

#: The old route names. None may reverse any more.
REMOVED_NAMES = (
    'login', 'ops_mfa_challenge', 'logout', 'fleet_monitor', 'driver_onboarding',
    'dispute_support', 'payment_dashboard', 'executive_revenue', 'driver_loyalty', 'ride',
    'ride_detail', 'riders', 'promo_codes', 'emergency_dashboard', 'stale_rides',
    'transaction_dashboard', 'fare_surge', 'predictive_heatmaps', 'global_search',
    'global_search_api', 'driver_profile', 'notifications', 'admin_notifications_feed',
    'admin_notification_mark_read',
    'admin:index', 'admin:login', 'admin:logout', 'admin:password_change',
    'admin:password_change_done', 'admin:jsi18n', 'admin:app_list',
)

ALL_PATHS = CONSOLE_PATHS + ADMIN_PATHS


@pytest.fixture
def superuser_client(db):
    user = User.objects.create_user(
        phone_number='+919000000099', role='admin', is_staff=True, is_superuser=True,
        password='console-off-test-password')
    client = Client()
    client.force_login(user)
    return client


class TestEveryRemovedRouteIs404:
    @pytest.mark.parametrize('path', ALL_PATHS)
    def test_the_resolver_has_no_such_route(self, path):
        with pytest.raises(Resolver404):
            resolve(path)

    @pytest.mark.parametrize('path', ALL_PATHS)
    def test_anonymous_gets_404_on_get_and_post(self, path):
        client = Client(enforce_csrf_checks=False)
        assert client.get(path).status_code == 404, path
        assert client.post(path, {}).status_code == 404, path

    @pytest.mark.parametrize('path', ALL_PATHS)
    def test_a_signed_in_superuser_gets_404_on_get_and_post(self, path, superuser_client):
        assert superuser_client.get(path).status_code == 404, path
        assert superuser_client.post(path, {}).status_code == 404, path

    def test_a_trailing_slash_is_not_redirected_into_existence(self, superuser_client):
        for path in ('/admin', '/login', '/mfa', '/logout', '/ride'):
            assert superuser_client.get(path).status_code == 404, path

    @pytest.mark.parametrize('name', REMOVED_NAMES)
    def test_no_removed_route_name_reverses(self, name):
        with pytest.raises(NoReverseMatch):
            reverse(name, args=[1]) if name in ('ride_detail', 'driver_profile',
                                                'admin_notification_mark_read') else reverse(name)

    def test_the_404_page_gives_nothing_away(self, superuser_client):
        body = superuser_client.get('/admin/').content.decode().lower()
        assert 'django' not in body and 'admin' not in body.replace('page not found', '')


class TestWhatIsLeftIsOnlyTheApi:
    def test_nothing_in_the_root_urlconf_is_a_django_page(self):
        from django.urls import get_resolver

        prefixes = set()
        for entry in get_resolver().url_patterns:
            pattern = str(entry.pattern)
            prefixes.add(pattern.split('/')[0] if '/' in pattern else pattern)
        assert prefixes <= {'healthz', 'version', 'api'}, prefixes

    def test_no_admin_namespace_exists(self):
        from django.urls import get_resolver

        assert 'admin' not in get_resolver().namespace_dict

    def test_the_api_the_next_console_uses_is_still_routed(self):
        for path in ('/api/v1/auth/admin/login/', '/api/v1/auth/admin/mfa/status/',
                     '/api/v1/auth/admin/mfa/recovery-codes/regenerate/',
                     '/api/v1/ride/admin/dashboard/', '/api/v1/driver/admin/',
                     '/healthz', '/version'):
            assert resolve(path) is not None, path

    def test_the_operator_api_sign_in_still_answers_json_not_404(self):
        client = Client()
        response = client.post('/api/v1/auth/admin/login/',
                               {'phone_number': '+910000000000', 'password': 'wrong'},
                               content_type='application/json')
        assert response.status_code in (400, 401, 403, 429), response.status_code
        assert response['Content-Type'].startswith('application/json')

    def test_the_health_probe_still_answers(self):
        assert Client().get('/healthz').status_code == 200


class TestTheMachineryIsGone:
    def test_the_django_admin_app_is_not_installed(self):
        assert 'django.contrib.admin' not in settings.INSTALLED_APPS
        assert not any('admin_dashboard.lockdown' in app for app in settings.INSTALLED_APPS)

    def test_the_lockdown_middleware_is_gone(self):
        assert not any('lockdown' in m or 'FrozenConsole' in m for m in settings.MIDDLEWARE)

    def test_the_allowlist_settings_are_gone(self):
        assert not hasattr(settings, 'FROZEN_CONSOLE_ALLOWED_ROUTES')
        assert not hasattr(settings, 'FROZEN_CONSOLE_WRITABLE_MODELS')

    @pytest.mark.parametrize('module', [
        'servers.admin_dashboard.lockdown', 'servers.admin_dashboard.inventory',
        'servers.admin_dashboard.urls', 'servers.admin_dashboard.views',
        'servers.admin_dashboard.notification_api',
    ])
    def test_the_console_modules_are_deleted(self, module):
        with pytest.raises(ImportError):
            importlib.import_module(module)

    def test_the_login_guard_the_api_shares_survives(self):
        from servers.admin_dashboard import login_guard

        assert callable(login_guard.is_locked) and callable(login_guard.record_failure)

    def test_manage_py_check_passes(self):
        call_command('check')

    def test_manage_py_still_runs_management_commands(self):
        from io import StringIO

        out = StringIO()
        call_command('showmigrations', 'auth_user', stdout=out)
        assert out.getvalue()
