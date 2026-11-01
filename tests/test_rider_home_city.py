"""R6 (AUDIT §11a): the rider app sends the city chosen at sign-up as `home_city`.

PATCH /auth/update/ takes a live city code from app-config `cities[]` and sets
`Rider.home_city`, the anchor for operator scope. Anything else is refused;
drivers and requests without the key are unaffected.
"""
import pytest

from servers.admin_access.models import GeoNode
from servers.rider.models import Rider
from test_sos_support_zones_matrix import geo  # noqa: F401 - fixture

pytestmark = pytest.mark.django_db

URL = '/api/v1/auth/update/'


def _home_city(user):
    return Rider.objects.get(user_id=user).home_city


class TestRiderHomeCity:
    def test_a_live_city_code_sets_home_city_with_the_rest_of_the_profile(
            self, auth_client_rider, geo):
        client, user = auth_client_rider
        resp = client.patch(URL, {'full_name': 'Asha Rao', 'home_city': 'IN-TG-HYD'},
                            format='json')
        assert resp.status_code == 200
        assert _home_city(user) == geo['IN-TG-HYD']
        user.refresh_from_db()
        assert user.full_name == 'Asha Rao'

    def test_home_city_alone_is_enough(self, auth_client_rider, geo):
        client, user = auth_client_rider
        resp = client.patch(URL, {'home_city': 'IN-TG-HYD'}, format='json')
        assert resp.status_code == 200
        assert _home_city(user) == geo['IN-TG-HYD']

    @pytest.mark.parametrize('code', ['NOPE', 'IN', 'IN-TG'])
    def test_an_unknown_code_or_a_non_city_is_refused_and_nothing_is_saved(
            self, auth_client_rider, geo, code):
        client, user = auth_client_rider
        resp = client.patch(URL, {'full_name': 'Asha Rao', 'home_city': code},
                            format='json')
        assert resp.status_code == 400
        assert resp.json()['error']['details']['field'] == 'home_city'
        assert _home_city(user) is None
        user.refresh_from_db()
        assert user.full_name != 'Asha Rao'

    def test_a_city_that_is_not_live_is_refused(self, auth_client_rider, geo):
        client, user = auth_client_rider
        GeoNode.objects.filter(code='IN-TG-HYD').update(status='paused')
        resp = client.patch(URL, {'home_city': 'IN-TG-HYD'}, format='json')
        assert resp.status_code == 400
        assert _home_city(user) is None

    def test_a_blank_code_is_ignored(self, auth_client_rider, geo):
        client, user = auth_client_rider
        resp = client.patch(URL, {'full_name': 'Asha Rao', 'home_city': ''}, format='json')
        assert resp.status_code == 200
        assert _home_city(user) is None

    def test_without_the_key_home_city_is_untouched(self, auth_client_rider, geo):
        client, user = auth_client_rider
        Rider.objects.filter(user_id=user).update(home_city=geo['IN-TG-HYD'])
        resp = client.patch(URL, {'full_name': 'Asha Rao'}, format='json')
        assert resp.status_code == 200
        assert _home_city(user) == geo['IN-TG-HYD']

    def test_a_driver_sending_home_city_is_not_affected(self, auth_client_driver, geo):
        client, user = auth_client_driver
        resp = client.patch(URL, {'full_name': 'Ravi Kumar', 'home_city': 'NOPE'},
                            format='json')
        assert resp.status_code == 200
