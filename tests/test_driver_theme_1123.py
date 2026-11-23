"""Drivers choose Light or Dark and it is saved to their account, like the console (23 Nov).

`/api/v1/auth/theme/` is the self-service setting for any signed-in account; it stores the same
`UserPreference.preferred_theme` the console's `/api/v1/auth/console-preferences/` does.
"""

import pytest
from rest_framework.test import APIClient

from finance_world import client_for, make_driver, make_rider
from servers.auth_user.models import UserPreference
from test_sos_support_zones_matrix import geo, operator  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db
THEME = '/api/v1/auth/theme/'
CONSOLE = '/api/v1/auth/console-preferences/'


def test_a_driver_is_light_by_default_and_keeps_dark_once_chosen(geo):
    driver = make_driver(geo, 'IN-TG-HYD', 'th', currency='INR', calling='91')
    client = client_for(driver.user_id)
    assert client.get(THEME).json()['data']['theme'] == 'light'
    assert client.patch(THEME, {'theme': 'Dark'}, format='json').json()['data']['theme'] == 'dark'
    assert UserPreference.objects.get(user=driver.user_id).preferred_theme == 'dark'
    assert client_for(driver.user_id).get(THEME).json()['data']['theme'] == 'dark'      # a new session
    assert client.patch(THEME, {'theme': 'light'}, format='json').json()['data']['theme'] == 'light'


def test_a_rider_may_choose_too():
    rider = make_rider('th', '91')
    assert client_for(rider).patch(THEME, {'theme': 'dark'}, format='json').status_code == 200
    assert UserPreference.objects.get(user=rider).preferred_theme == 'dark'


def test_only_light_or_dark(geo):
    driver = make_driver(geo, 'IN-TG-HYD', 'tv', currency='INR', calling='91')
    client = client_for(driver.user_id)
    for bad in ('purple', 'auto', ''):
        r = client.patch(THEME, {'theme': bad}, format='json')
        assert r.status_code == 400 and r.json()['error']['code'] == 'INVALID_THEME'
    assert not UserPreference.objects.filter(user=driver.user_id).exists()


def test_signed_out_is_refused():
    assert APIClient().get(THEME).status_code == 401
    assert APIClient().patch(THEME, {'theme': 'dark'}, format='json').status_code == 401


def test_it_is_the_one_choice_the_console_reads(geo):
    client, user = operator('city_ops_manager', geo)
    client.patch(THEME, {'theme': 'dark'}, format='json')
    assert client.get(CONSOLE).json()['data']['theme'] == 'dark'
    client.patch(CONSOLE, {'theme': 'light'}, format='json')
    assert client.get(THEME).json()['data']['theme'] == 'light'


def test_the_console_endpoint_still_refuses_a_driver(geo):
    driver = make_driver(geo, 'IN-TG-HYD', 'tc', currency='INR', calling='91')
    assert client_for(driver.user_id).get(CONSOLE).status_code == 403
