"""The console theme is the person's own choice, saved to their account; light by default (22 Nov)."""

import pytest

from servers.auth_user.models import UserPreference
from test_sos_support_zones_matrix import geo, operator  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db
PREFS = '/api/v1/auth/admin/preferences/'


def test_light_by_default_and_dark_once_chosen(geo):
    client, user = operator('city_ops_manager', geo)
    assert client.get(PREFS).json()['data']['theme'] == 'light'
    assert client.patch(PREFS, {'theme': 'dark'}, format='json').json()['data']['theme'] == 'dark'
    assert UserPreference.objects.get(user=user).preferred_theme == 'dark'
    assert client.get(PREFS).json()['data']['theme'] == 'dark'


def test_only_light_or_dark(geo):
    client, _ = operator('city_ops_manager', geo)
    assert client.patch(PREFS, {'theme': 'purple'}, format='json').status_code == 400


def test_the_reset_puts_everyone_back_on_light(geo):
    from io import StringIO

    from django.core.management import call_command

    _, user = operator('city_ops_manager', geo)
    UserPreference.objects.create(user=user, preferred_theme='dark')
    call_command('reset_themes_to_light', confirm=True, actor_email=user.email, stdout=StringIO())
    assert UserPreference.objects.get(user=user).preferred_theme == 'light'
