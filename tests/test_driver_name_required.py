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


# ---------------------------------------------------------------------------
# AUDIT 27.71: one rule for the app and the server, at every gate
# ---------------------------------------------------------------------------

class TestTheRule:
    @pytest.mark.parametrize('good', ['Ravi Kumar', 'Ay', "D'Souza", 'Anne-Marie O.',
                                      'Rāmu Ūrmila', 'రాము', 'José Núñez'])
    def test_a_valid_name(self, good):
        from servers.driver.serializers import is_valid_driver_name
        assert is_valid_driver_name(good)

    @pytest.mark.parametrize('bad', ['', '   ', 'R', ' R ', '9876543210', 'a@b.com',
                                     'QA Driver 2', 'Ravi_K', '..', "-'-"])
    def test_an_invalid_name(self, bad):
        from servers.driver.serializers import is_valid_driver_name
        assert not is_valid_driver_name(bad)


@pytest.mark.django_db
class TestOnlyAGivenNameIsChecked:
    def _old_account(self, user, name='QA Driver 2'):
        user.full_name, user.is_updated = name, True
        user.save(update_fields=['full_name', 'is_updated'])

    def test_resending_the_stored_name_on_an_unrelated_save_works(self, auth_client_driver):
        client, user = auth_client_driver
        self._old_account(user)
        resp = client.patch(URL, {'full_name': 'QA Driver 2', 'city': 'Hyderabad'},
                            format='json')
        assert resp.status_code == 200
        user.refresh_from_db()
        assert user.city == 'Hyderabad'

    def test_a_new_name_with_digits_is_refused(self, auth_client_driver):
        client, user = auth_client_driver
        self._old_account(user)
        resp = client.patch(URL, {'full_name': 'QA Driver 3'}, format='json')
        assert resp.status_code == 400 and _error_field(resp) == 'full_name'
        user.refresh_from_db()
        assert user.full_name == 'QA Driver 2'

    def test_completing_with_a_stand_in_stored_name_is_refused(self, auth_client_driver):
        client, user = auth_client_driver
        user.full_name = 'Driver 7'
        user.save(update_fields=['full_name'])
        resp = client.patch(URL, {'is_updated': True}, format='json')
        assert resp.status_code == 400 and _error_field(resp) == 'full_name'

    def test_completing_while_resending_a_one_letter_name_is_refused(self, auth_client_driver):
        client, user = auth_client_driver
        user.full_name = 'R'
        user.save(update_fields=['full_name'])
        resp = client.patch(URL, {'is_updated': True, 'full_name': 'R'}, format='json')
        assert resp.status_code == 400 and _error_field(resp) == 'full_name'


@pytest.mark.django_db
class TestKycAndOnboardingNeedTheName:
    def _approvable(self, name):
        from django.utils import timezone

        from servers.driver.models import Driver, Vehicle, VehicleType

        user = get_user_model().objects.create_user(
            phone_number='+919800000861', role='driver', full_name=name)
        driver = Driver.objects.create(user_id=user, approved=False, status='off')
        vt, _ = VehicleType.objects.get_or_create(type='sedan')
        vehicle = Vehicle.objects.create(driver_id=driver, vehicle_type_id=vt,
                                         vehicle_number='TS09AB0861', rc_doc='rc_docs/x.jpg')
        driver.license_doc = 'license_docs/x.jpg'
        driver.license_expiry = timezone.localdate() + timezone.timedelta(days=365)
        driver.active_vehicle = vehicle
        driver.save()
        return driver

    @pytest.mark.parametrize('name, ok', [('Ravi Kumar', True), ('Driver 7', False),
                                          ('R', False), ('', False)])
    def test_kyc_approval_needs_a_valid_name(self, name, ok):
        from servers.driver.serializers import KYCApprovalSerializer

        serializer = KYCApprovalSerializer(self._approvable(name), data={'approved': True},
                                           partial=True)
        assert serializer.is_valid() is ok
        if not ok:
            assert 'full_name' in str(serializer.errors['documents'])

    @pytest.mark.parametrize('name, ok', [('Ravi Kumar', True), ('Driver 7', False),
                                          ('', False)])
    def test_the_onboarding_checklist_lists_the_name(self, name, ok):
        from rest_framework.test import APIClient

        driver = self._approvable(name)
        client = APIClient()
        client.force_authenticate(user=driver.user_id)
        body = client.get('/api/v1/driver/driver/onboarding/').json()['data']
        assert body['full_name_ok'] is ok
        assert ('full_name' in body['outstanding']) is not ok
        if not ok:
            assert body['ready_for_review'] is False and body['outstanding'][0] == 'full_name'
