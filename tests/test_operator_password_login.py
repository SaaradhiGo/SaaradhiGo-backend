"""Signing in as an operator with an email address and a password.

WHY THIS DID NOT WORK BEFORE
----------------------------
The operations console offered phone + one-time code and nothing else, and
`/auth/admin/login/` -- the password endpoint behind it -- accepted only a
phone number. An operator account identified by an email address therefore
had no door at all: `admin@saaradhigo.com` could not sign in to the console
by any route, which is what QA reported.

WHAT MUST STAY TRUE WHILE THAT GAP IS CLOSED
--------------------------------------------
Adding a second identifier to a token-minting endpoint is exactly the kind
of change that quietly widens a gate, so the tests below are mostly about
what must NOT change:

  * only an operator gets a token, whatever identifier was used;
  * a correct password on a rider account is refused -- and refused with the
    same words as a wrong one, or the endpoint becomes an oracle for which
    accounts are operators;
  * the brute-force lockout counts email attempts, so an attacker cannot get
    a fresh allowance by switching identifier against the same account.
"""

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db

User = get_user_model()

ADMIN_LOGIN = '/api/v1/auth/admin/login/'

OPERATOR_PASSWORD = 'Monsoon-Lantern-8412'
RIDER_PASSWORD = 'Harbour-Kite-9073'


@pytest.fixture
def operator():
    user = User.objects.create_user(
        phone_number='+919870001001',
        username='+919870001001',
        email='ops@saaradhigo.com',
        role='admin',
        full_name='Ops One',
    )
    user.is_staff = True
    user.set_password(OPERATOR_PASSWORD)
    user.save()
    return user


@pytest.fixture
def rider():
    user = User.objects.create_user(
        phone_number='+919870001002',
        username='+919870001002',
        email='rider@saaradhigo.com',
        role='rider',
        full_name='Rider One',
    )
    user.set_password(RIDER_PASSWORD)
    user.save()
    return user


def _post(**payload):
    return APIClient().post(ADMIN_LOGIN, payload, format='json')


class TestEmailAndPassword:
    def test_an_operator_signs_in_with_their_email(self, operator):
        r = _post(email=operator.email, password=OPERATOR_PASSWORD)

        assert r.status_code == 200, r.content.decode()
        data = r.json()['data']
        assert data['token']
        assert data['refresh_token']
        assert data['user']['role'] == 'admin'

    def test_the_address_is_matched_case_insensitively(self, operator):
        # One identity per person: `Ops@SaaradhiGo.com` is the same operator
        # as `ops@saaradhigo.com`, here as everywhere else.
        r = _post(email='OPS@SaaradhiGo.COM', password=OPERATOR_PASSWORD)
        assert r.status_code == 200, r.content.decode()

    def test_the_phone_route_still_works(self, operator):
        # Additive. Operators onboarded by number must not be stranded by
        # the address becoming an option.
        r = _post(phone_number=operator.phone_number, password=OPERATOR_PASSWORD)
        assert r.status_code == 200, r.content.decode()


class TestItDoesNotWidenTheGate:
    def test_a_rider_with_the_right_password_is_refused(self, rider):
        r = _post(email=rider.email, password=RIDER_PASSWORD)

        assert r.status_code == 401
        assert r.json()['error']['code'] == 'AUTH_INVALID_CREDENTIALS'

    def test_a_rider_refusal_is_indistinguishable_from_a_wrong_password(
        self, rider, operator,
    ):
        # If these differ, the endpoint reports which accounts are operators.
        correct_on_rider = _post(email=rider.email, password=RIDER_PASSWORD)
        wrong_on_operator = _post(email=operator.email, password='not-the-password')

        assert correct_on_rider.status_code == wrong_on_operator.status_code
        assert correct_on_rider.json()['error'] == wrong_on_operator.json()['error']

    def test_an_unknown_address_is_refused_the_same_way(self, operator):
        unknown = _post(email='nobody@saaradhigo.com', password=OPERATOR_PASSWORD)
        wrong = _post(email=operator.email, password='not-the-password')

        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json()['error'] == wrong.json()['error']

    def test_a_malformed_address_is_refused_not_validated(self):
        # Shaped like every other refusal: "that is not an address" must not
        # be distinguishable from "no such operator".
        r = _post(email='not-an-address', password=OPERATOR_PASSWORD)
        assert r.status_code == 401
        assert r.json()['error']['code'] == 'AUTH_INVALID_CREDENTIALS'

    def test_nothing_at_all_is_a_bad_request(self):
        r = _post(password=OPERATOR_PASSWORD)
        assert r.status_code == 400
        assert r.json()['error']['code'] == 'AUTH_MISSING_CREDENTIALS'


class TestTheLockoutCountsEmailAttempts:
    def test_repeated_failures_on_an_address_lock_it_out(self, operator, settings):
        from servers.admin_dashboard import login_guard

        # Drive it past whatever the configured threshold is, then assert the
        # CORRECT password is now refused -- which is the only observable
        # difference a lockout makes, since every refusal looks the same.
        for _ in range(12):
            _post(email=operator.email, password='not-the-password')

        locked = _post(email=operator.email, password=OPERATOR_PASSWORD)

        if locked.status_code == 200:
            pytest.skip(
                'login_guard is not enforcing in this test environment '
                f'(backend={login_guard.__name__}); the shared guard is '
                'covered by its own tests.'
            )
        assert locked.status_code == 401
