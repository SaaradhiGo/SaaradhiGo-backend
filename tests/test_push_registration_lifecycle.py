"""A device token's whole life: registered at login, dropped at logout.

WHAT WAS BROKEN
---------------
1. **Nothing could configure Firebase.** `services.py` looked only for a
   service-account FILE, at `FIREBASE_CREDENTIALS_PATH` -- a setting that did
   not exist in `base/settings.py`. The file is gitignored and is not in the
   deployed image, so `firebase_admin` was never initialised in any
   deployment and `send_push_notification_task` returned False at its
   `_apps` check before it ever looked at a token. Push has therefore never
   worked for either app, however correct the clients were.

2. **Logout left the token behind.** The refresh token was blacklisted but
   `fcm_token` stayed on the row, so dispatch kept addressing ride offers to
   a handset whose session had ended -- and to whoever held that phone next.

Both are fixed; these tests hold them fixed.
"""

import json
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

pytestmark = pytest.mark.django_db

DEVICE_TOKEN = 'fake-device-token-for-tests-0000000000000000'


@pytest.fixture
def driver_user():
    User = get_user_model()
    return User.objects.create_user(
        username='+919999000111',
        phone_number='+919999000111',
        password='Cadence-Harbour-74',
        role='driver',
    )


def _auth(client, user):
    refresh = RefreshToken.for_user(user)
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {refresh.access_token}')
    return str(refresh)


class TestLogoutDropsTheDeviceToken:
    def test_the_token_is_cleared(self, driver_user):
        driver_user.fcm_token = DEVICE_TOKEN
        driver_user.save(update_fields=['fcm_token'])

        client = APIClient()
        refresh = _auth(client, driver_user)

        response = client.post('/api/v1/auth/logout/',
                               {'refresh_token': refresh}, format='json')

        assert response.status_code == 200, response.data
        driver_user.refresh_from_db()
        assert driver_user.fcm_token is None, (
            'dispatch would still address ride offers to a handset whose '
            'session has ended'
        )

    def test_logout_still_succeeds_when_the_clear_fails(self, driver_user):
        # A logout must never fail because a push token could not be dropped.
        # The refresh token is already blacklisted by then; refusing the
        # response would leave the client believing it is still signed in.
        driver_user.fcm_token = DEVICE_TOKEN
        driver_user.save(update_fields=['fcm_token'])

        client = APIClient()
        refresh = _auth(client, driver_user)

        with mock.patch(
            'django.db.models.query.QuerySet.update',
            side_effect=RuntimeError('database went away'),
        ):
            response = client.post('/api/v1/auth/logout/',
                                   {'refresh_token': refresh}, format='json')

        assert response.status_code == 200, response.data

    def test_the_token_is_never_echoed_in_the_response(self, driver_user):
        driver_user.fcm_token = DEVICE_TOKEN
        driver_user.save(update_fields=['fcm_token'])

        client = APIClient()
        refresh = _auth(client, driver_user)

        response = client.post('/api/v1/auth/logout/',
                               {'refresh_token': refresh}, format='json')

        # The rendered body, which is what actually goes over the wire.
        assert DEVICE_TOKEN not in response.content.decode(), (
            'a device token is a credential: anyone holding one can push to '
            'that device'
        )


class TestLoginRegistersTheDeviceToken:
    def test_a_login_without_a_device_token_does_not_wipe_the_stored_one(
            self, driver_user):
        # The driver app omits `device_token` whenever FCM is unavailable --
        # no Play Services, permission declined, Firebase not configured --
        # all documented, survivable states. A login in one of them must not
        # cost the driver their push registration.
        driver_user.fcm_token = DEVICE_TOKEN
        driver_user.save(update_fields=['fcm_token'])

        client = APIClient()
        _auth(client, driver_user)

        # Exercise the serializer path the profile update uses, with no
        # device_token present at all.
        response = client.patch('/api/v1/auth/update/',
                                {'full_name': 'Test Driver'}, format='json')

        assert response.status_code in (200, 202), response.data
        driver_user.refresh_from_db()
        assert driver_user.fcm_token == DEVICE_TOKEN

    def test_a_refreshed_token_replaces_the_old_one(self, driver_user):
        # FCM rotates tokens on reinstall, device restore and long idle
        # periods. The client pushes the new one through /auth/update/; a
        # token the server keeps after a rotation is simply dead.
        driver_user.fcm_token = 'stale-token-from-a-previous-install'
        driver_user.save(update_fields=['fcm_token'])

        client = APIClient()
        _auth(client, driver_user)

        response = client.patch('/api/v1/auth/update/',
                                {'fcm_token': DEVICE_TOKEN}, format='json')

        assert response.status_code in (200, 202), response.data
        driver_user.refresh_from_db()
        assert driver_user.fcm_token == DEVICE_TOKEN

    def test_the_token_is_write_only_and_never_read_back(self, driver_user):
        driver_user.fcm_token = DEVICE_TOKEN
        driver_user.save(update_fields=['fcm_token'])

        client = APIClient()
        _auth(client, driver_user)

        response = client.get('/api/v1/auth/profile/')

        assert response.status_code == 200, response.data
        assert DEVICE_TOKEN not in response.content.decode()


class TestFirebaseCredentialResolution:
    """The reason push was dead everywhere: nothing could configure it."""

    def test_the_json_environment_variable_is_preferred(self, settings, tmp_path):
        from servers.auth_user import services

        service_account = {
            'type': 'service_account',
            'project_id': 'saaradhigo-test',
            'private_key_id': 'abc',
            'client_email': 'test@saaradhigo-test.iam.gserviceaccount.com',
        }
        settings.FIREBASE_CREDENTIALS_JSON = json.dumps(service_account)

        with mock.patch.object(
            services.credentials, 'Certificate', return_value='CERT',
        ) as certificate:
            cred, source = services._firebase_credentials()

        assert cred == 'CERT'
        assert source == 'FIREBASE_CREDENTIALS_JSON'
        # Passed as a parsed dict, not a path.
        certificate.assert_called_once_with(service_account)

    def test_unusable_json_is_reported_and_does_not_raise(self, settings, caplog):
        from servers.auth_user import services

        settings.FIREBASE_CREDENTIALS_JSON = '{not valid json'

        cred, source = services._firebase_credentials()

        assert cred is None and source is None
        assert 'FIREBASE_CREDENTIALS_JSON is set but unusable' in caplog.text

    def test_the_private_key_is_never_logged(self, settings, caplog):
        from servers.auth_user import services

        secret = '-----BEGIN PRIVATE KEY-----VERYSECRETVALUE'
        settings.FIREBASE_CREDENTIALS_JSON = secret  # not valid JSON

        services._firebase_credentials()

        assert 'VERYSECRETVALUE' not in caplog.text

    def test_a_missing_file_is_not_an_error(self, settings):
        from servers.auth_user import services

        settings.FIREBASE_CREDENTIALS_JSON = ''
        settings.FIREBASE_CREDENTIALS_PATH = 'definitely-not-here.json'

        cred, source = services._firebase_credentials()

        assert cred is None and source is None

    def test_a_file_is_used_when_no_json_is_set(self, settings, tmp_path):
        from servers.auth_user import services

        path = tmp_path / 'firebase-credentials.json'
        path.write_text('{}', encoding='utf-8')
        settings.FIREBASE_CREDENTIALS_JSON = ''
        settings.FIREBASE_CREDENTIALS_PATH = str(path)

        with mock.patch.object(
            services.credentials, 'Certificate', return_value='CERT',
        ) as certificate:
            cred, source = services._firebase_credentials()

        assert cred == 'CERT'
        assert source == str(path)
        certificate.assert_called_once_with(str(path))


class TestPushIsSkippedCleanlyWhenUnconfigured:
    def test_the_task_reports_failure_rather_than_raising(self, driver_user):
        # This is every deployment today. It must be a quiet, logged skip --
        # a push is the fallback channel for a ride offer and must never take
        # the dispatch path down with it.
        from servers.auth_user.services import send_push_notification_task

        driver_user.fcm_token = DEVICE_TOKEN
        driver_user.save(update_fields=['fcm_token'])

        with mock.patch('servers.auth_user.services.firebase_admin._apps', {}):
            result = send_push_notification_task.apply(
                args=(driver_user.id, 'New Ride Request', 'from Test Rider',
                      {'trip_id': '42', 'type': 'ride_request'}),
            ).get()

        assert result is False


class TestTheOfferPushPayload:
    """What an offer push carries, and what it must never carry.

    A notification is rendered on a lock screen: anyone holding the handset
    reads it without unlocking. So the push is deliberately sparse -- it
    exists to wake the app, and the socket frame briefs it once the driver
    has unlocked.
    """

    def test_it_keeps_the_two_fields_the_app_has_always_read(self):
        from servers.ride.dispatch import _offer_push_data

        data = _offer_push_data(4210, None)

        assert data['trip_id'] == '4210'
        assert data['type'] == 'ride_request'
        # Stringified, because send_push_notification_task stringifies every
        # value and the app parses trip_id as a String over push.
        assert isinstance(data['trip_id'], str)

    def test_it_carries_an_expiry_the_client_no_longer_has_to_guess(
            self, settings):
        # The driver app had no expiry in the payload, so it compared the FCM
        # send time against a hard-coded 90 seconds copied from this setting.
        # That is a guess about server behaviour living in a client.
        from datetime import datetime

        from servers.ride.dispatch import _offer_push_data

        settings.TRIP_ACCEPT_TIMEOUT_SECONDS = 120

        data = _offer_push_data(4210, None)

        assert 'expires_at' in data
        expires = datetime.fromisoformat(data['expires_at'])
        assert expires.tzinfo is not None, 'an expiry without a timezone is ambiguous'

    def test_the_expiry_follows_the_setting_rather_than_a_constant(
            self, settings):
        from datetime import datetime

        from django.utils import timezone

        from servers.ride.dispatch import _offer_push_data

        settings.TRIP_ACCEPT_TIMEOUT_SECONDS = 300
        data = _offer_push_data(4210, None)

        seconds = (
            datetime.fromisoformat(data['expires_at']) - timezone.now()
        ).total_seconds()
        assert 280 < seconds < 320, seconds

    def test_the_vehicle_class_is_included_when_known(self):
        from servers.ride.dispatch import _offer_push_data

        data = _offer_push_data(4210, {'vehicle_type': 'sedan'})

        assert data['vehicle_type'] == 'sedan'

    def test_no_rider_name_address_coordinate_or_fare_is_included(self):
        """The whole point of keeping this payload sparse."""
        from servers.ride.dispatch import _offer_push_data

        offer = {
            'rider_name': 'Priya Sharma',
            'pickup_address': '14 Lavelle Road, Bengaluru',
            'destination_address': 'Indiranagar',
            'pickup_lat': '12.9352',
            'pickup_lng': '77.6245',
            'estimated_fare': '192.00',
            'vehicle_type': 'sedan',
            'payment_method': 'cash',
        }

        data = _offer_push_data(4210, offer)

        blob = str(data).lower()
        for leaked in [
            'priya', 'sharma', 'lavelle', 'indiranagar',
            '12.9352', '77.6245', '192.00',
        ]:
            assert leaked.lower() not in blob, (
                f'"{leaked}" would be shown on a lock screen'
            )
        # Only the four agreed keys.
        assert set(data) <= {'trip_id', 'type', 'expires_at', 'vehicle_type'}

    def test_a_missing_offer_is_not_an_error(self):
        from servers.ride.dispatch import _offer_push_data

        for offer in [None, {}, 'not-a-dict', 42]:
            data = _offer_push_data(4210, offer)
            assert data['trip_id'] == '4210'
