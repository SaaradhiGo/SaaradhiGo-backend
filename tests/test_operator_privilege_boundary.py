"""B3/B4 — nobody may promote themselves to operator, and the API is the boundary.

THE DEFECT THIS FILE EXISTS FOR
-------------------------------
`role` was something a client asked for. `POST /api/v1/auth/otp/` accepted
`"role": "admin"` because `'admin'` was in `VALID_ROLES`, cached it, and `/login/`
created the account with it. `base.permissions.IsAdmin` then granted access on
`role == 'admin'` alone.

Measured before the fix, on a fresh phone number, with one OTP:

    created user: role='admin' is_staff=False is_superuser=False
      /api/v1/driver/admin/                    -> HTTP 200   KYC queue
      /api/v1/driver/admin/withdrawals/        -> HTTP 200   payout queue
      /api/v1/ride/admin/trips/                -> HTTP 200   every trip
      /api/v1/ride/admin/live-locations/       -> HTTP 200   live driver positions
      /api/v1/ride/admin/dashboard/            -> HTTP 200   operations dashboard

Anyone who could receive an SMS could approve driver KYC and read the payout
queue.

TWO DEFECTS, EITHER ONE SUFFICIENT
----------------------------------
1. `'admin'` was a role a client could request.
2. `IsAdmin` did not check `is_staff`, although its own docstring said it did --
   so the second gate existed in documentation and nowhere else.

Both are closed, and both are tested here separately. Fixing only one would leave
the system one edit away from the same hole.

WHY THESE TESTS CALL THE API DIRECTLY
-------------------------------------
Because that is where authorization lives or does not. The ops console not showing
a payout button to a non-operator is not authorization -- it is layout. Every test
below holds a real token and calls the endpoint itself.
"""

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from servers.driver.models import Driver
from servers.rider.models import Rider

User = get_user_model()

pytestmark = pytest.mark.django_db

OTP_URL = '/api/v1/auth/otp/'
LOGIN_URL = '/api/v1/auth/login/'

ATTACKER_PHONE = '+919511000001'
ATTACKER_OTP = '445566'

# Every operator-only API surface. Read endpoints are listed too: the KYC queue,
# the payout queue and live driver locations are sensitive to READ, not only to
# write.
OPERATOR_READ_ENDPOINTS = [
    ('driver KYC queue', '/api/v1/driver/admin/'),
    ('payout queue', '/api/v1/driver/admin/withdrawals/'),
    ('all trips', '/api/v1/ride/admin/trips/'),
    ('live driver locations', '/api/v1/ride/admin/live-locations/'),
    ('operations dashboard', '/api/v1/ride/admin/dashboard/'),
]


@pytest.fixture(autouse=True)
def bypass(settings):
    settings.TEST_PHONE_NUMBERS = {ATTACKER_PHONE: ATTACKER_OTP}


def _token_client(user):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    return c


@pytest.fixture
def rider():
    u = User.objects.create_user(phone_number='+919512000001', role='rider',
                                 username='+919512000001')
    Rider.objects.create(user_id=u)
    return u


@pytest.fixture
def driver():
    """The user. `driver_row` is the Driver record, when a test needs its id."""
    u = User.objects.create_user(phone_number='+919612000001', role='driver',
                                 username='+919612000001')
    Driver.objects.create(user_id=u)
    return u


@pytest.fixture
def driver_row(driver):
    return Driver.objects.get(user_id=driver)


@pytest.fixture
def real_operator():
    """What `bootstrap_qa_operator` creates: role admin AND is_staff."""
    return User.objects.create_user(
        phone_number='+919712000001', role='admin', username='+919712000001',
        is_staff=True, password='operator-not-a-real-secret',
    )


# ===========================================================================
# Defect 1 — a client may not ask to be an operator
# ===========================================================================

def test_the_otp_endpoint_refuses_a_request_to_be_an_admin():
    """The entry point of the escalation."""
    resp = APIClient().post(
        OTP_URL, {'phone_number': ATTACKER_PHONE, 'role': 'admin'},
        format='json')

    assert resp.status_code == 400, (
        f'requesting role=admin was accepted (HTTP {resp.status_code}); the '
        f'account minted by the next call would carry operator authority'
    )
    assert resp.json()['error']['code'] == 'AUTH_INVALID_ROLE'


def test_the_refusal_does_not_reveal_that_admin_is_a_real_role():
    """A distinct message for 'admin' would tell an attacker what to aim at."""
    admin_try = APIClient().post(
        OTP_URL, {'phone_number': ATTACKER_PHONE, 'role': 'admin'},
        format='json').json()['error']
    nonsense_try = APIClient().post(
        OTP_URL, {'phone_number': ATTACKER_PHONE, 'role': 'wizard'},
        format='json').json()['error']

    assert admin_try['code'] == nonsense_try['code']
    assert admin_try['message'] == nonsense_try['message'], (
        'asking for admin produces a different message from asking for '
        'nonsense, which distinguishes real privileged roles'
    )
    assert 'admin' not in admin_try['message'], (
        'the refusal lists admin as an option'
    )


@pytest.mark.parametrize('role', ['admin', 'superuser', 'staff', 'support',
                                  'ADMIN', 'Admin'])
def test_no_spelling_of_a_privileged_role_is_accepted(role):
    """Case and near-misses included, because the check is an allow-list and this
    is what proves it is one rather than a deny-list of the obvious string."""
    resp = APIClient().post(
        OTP_URL, {'phone_number': ATTACKER_PHONE, 'role': role}, format='json')

    assert resp.status_code == 400, f'role={role!r} was accepted'


@pytest.mark.parametrize('role', ['rider', 'driver'])
def test_the_ordinary_roles_still_work(role):
    """The control. A fix that blocked signup would be worse than the defect."""
    resp = APIClient().post(
        OTP_URL, {'phone_number': ATTACKER_PHONE, 'role': role}, format='json')

    assert resp.status_code == 200, resp.content[:200]


def test_the_full_escalation_chain_is_closed():
    """The whole attack, end to end, exactly as it was demonstrated."""
    client = APIClient()
    otp = client.post(OTP_URL, {'phone_number': ATTACKER_PHONE,
                                'role': 'admin'}, format='json')
    assert otp.status_code == 400

    # Even if the attacker proceeds anyway, the account must not be an operator.
    client.post(OTP_URL, {'phone_number': ATTACKER_PHONE, 'role': 'rider'},
                format='json')
    login = client.post(LOGIN_URL, {'phone_number': ATTACKER_PHONE,
                                    'otp': ATTACKER_OTP}, format='json')
    assert login.status_code == 200, login.content[:200]

    user = User.objects.get(phone_number=ATTACKER_PHONE)
    assert user.role != 'admin'
    assert not user.is_staff
    assert not user.is_superuser

    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION=f'Bearer {login.json()["data"]["token"]}')
    for label, url in OPERATOR_READ_ENDPOINTS:
        resp = api.get(url)
        assert resp.status_code in (401, 403), (
            f'a self-signed-up account reached {label} ({url}) with HTTP '
            f'{resp.status_code}'
        )


# ===========================================================================
# Defect 2 — role alone is not operator authority
# ===========================================================================

@pytest.mark.parametrize('label,url', OPERATOR_READ_ENDPOINTS)
def test_role_admin_without_staff_is_refused(label, url):
    """The second gate, which used to exist only in a docstring.

    An account can end up role='admin' without is_staff through any number of
    routes -- a fixture, a data import, a future code path, a direct database
    edit. None of them should confer payout and KYC authority.
    """
    impostor = User.objects.create_user(
        phone_number='+919513000001', role='admin', username='+919513000001',
        is_staff=False,
    )

    resp = _token_client(impostor).get(url)

    assert resp.status_code in (401, 403), (
        f'role=admin with is_staff=False reached {label} with HTTP '
        f'{resp.status_code}'
    )


@pytest.mark.parametrize('label,url', OPERATOR_READ_ENDPOINTS)
def test_a_real_operator_still_has_access(real_operator, label, url):
    """The control, and the more important half.

    A permission fix that locks operators out of the console is an outage, not a
    security improvement. Without this, tightening IsAdmin to something that
    refuses everyone would pass every test above.
    """
    resp = _token_client(real_operator).get(url)

    assert resp.status_code == 200, (
        f'a legitimate operator (role=admin, is_staff=True) was refused '
        f'{label}: HTTP {resp.status_code} {resp.content[:200]}'
    )


@pytest.mark.parametrize('label,url', OPERATOR_READ_ENDPOINTS)
def test_a_superuser_still_has_access(label, url):
    """Break-glass must keep working even without role='admin'."""
    su = User.objects.create_superuser(
        phone_number='+919714000001', username='+919714000001',
        password='break-glass-not-a-real-secret',
    )

    resp = _token_client(su).get(url)

    assert resp.status_code == 200, (
        f'a superuser was refused {label}: HTTP {resp.status_code}'
    )


# ===========================================================================
# Riders and drivers, calling the operator API directly
# ===========================================================================

@pytest.mark.parametrize('label,url', OPERATOR_READ_ENDPOINTS)
def test_a_rider_cannot_reach_the_operator_api(rider, label, url):
    resp = _token_client(rider).get(url)

    assert resp.status_code in (401, 403), (
        f'a rider token reached {label} with HTTP {resp.status_code}'
    )


@pytest.mark.parametrize('label,url', OPERATOR_READ_ENDPOINTS)
def test_a_driver_cannot_reach_the_operator_api(driver, label, url):
    """A driver reaching the KYC queue could approve themselves."""
    resp = _token_client(driver).get(url)

    assert resp.status_code in (401, 403), (
        f'a driver token reached {label} with HTTP {resp.status_code}'
    )


# ===========================================================================
# Write actions — the ones that move money or let someone drive
# ===========================================================================

def test_a_driver_cannot_approve_their_own_kyc(driver, driver_row):
    """The single most attractive target in the operator API."""
    url = f'/api/v1/driver/admin/{driver_row.id}/update-kyc/'

    resp = _token_client(driver).post(url, {'approved': True}, format='json')

    assert resp.status_code in (401, 403), (
        f'a driver reached their own KYC approval with HTTP {resp.status_code}'
    )
    driver_row.refresh_from_db()
    assert not driver_row.approved, 'the driver approved themselves'


def test_a_rider_cannot_approve_a_payout(rider, driver):
    """Payout release is the action with the shortest path to lost money."""
    url = '/api/v1/driver/admin/withdrawals/1/approve/'

    resp = _token_client(rider).post(url, {}, format='json')

    assert resp.status_code in (401, 403), (
        f'a rider reached payout approval with HTTP {resp.status_code}; a 404 '
        f'here would mean authorization is being decided after the lookup'
    )


def test_a_non_staff_admin_cannot_approve_a_payout(driver):
    """The escalated account, aimed at the money."""
    impostor = User.objects.create_user(
        phone_number='+919515000001', role='admin', username='+919515000001',
        is_staff=False,
    )

    resp = _token_client(impostor).post(
        '/api/v1/driver/admin/withdrawals/1/approve/', {}, format='json')

    assert resp.status_code in (401, 403), (
        f'role=admin with is_staff=False reached payout approval: HTTP '
        f'{resp.status_code}'
    )


def test_an_anonymous_caller_is_refused_every_operator_endpoint():
    """Already covered for the console; asserted here for the API too."""
    anon = APIClient()
    for label, url in OPERATOR_READ_ENDPOINTS:
        resp = anon.get(url)
        assert resp.status_code in (401, 403), (
            f'anonymous reached {label} with HTTP {resp.status_code}'
        )


# ===========================================================================
# One definition of "operator", not three
# ===========================================================================

def test_the_console_and_the_api_agree_on_who_is_an_operator():
    """They used to be three separate copies of the same expression.

    Three copies is how two of them end up disagreeing, and a console that admits
    someone the API refuses (or the reverse) is a boundary nobody can reason
    about.
    """
    from base.permissions import IsAdmin
    from servers.admin_dashboard.views import is_operator

    class _Req:
        def __init__(self, user):
            self.user = user

    cases = [
        ('operator', dict(role='admin', is_staff=True, is_superuser=False), True),
        ('role only', dict(role='admin', is_staff=False, is_superuser=False), False),
        ('staff only', dict(role='rider', is_staff=True, is_superuser=False), False),
        ('superuser', dict(role='rider', is_staff=False, is_superuser=True), True),
        ('rider', dict(role='rider', is_staff=False, is_superuser=False), False),
    ]

    class _User:
        """A stand-in, because `is_authenticated` is a read-only property on the
        real model and both checks only read attributes."""
        is_authenticated = True

        def __init__(self, role, is_staff, is_superuser):
            self.role = role
            self.is_staff = is_staff
            self.is_superuser = is_superuser

    for name, attrs, expected in cases:
        user = _User(**attrs)
        api_says = IsAdmin().has_permission(_Req(user), None)
        console_says = is_operator(user)
        assert api_says == console_says == expected, (
            f'{name}: API says {api_says}, console says {console_says}, '
            f'expected {expected}'
        )
