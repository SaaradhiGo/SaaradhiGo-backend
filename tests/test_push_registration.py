"""B5 — registering a device for push, at the HTTP boundary.

WHY AT THE BOUNDARY
-------------------
The whole of push registration is four lines inside the login view. There is no
service layer to test, and the defects found here were both in how those lines
interact with the request -- an absent field, and a full-row save -- so they are
only visible through a real request.

THE TWO DEFECTS THIS FILE PINS
------------------------------
**A login without a device token used to erase the stored one.** `device_token` is
optional and defaulted to None, and the assignment was unconditional. The driver
app's own `PushService` documents four ordinary states in which it has no token to
send -- no Firebase config, no Play Services, permission not yet granted, no network
at launch -- and it omits the field in each. So a driver who logged in during any of
them silently lost push, and stayed without it until some later login happened to
carry a working token.

That matters because push is the fallback dispatch channel: it is what buzzes a
driver whose socket is down with the screen off. Losing it produces no error
anywhere. It produces a driver who stops getting offers in the background and
cannot say why.

**A bare `save()` wrote every column.** On the login view's error path `user` is
re-fetched after a failed create, so the instance can be stale, and a full-row write
from a stale instance is the same shape as the defect that used to cancel trips
drivers had already accepted.

AND ONE PRIVACY PROPERTY
------------------------
An FCM token is a device credential -- holding one is sufficient to push to that
handset. It must not appear in logs, and it must not be echoed back in API
responses, where it rode along in every payload the user serializer renders.
"""

import logging

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from servers.driver.models import Driver

User = get_user_model()

pytestmark = pytest.mark.django_db

LOGIN_URL = '/api/v1/auth/login/'
DRIVER_PHONE = '+919555000001'
DRIVER_OTP = '100777'
EXISTING_TOKEN = 'existing-device-token-abcdef'
NEW_TOKEN = 'rotated-device-token-123456'


@pytest.fixture(autouse=True)
def test_phone(settings):
    """Use the OTP bypass so these tests exercise the real login view.

    Mocking the OTP check would move the test above the code under test -- and
    the point of this file is that the defects were in the view.
    """
    settings.TEST_PHONE_NUMBERS = {DRIVER_PHONE: DRIVER_OTP}
    return settings


@pytest.fixture
def driver_user():
    u = User.objects.create_user(
        phone_number=DRIVER_PHONE, role='driver', username=DRIVER_PHONE,
        fcm_token=EXISTING_TOKEN,
    )
    Driver.objects.create(user_id=u)
    return u


OTP_URL = '/api/v1/auth/otp/'


def _login(**extra):
    """Request an OTP, then log in -- the real two-step flow.

    The bypass does not skip the OTP record; it only fixes the code and skips the
    SMS. Posting straight to /login/ returns AUTH_OTP_EXPIRED, which is correct
    behaviour and would have made every test here fail for the wrong reason.
    """
    client = APIClient()
    otp_resp = client.post(OTP_URL, {'phone_number': DRIVER_PHONE}, format='json')
    assert otp_resp.status_code == 200, (
        f'setup: could not request an OTP: {otp_resp.content[:200]}')
    body = {'phone_number': DRIVER_PHONE, 'otp': DRIVER_OTP, 'role': 'driver'}
    body.update(extra)
    return client.post(LOGIN_URL, body, format='json')


# ---------------------------------------------------------------------------
# The defect: an absent token must not erase a working one
# ---------------------------------------------------------------------------

def test_a_login_without_a_device_token_keeps_the_stored_one(driver_user):
    """The driver app omits the field whenever FCM is unavailable."""
    resp = _login()

    assert resp.status_code == 200, resp.content[:300]
    driver_user.refresh_from_db()
    assert driver_user.fcm_token == EXISTING_TOKEN, (
        'logging in without a device token erased the registered one; this '
        'driver now receives no background ride offers and nothing reports it'
    )


def test_an_explicit_null_device_token_also_keeps_the_stored_one(driver_user):
    """A client that sends the key with a null value must be treated the same.

    The driver app omits the key, but the rider app assigns `deviceToken` from a
    nullable getter, so an explicit null is a shape that really reaches this view.
    """
    resp = _login(device_token=None)

    assert resp.status_code == 200
    driver_user.refresh_from_db()
    assert driver_user.fcm_token == EXISTING_TOKEN


def test_an_empty_string_device_token_keeps_the_stored_one(driver_user):
    """An empty string is not a registration either."""
    resp = _login(device_token='')

    assert resp.status_code == 200
    driver_user.refresh_from_db()
    assert driver_user.fcm_token == EXISTING_TOKEN


# ---------------------------------------------------------------------------
# The control: a real token must still register, and must replace an old one
# ---------------------------------------------------------------------------

def test_a_device_token_is_registered(driver_user):
    """Without this, the fix above would be indistinguishable from not storing
    tokens at all."""
    resp = _login(device_token=NEW_TOKEN)

    assert resp.status_code == 200
    driver_user.refresh_from_db()
    assert driver_user.fcm_token == NEW_TOKEN


def test_a_rotated_token_replaces_the_previous_one(driver_user):
    """FCM rotates tokens. Keeping the old one would push into a dead handle."""
    _login(device_token=NEW_TOKEN)
    _login(device_token='third-token-xyz')

    driver_user.refresh_from_db()
    assert driver_user.fcm_token == 'third-token-xyz'


def test_a_first_time_login_registers_a_token(test_phone):
    """A user created by this very request must still get their token stored."""
    resp = _login(device_token=NEW_TOKEN)

    assert resp.status_code == 200, resp.content[:300]
    user = User.objects.get(phone_number=DRIVER_PHONE)
    assert user.fcm_token == NEW_TOKEN


# ---------------------------------------------------------------------------
# Registration must never be what fails a login
# ---------------------------------------------------------------------------

def test_a_failing_registration_still_lets_the_driver_in(driver_user):
    """Push is redundant to the WebSocket. Losing it must not lock a driver out.

    A driver who cannot sign in cannot earn. A driver signed in without push
    still gets every offer while the app is foreground, which is the overwhelming
    majority of a shift.
    """
    from unittest import mock

    with mock.patch.object(
        type(driver_user), 'save', side_effect=RuntimeError('db write failed'),
    ):
        resp = _login(device_token=NEW_TOKEN)

    assert resp.status_code == 200, (
        f'a failed push registration blocked the login (HTTP '
        f'{resp.status_code}); the driver cannot work at all'
    )
    assert resp.json()['data']['token'], 'no access token was issued'


def test_the_registration_write_is_narrow(driver_user):
    """A full-row save from a possibly-stale instance is how accepted trips got
    reverted elsewhere. Assert the write names its column."""
    from unittest import mock

    with mock.patch.object(type(driver_user), 'save', autospec=True) as saver:
        _login(device_token=NEW_TOKEN)

    assert saver.called, 'no save happened, so nothing was registered'
    _, kwargs = saver.call_args
    assert kwargs.get('update_fields') == ['fcm_token'], (
        f'the push registration saves with update_fields='
        f'{kwargs.get("update_fields")!r}; a bare save writes every column '
        f'from the in-memory instance'
    )


# ---------------------------------------------------------------------------
# A push token is a credential
# ---------------------------------------------------------------------------

def test_the_token_is_not_echoed_back_in_the_login_response(driver_user):
    """Anyone holding an FCM token can push to that handset.

    It rode along in every payload the user serializer renders, which includes
    admin views of a driver's profile. No client reads it back.
    """
    resp = _login(device_token=NEW_TOKEN)

    body = resp.json()
    assert NEW_TOKEN not in resp.content.decode(), (
        'the login response contains the FCM token; a push credential is being '
        'sent further than it needs to go'
    )
    assert 'fcm_token' not in body['data']['user']


def test_the_token_never_reaches_the_logs(driver_user, caplog):
    """Log storage is a different trust boundary from the database."""
    with caplog.at_level(logging.DEBUG):
        _login(device_token=NEW_TOKEN)

    assert NEW_TOKEN not in caplog.text, (
        'the FCM token was written to the log; log storage now holds a '
        'credential that can push to a real device'
    )


def test_a_failed_registration_does_not_log_the_token(driver_user, caplog):
    """The error path is the one most likely to log the whole object."""
    from unittest import mock

    with caplog.at_level(logging.DEBUG), mock.patch.object(
        type(driver_user), 'save', side_effect=RuntimeError('db write failed'),
    ):
        _login(device_token=NEW_TOKEN)

    assert NEW_TOKEN not in caplog.text, (
        'the failure path logged the FCM token'
    )
