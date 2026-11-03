"""A rider never sees the driver's phone number as their name.

`str(driver)` falls back to the phone number when the driver has no name on
file, and the rider-facing payloads used it: the rider app's driver card read
"+91..." on QA (product phase 1 emulator run, trip 118). Contacts stay masked.
"""
import pytest
from django.contrib.auth import get_user_model

from servers.driver.models import Driver

pytestmark = pytest.mark.django_db
User = get_user_model()


def _driver(name):
    u = User.objects.create_user(phone_number='+919811100001', role='driver', full_name=name)
    return Driver.objects.create(user_id=u, approved=True)


def test_a_named_driver_is_shown_by_name():
    assert _driver('Ravi Kumar').public_name == 'Ravi Kumar'


@pytest.mark.parametrize('name', ['', '   ', None])
def test_no_name_never_falls_back_to_the_phone(name):
    d = _driver(name)
    assert d.public_name == 'Your driver'
    assert '981' not in d.public_name
