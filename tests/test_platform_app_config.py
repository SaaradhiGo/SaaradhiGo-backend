"""The public app-config endpoint (module 15) and the server-side app-version gate.

    GET /api/v1/app-config/?country=XX[&city=CODE][&app=rider|driver]

Public, read-only, cacheable, rate limited; no PII and no secret; an unknown country is a 404 and
NEVER India.
"""

import json
from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from servers.admin_access.models import GeoNode
from servers.platform_ops.models import AppVersionPolicy, FeatureFlag, FeatureFlagOverride
from test_sos_support_zones_matrix import _zone, geo  # noqa: F401 - fixtures
from platform_support import data, error_code

pytestmark = pytest.mark.django_db

URL = '/api/v1/app-config/'


def get(query='', **headers):
    return APIClient().get(URL + query, **headers)


@pytest.fixture
def markets(geo):
    """Hyderabad live with a zone; Austin live with a zone (the matrix polygon for both)."""
    _zone('IN-TG-HYD', geo['IN-TG-HYD'])
    _zone('US-TX-AUS', geo['US-TX-AUS'], country='US')
    GeoNode.objects.filter(code='US-TX-AUS').update(status='active')
    cache.clear()
    return geo


class TestConfigPerCountry:
    def test_india(self, markets):
        body = data(get('?country=IN'))
        assert body['country']['code'] == 'IN'
        assert body['country']['calling_code'] == '91'
        assert body['country']['phone']['national_number_lengths'] == [10]
        assert body['country']['currency'] == {
            'code': 'INR', 'symbol': '₹', 'symbol_position': 'prefix', 'minor_units': 2}
        assert body['country']['distance_unit'] == 'km'
        assert body['country']['emergency']['numbers'] == [{'kind': 'unified', 'number': '112'}]
        assert body['country']['emergency']['label'] == 'Emergency services'
        assert body['languages'] == ['en', 'hi', 'te']
        assert body['status'] == 'live'
        hyd = {c['code']: c for c in body['cities']}['IN-TG-HYD']
        assert set(hyd['service_area']) == {'center', 'bounds'}
        assert hyd['service_area']['bounds']['south'] < hyd['service_area']['bounds']['north']

    def test_united_states(self, markets):
        body = data(get('?country=US'))
        assert body['country']['calling_code'] == '1'
        assert body['country']['currency']['code'] == 'USD' and \
            body['country']['currency']['symbol'] == '$'
        assert body['country']['distance_unit'] == 'mi'
        assert body['country']['tax']['mode'] == 'additive'
        assert body['country']['emergency']['numbers'] == [{'kind': 'unified', 'number': '911'}]
        assert body['languages'] == ['en']
        assert [c['code'] for c in body['cities']] == ['US-TX-AUS']

    def test_a_draft_city_is_not_offered_and_a_live_one_without_a_zone_is_not_either(self, markets):
        GeoNode.objects.filter(code='US-TX-AUS').update(status='draft')
        assert data(get('?country=US'))['cities'] == []
        GeoNode.objects.filter(code='IN-TG-WGL').update(status='active')   # live, zone inactive
        from servers.pricing.models import ServiceZone

        ServiceZone.objects.filter(code='IN-TG-WGL').update(is_active=False)
        assert 'IN-TG-WGL' not in {c['code'] for c in data(get('?country=IN'))['cities']}

    def test_a_city_query_scopes_flags_and_derives_the_country(self, markets):
        flag = FeatureFlag.objects.create(key='cfg.test_flag', description='test only')
        FeatureFlagOverride.objects.create(flag=flag, node=markets['IN'], enabled=True)
        FeatureFlagOverride.objects.create(flag=flag, node=markets['IN-TG-HYD'], enabled=False)

        assert data(get('?country=IN'))['flags']['cfg.test_flag'] is True
        body = data(get('?city=IN-TG-HYD'))
        assert body['country']['code'] == 'IN' and body['flags']['cfg.test_flag'] is False
        assert data(get('?country=US'))['flags']['cfg.test_flag'] is False       # the default

    def test_with_no_market_named_it_lists_the_live_countries_only(self, markets):
        body = data(get())
        assert {c['code'] for c in body['countries']} == {'IN', 'US'}
        GeoNode.objects.filter(code='US-TX-AUS').update(status='draft')
        assert {c['code'] for c in data(get())['countries']} == {'IN'}


class TestNeverIndiaByDefault:
    @pytest.mark.parametrize('query', ['?country=ZZ', '?country=XX', '?country=india',
                                       '?country=IN%20', '?country=GB'])
    def test_an_unknown_country_is_a_404_not_india(self, markets, query):
        if query == '?country=IN%20':
            assert get(query).status_code == 200            # whitespace is trimmed, not guessed
            return
        response = get(query)
        assert response.status_code == 404 and error_code(response) == 'UNKNOWN_COUNTRY'
        assert 'INR' not in response.content.decode()

    def test_an_unknown_city_is_a_404(self, markets):
        response = get('?city=IN-XX-NOWHERE')
        assert response.status_code == 404 and error_code(response) == 'UNKNOWN_CITY'

    def test_a_city_in_the_wrong_country_is_a_404(self, markets):
        assert get('?country=US&city=IN-TG-HYD').status_code == 404

    def test_a_bad_app_is_a_400(self, markets):
        assert get('?country=IN&app=console').status_code == 400


class TestSafety:
    def test_no_pii_and_no_secret(self, markets, settings):
        planted = {'CASHFREE_SECRET_KEY': 'cf-secret-planted', 'AWS_SECRET_ACCESS_KEY': 'aws-planted',
                   'GOOGLE_MAPS_API_KEY': 'maps-key-planted', 'SENTRY_DSN': 'https://k@sentry.test/1'}
        for name, value in planted.items():
            setattr(settings, name, value)
        text = get('?country=IN').content.decode()
        for value in planted.values():
            assert value not in text
        body = json.loads(text)['data']

        def keys(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    yield k
                    yield from keys(v)
            elif isinstance(node, list):
                for item in node:
                    yield from keys(item)

        bad = [k for k in keys(body) if any(w in k.lower() for w in
                                            ('phone_number', 'email', 'secret', 'token',
                                             'password', 'address', 'user'))]
        assert bad == []

    def test_it_is_public_cacheable_and_read_only(self, markets):
        response = get('?country=IN')
        assert response.status_code == 200
        assert 'public' in response['Cache-Control'] and 'max-age' in response['Cache-Control']
        assert APIClient().post(URL, {}, format='json').status_code == 405

    def test_it_is_rate_limited_per_client_address(self, markets):
        cache.clear()
        statuses = {get('?country=IN', HTTP_X_FORWARDED_FOR='198.51.100.7').status_code
                    for _ in range(130)}
        assert 429 in statuses
        assert get('?country=IN', HTTP_X_FORWARDED_FOR='198.51.100.8').status_code == 200


class TestVersionsAndNotices:
    def test_the_version_policy_and_active_notices_are_in_the_document(self, markets):
        from servers.platform_ops.models import MaintenanceNotice

        AppVersionPolicy.objects.create(app='rider', platform='android', minimum_version='1.2.0',
                                        latest_version='1.4.0', store_url='https://play.test/r',
                                        message='Please update')
        now = timezone.now()
        MaintenanceNotice.objects.create(
            audience='rider', starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=1),
            message='Short outage tonight', severity='warning')
        MaintenanceNotice.objects.create(
            audience='driver', starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=1),
            message='Driver only')

        body = data(get('?country=IN&app=rider'))
        assert body['app_versions']['rider']['android']['minimum_version'] == '1.2.0'
        assert 'driver' not in body['app_versions']
        assert [n['message'] for n in body['notices']] == ['Short outage tonight']
        both = data(get('?country=IN'))
        assert {n['message'] for n in both['notices']} == {'Short outage tonight', 'Driver only'}


class TestServerSideGate:
    """426 APP_UPDATE_REQUIRED only when X-App-Version is PRESENT and below the minimum."""

    @pytest.fixture(autouse=True)
    def policy(self, markets):
        AppVersionPolicy.objects.create(app='rider', platform='android', minimum_version='2.0.0',
                                        latest_version='2.1.0', store_url='https://play.test/r',
                                        message='Please update the app')
        cache.clear()

    ESTIMATE = {'pickup_lat': 17.45, 'pickup_long': 78.40, 'destination_lat': 17.46,
                'destination_long': 78.41, 'distance_km': 3, 'duration_min': 9,
                'vehicle_type': 'sedan'}

    def estimate(self, **headers):
        return APIClient().post('/api/v1/ride/estimate-fare/', self.ESTIMATE, format='json',
                                **headers)

    def test_a_build_below_the_minimum_is_told_to_update(self):
        response = self.estimate(HTTP_X_APP_VERSION='1.9.9', HTTP_X_APP_PLATFORM='android')
        assert response.status_code == 426 and error_code(response) == 'APP_UPDATE_REQUIRED'
        update = response.json()['error']['update']
        assert update['minimum_version'] == '2.0.0' and update['store_url'] == 'https://play.test/r'

    def test_an_absent_header_is_allowed_so_current_apps_keep_working(self):
        assert self.estimate().status_code != 426

    @pytest.mark.parametrize('headers', [
        {'HTTP_X_APP_VERSION': '2.0.0', 'HTTP_X_APP_PLATFORM': 'android'},      # exactly the minimum
        {'HTTP_X_APP_VERSION': '2.3.1+7', 'HTTP_X_APP_PLATFORM': 'android'},
        {'HTTP_X_APP_VERSION': 'garbage', 'HTTP_X_APP_PLATFORM': 'android'},    # unparseable
        {'HTTP_X_APP_VERSION': '1.0.0', 'HTTP_X_APP_PLATFORM': 'ios'},          # no policy for ios
        {'HTTP_X_APP_VERSION': '1.0.0'},                                        # no platform
    ])
    def test_everything_else_is_allowed(self, headers):
        assert self.estimate(**headers).status_code != 426

    def test_the_same_gate_guards_sign_in(self):
        response = APIClient().post('/api/v1/auth/otp/', {'phone_number': '+919876543210',
                                                          'role': 'rider'}, format='json',
                                    HTTP_X_APP_VERSION='1.0.0', HTTP_X_APP_PLATFORM='android')
        assert response.status_code == 426
        driver = APIClient().post('/api/v1/auth/otp/', {'phone_number': '+919876543210',
                                                        'role': 'driver'}, format='json',
                                  HTTP_X_APP_VERSION='1.0.0', HTTP_X_APP_PLATFORM='android')
        assert driver.status_code != 426            # no driver policy exists
