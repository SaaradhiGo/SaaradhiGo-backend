"""Load, e2e and CI runs never call real Google Maps (17 Nov, PROD round 2).

The 16 Nov pilot load on QA made about 1,400 real Routes calls. MAPS_ADAPTER='fake' answers every
Maps call from servers/ride/maps_fake.py; a load or e2e boot holding a real key is refused, and so
is the fake in production; /healthz names the adapter so a load script can refuse a live target.
"""
from unittest import mock

import pytest
from django.test import override_settings

from servers.ride import maps_utils
from tests.test_production_boot_guards import _boot


def _no_network():
    boom = mock.Mock(side_effect=AssertionError('the fake maps adapter made a network call'))
    return mock.patch.multiple('servers.ride.maps_utils.requests', get=boom, post=boom)


@override_settings(MAPS_ADAPTER='fake', GOOGLE_MAPS_API_KEY='')
def test_every_maps_call_is_answered_by_the_fake_with_no_network():
    with _no_network():
        auto = maps_utils.google_places_autocomplete('Charminar', location='17.36,78.47')
        assert auto['status'] == 'OK' and len(auto['predictions']) == 3
        assert auto['predictions'][0]['structured_formatting']['main_text'] == 'Charminar 1'

        details = maps_utils.google_place_details(auto['predictions'][0]['place_id'])
        assert details['status'] == 'OK'
        assert details['location'] == pytest.approx({'lat': 17.37, 'lng': 78.482})

        geo = maps_utils.google_geocode(latlng='17.44,78.38')
        assert geo['results'][0]['geometry']['location'] == pytest.approx({'lat': 17.44, 'lng': 78.38})

        route = maps_utils.google_directions(17.4435, 78.3772, 17.3616, 78.4747)
        leg = route['routes'][0]['legs'][0]
        assert 17000 < leg['distance']['value'] < 20000      # ~13.8 km straight x 1.3
        assert leg['duration']['value'] > 0 and leg['distance']['text'].endswith(' km')
    assert maps_utils.maps_adapter_status() == 'fake'
    assert maps_utils.maps_configured()


@pytest.mark.parametrize('key, expected', [('', 'not_configured'), ('a-test-value', 'google')])
def test_the_adapter_status_without_the_fake(key, expected):
    with override_settings(MAPS_ADAPTER='google', GOOGLE_MAPS_API_KEY=key):
        assert maps_utils.maps_adapter_status() == expected


@pytest.mark.django_db
@override_settings(MAPS_ADAPTER='fake')
def test_healthz_names_the_maps_adapter(client):
    assert client.get('/healthz').json()['maps'] == 'fake'


def test_a_load_boot_holding_a_real_key_is_refused():
    rc, out = _boot(ENVIRONMENT='load', DEBUG_ENV='True', GOOGLE_MAPS_API_KEY='a-test-value')
    assert rc != 0 and 'must not hold a real GOOGLE_MAPS_API_KEY' in out


def test_a_load_boot_defaults_to_the_fake():
    code = ('import django; django.setup(); from django.conf import settings; '
            'print("ADAPTER", settings.MAPS_ADAPTER)')
    rc, out = _boot(code, ENVIRONMENT='load', DEBUG_ENV='True', GOOGLE_MAPS_API_KEY=None)
    assert rc == 0 and 'ADAPTER fake' in out, out[-400:]


def test_production_refuses_the_fake():
    rc, out = _boot(MAPS_ADAPTER='fake')
    assert rc != 0 and 'never production' in out


def test_ci_cannot_reach_google():
    import socket

    with pytest.raises(RuntimeError, match='never calls real Google Maps'):
        socket.getaddrinfo('routes.googleapis.com', 443)
