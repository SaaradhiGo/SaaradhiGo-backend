"""The app language a user picked is kept on the account (2026-11-06).

Nullable: unset until the app sends it. The apps PATCH it when the user picks
a language and read it at sign-in when the device has no choice saved.
"""

import pytest

pytestmark = pytest.mark.django_db

UPDATE = '/api/v1/auth/update/'
PROFILE = '/api/v1/auth/profile/'


def _profile(client):
    body = client.get(PROFILE).json()
    data = body.get('data', body)
    return data.get('user', data)


def test_unset_until_the_app_sends_it(auth_client_rider):
    client, _ = auth_client_rider
    assert _profile(client)['preferred_language'] is None


def test_the_app_saves_and_reads_it_back(auth_client_rider):
    client, user = auth_client_rider
    assert client.patch(UPDATE, {'preferred_language': 'te'}, format='json').status_code == 200
    user.refresh_from_db()
    assert user.preferred_language == 'te'
    assert _profile(client)['preferred_language'] == 'te'


def test_a_language_the_apps_do_not_offer_is_refused(auth_client_rider):
    client, user = auth_client_rider
    assert client.patch(UPDATE, {'preferred_language': 'fr'}, format='json').status_code == 400
    user.refresh_from_db()
    assert user.preferred_language is None
