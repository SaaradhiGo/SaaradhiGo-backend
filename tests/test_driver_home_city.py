"""A self-signed-up driver chooses a home city; sign-up applies the driver-name rule (AUDIT 27.76).

Found by the QA payout run: a driver who signed up by email (or OTP) had no way to set a home city,
so every city-scoped operator (KYC, Finance) got 404 for them and their KYC could only be approved by
a Super Admin. And email sign-up stored a name the driver-name rule refuses, which profile completion
then rejected.
"""
import pytest

from servers.driver.models import Driver
from test_sos_support_zones_matrix import geo  # noqa: F401 - fixture

pytestmark = pytest.mark.django_db

URL = '/api/v1/auth/update/'
REGISTER = '/api/v1/auth/email/register/'


def _city(user):
    return Driver.objects.get(user_id=user).home_city


class TestDriverHomeCity:
    def test_an_onboarding_driver_sets_a_live_city(self, auth_client_driver, geo):
        client, user = auth_client_driver
        Driver.objects.filter(user_id=user).update(approved=False, home_city=None)
        resp = client.patch(URL, {'home_city': 'IN-TG-HYD'}, format='json')
        assert resp.status_code == 200, resp.content
        assert _city(user) == geo['IN-TG-HYD']

    def test_with_the_rest_of_the_profile(self, auth_client_driver, geo):
        client, user = auth_client_driver
        Driver.objects.filter(user_id=user).update(approved=False, home_city=None)
        resp = client.patch(URL, {'full_name': 'Ravi Kumar', 'home_city': 'IN-TG-HYD'},
                            format='json')
        assert resp.status_code == 200
        assert _city(user) == geo['IN-TG-HYD']

    def test_an_unknown_city_is_refused_and_nothing_saved(self, auth_client_driver, geo):
        client, user = auth_client_driver
        Driver.objects.filter(user_id=user).update(approved=False, home_city=None)
        resp = client.patch(URL, {'home_city': 'XX-NOPE', 'full_name': 'Ravi Kumar'},
                            format='json')
        assert resp.status_code == 400
        assert resp.json()['error']['details']['field'] == 'home_city'
        assert _city(user) is None

    def test_an_approved_driver_cannot_move_themselves(self, auth_client_driver, geo):
        client, user = auth_client_driver
        Driver.objects.filter(user_id=user).update(approved=True, home_city=geo['IN-TG-HYD'])
        resp = client.patch(URL, {'home_city': 'US-TX-AUS'}, format='json')
        assert resp.status_code == 400
        assert 'support' in resp.json()['error']['message']
        assert _city(user) == geo['IN-TG-HYD']

    def test_the_city_makes_the_driver_visible_to_the_citys_kyc_team(self, auth_client_driver,
                                                                    geo):
        from finance_world import operator

        client, user = auth_client_driver
        Driver.objects.filter(user_id=user).update(approved=False, home_city=None)
        driver = Driver.objects.get(user_id=user)
        kyc, _ = operator('kyc_verifier', geo, 'IN-TG-HYD')
        assert kyc.get(f'/api/v1/driver/admin/{driver.id}/').status_code == 404
        client.patch(URL, {'home_city': 'IN-TG-HYD'}, format='json')
        assert kyc.get(f'/api/v1/driver/admin/{driver.id}/').status_code == 200


class TestSignUpName:
    def _register(self, client, name, role='driver'):
        return client.post(REGISTER, {
            'full_name': name, 'phone_number': '+919811100771', 'email': 'new.driver@example.test',
            'password': 'Plain-test-pass-1', 'confirm_password': 'Plain-test-pass-1',
            'role': role}, format='json')

    def test_a_driver_name_breaking_the_rule_is_refused_at_sign_up(self, api_client):
        r = self._register(api_client, 'E2E Test Driver')
        assert r.status_code == 400
        assert r.json()['error']['details']['field'] == 'full_name'

    def test_a_valid_driver_name_signs_up(self, api_client):
        assert self._register(api_client, 'Ravi Kumar').status_code in (201, 202)

    def test_a_rider_name_is_not_held_to_the_driver_rule(self, api_client):
        assert self._register(api_client, 'Rider 2', role='rider').status_code in (201, 202)
