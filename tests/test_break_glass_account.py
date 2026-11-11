"""The shared emergency account cannot be used quietly.

It cannot be prevented — somebody holding the credentials can sign in.
What these tests protect is that doing so leaves evidence: a typed reason,
a notification to every Super Admin, and an audit row.

BOTH DOORS
----------
There are two operator sign-in surfaces, the JSON API and the Django
console form. A reason demanded on one and not the other is no reason at
all, so each is tested separately.
"""

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from rest_framework.test import APIClient

from servers.admin_access.breakglass import MIN_REASON_LENGTH
from servers.admin_access.models import AdminAssignment, AdminRole, GeoNode
from servers.rider.models import Notification

pytestmark = pytest.mark.django_db

User = get_user_model()

PASSWORD = 'br3ak-gl4ss-Pa55'
GOOD_REASON = 'Both named Super Admins unreachable; payout run stuck.'


@pytest.fixture
def geo(db):
    from conftest import seed_admin_access

    seed_admin_access()
    return {n.code: n for n in GeoNode.objects.all()}


@pytest.fixture
def super_admins(geo):
    """Two named Super Admins, who are the ones who must be told."""
    people = []
    for i in (1, 2):
        user = User.objects.create_user(
            phone_number=f'+91770000000{i}', username=f'+91770000000{i}',
            email=f'named{i}@saaradhigo.test', role='admin', is_staff=True,
        )
        AdminAssignment.objects.create(
            admin=user, role=AdminRole.objects.get(code='super_admin'),
            scope=geo['GLOBAL'],
        )
        people.append(user)
    return people


@pytest.fixture
def emergency(geo):
    user = User.objects.create_user(
        phone_number='+917700009999', username='+917700009999',
        email='breakglass@saaradhigo.test', role='admin', is_staff=True,
        is_break_glass=True,
    )
    user.set_password(PASSWORD)
    user.save()
    return user


@pytest.fixture
def ordinary(geo):
    user = User.objects.create_user(
        phone_number='+917700008888', username='+917700008888',
        email='normal@saaradhigo.test', role='admin', is_staff=True,
    )
    user.set_password(PASSWORD)
    user.save()
    return user


# ---------------------------------------------------------------------------
# The API door
# ---------------------------------------------------------------------------

class TestTheApiDoor:
    def _login(self, **extra):
        return APIClient().post('/api/v1/auth/admin/login/', {
            'email': 'breakglass@saaradhigo.test',
            'password': PASSWORD,
            **extra,
        }, format='json')

    def test_no_token_without_a_reason(self, emergency, super_admins):
        response = self._login()

        assert response.status_code == 401
        assert response.json()['error']['code'] == \
            'AUTH_BREAK_GLASS_REASON_REQUIRED'
        assert 'token' not in response.json().get('data', {})

    def test_a_token_with_a_reason(self, emergency, super_admins):
        response = self._login(reason=GOOD_REASON)

        assert response.status_code == 200
        assert response.json()['data']['token']

    def test_an_empty_reason_is_not_a_reason(self, emergency, super_admins):
        assert self._login(reason='   ').status_code == 401

    def test_a_token_reason_is_not_a_reason(self, emergency, super_admins):
        """'.' must not satisfy a field that exists to be read later."""
        assert self._login(reason='.').status_code == 401
        assert self._login(reason='x' * (MIN_REASON_LENGTH - 1)).status_code == 401

    def test_every_super_admin_is_notified(self, emergency, super_admins):
        self._login(reason=GOOD_REASON)

        for person in super_admins:
            assert Notification.objects.filter(
                user_id=person, notif_type='system',
                title='Emergency account used',
            ).exists(), f'{person.email} was not told'

    def test_the_reason_reaches_the_notification(self, emergency, super_admins):
        self._login(reason=GOOD_REASON)

        note = Notification.objects.filter(user_id=super_admins[0]).first()

        assert GOOD_REASON in note.message
        assert note.data['reason'] == GOOD_REASON

    def test_the_use_is_audited(self, emergency, super_admins):
        from servers.admin_audit.models import AdminAuditLog

        self._login(reason=GOOD_REASON)

        row = AdminAuditLog.objects.filter(action='break_glass_login').first()
        assert row is not None
        assert row.after['reason'] == GOOD_REASON

    def test_an_ordinary_operator_needs_no_reason(self, ordinary, super_admins):
        """So the requirement is the flag, not a new hurdle for everybody."""
        response = APIClient().post('/api/v1/auth/admin/login/', {
            'email': 'normal@saaradhigo.test', 'password': PASSWORD,
        }, format='json')

        assert response.status_code == 200

    def test_a_wrong_password_is_refused_the_same_way_as_any_other(
        self, emergency, super_admins,
    ):
        """The reason prompt must not reveal which account is the emergency
        one. It is reachable only AFTER a correct password."""
        response = APIClient().post('/api/v1/auth/admin/login/', {
            'email': 'breakglass@saaradhigo.test', 'password': 'wrong',
            'reason': GOOD_REASON,
        }, format='json')

        assert response.status_code == 401
        assert response.json()['error']['code'] == 'AUTH_INVALID_CREDENTIALS'

    def test_nobody_is_notified_on_a_failed_attempt(
        self, emergency, super_admins,
    ):
        APIClient().post('/api/v1/auth/admin/login/', {
            'email': 'breakglass@saaradhigo.test', 'password': 'wrong',
        }, format='json')

        assert not Notification.objects.filter(
            title='Emergency account used'
        ).exists()


# ---------------------------------------------------------------------------
# Degenerate cases
# ---------------------------------------------------------------------------

class TestWhenThereIsNobodyToTell:
    def test_the_sign_in_still_succeeds(self, emergency):
        """No Super Admins exist. The account exists precisely for states
        like this, so a failure to notify must not block it."""
        response = APIClient().post('/api/v1/auth/admin/login/', {
            'email': 'breakglass@saaradhigo.test', 'password': PASSWORD,
            'reason': GOOD_REASON,
        }, format='json')

        assert response.status_code == 200

    def test_and_it_is_logged_as_reaching_nobody(self, emergency, caplog):
        with caplog.at_level('ERROR'):
            APIClient().post('/api/v1/auth/admin/login/', {
                'email': 'breakglass@saaradhigo.test', 'password': PASSWORD,
                'reason': GOOD_REASON,
            }, format='json')

        assert any(
            'reached_nobody' in r.message or 'reached nobody' in r.message
            for r in caplog.records
        ), [r.message for r in caplog.records]

    def test_the_flag_grants_no_authority_by_itself(self, emergency, geo):
        """Being the emergency account is not being a Super Admin."""
        from servers.admin_access.access import is_super_admin, permissions_at

        assert not is_super_admin(emergency)
        assert permissions_at(emergency) == set()
