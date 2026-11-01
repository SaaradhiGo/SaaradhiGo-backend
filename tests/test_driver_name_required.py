"""D7 (owner ruling, AUDIT 27.62): a driver's name is required at sign-up.

A driver account is created by the OTP login with no name; the profile is
completed with PATCH /auth/update/. The rule lives there and applies only to
drivers. Accounts that predate it can still save unrelated changes.
"""
import pytest
from django.contrib.auth import get_user_model

URL = '/api/v1/auth/update/'


def _error_field(resp):
    return resp.json()['error']['details']['field']


@pytest.mark.django_db
class TestNewDriverSignUp:
    def test_completing_the_profile_without_a_name_is_refused(
            self, auth_client_driver):
        client, user = auth_client_driver
        resp = client.patch(
            URL, {'is_updated': True, 'email': 'd@example.test'}, format='json')
        assert resp.status_code == 400
        assert _error_field(resp) == 'full_name'
        user.refresh_from_db()
        assert user.is_updated is False

    @pytest.mark.parametrize('bad', ['', '   ', '9876543210', '+91 98765-43210',
                                     'a@b.com'])
    def test_a_blank_or_stand_in_name_is_refused(self, auth_client_driver, bad):
        client, user = auth_client_driver
        resp = client.patch(
            URL, {'is_updated': True, 'full_name': bad}, format='json')
        assert resp.status_code == 400
        assert _error_field(resp) == 'full_name'
        user.refresh_from_db()
        assert not user.full_name
        assert user.is_updated is False

    def test_a_real_name_completes_the_profile(self, auth_client_driver):
        client, user = auth_client_driver
        resp = client.patch(
            URL,
            {'is_updated': True, 'full_name': 'Ravi Kumar', 'gender': 'male'},
            format='json',
        )
        assert resp.status_code == 200
        user.refresh_from_db()
        assert user.full_name == 'Ravi Kumar'
        assert user.is_updated is True

    def test_multipart_is_updated_string_is_read_as_true(
            self, auth_client_driver):
        client, _ = auth_client_driver
        resp = client.patch(URL, {'is_updated': 'true'}, format='multipart')
        assert resp.status_code == 400
        assert _error_field(resp) == 'full_name'


@pytest.mark.django_db
class TestExistingDrivers:
    def test_unrelated_save_without_a_name_still_works(self, auth_client_driver):
        client, user = auth_client_driver
        user.is_updated = True
        user.save(update_fields=['is_updated'])
        resp = client.patch(URL, {'city': 'Hyderabad'}, format='json')
        assert resp.status_code == 200
        user.refresh_from_db()
        assert user.city == 'Hyderabad'
        assert not user.full_name

    def test_re_sending_is_updated_on_an_old_account_is_not_blocked(
            self, auth_client_driver):
        client, user = auth_client_driver
        user.is_updated = True
        user.save(update_fields=['is_updated'])
        resp = client.patch(
            URL, {'is_updated': True, 'email': 'd@example.test'}, format='json')
        assert resp.status_code == 200

    def test_a_named_driver_can_save_with_or_without_the_name(
            self, auth_client_driver):
        client, user = auth_client_driver
        user.full_name = 'Ravi Kumar'
        user.save(update_fields=['full_name'])
        assert client.patch(
            URL, {'is_updated': True}, format='json').status_code == 200
        assert client.patch(
            URL, {'full_name': 'Ravi K'}, format='json').status_code == 200

    def test_an_existing_name_cannot_be_blanked(self, auth_client_driver):
        client, user = auth_client_driver
        user.full_name = 'Ravi Kumar'
        user.save(update_fields=['full_name'])
        resp = client.patch(URL, {'full_name': ' '}, format='json')
        assert resp.status_code == 400
        user.refresh_from_db()
        assert user.full_name == 'Ravi Kumar'


@pytest.mark.django_db
class TestRidersAreUnaffected:
    def test_a_rider_can_still_save_without_a_name(self, auth_client_rider):
        client, user = auth_client_rider
        resp = client.patch(
            URL, {'is_updated': True, 'city': 'Hyderabad'}, format='json')
        assert resp.status_code == 200
        resp = client.patch(URL, {'full_name': ''}, format='json')
        assert resp.status_code == 200
        assert get_user_model().objects.get(pk=user.pk).role == 'rider'
