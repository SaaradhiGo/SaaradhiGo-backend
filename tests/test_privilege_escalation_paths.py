"""§1/§29 — every path by which a client might promote itself, tried again.

`test_operator_privilege_boundary.py` closed the path that was actually exploited:
ask for `role: "admin"` at the OTP step, and `IsAdmin` grants access on role alone.
This file asks the next question, which is the one worth asking after any auth fix:
**is that the only way in, or just the way that was found first?**

So it attacks the same goal from every direction the architecture offers:

  * the OTP step, with every privileged-looking role string;
  * the LOGIN step, in case role is honoured there too (it is read from the OTP
    cache, so a role in the login body should be inert -- proven, not assumed);
  * a JWT with a forged `role` claim, in case any check trusts the token instead of
    the database row;
  * an expired token and a structurally malformed one, in case failure is open;
  * the separate `/auth/admin/login/` password endpoint, which had its own weaker
    gate;
  * an ordinary rider token and an ordinary driver token, directly against the
    privileged APIs.

And the other half, which matters as much: **a legitimate operator must be
allowed.** A suite that only proves refusals is satisfied by an outage.

WHAT THE FOURTH GATE WAS
------------------------
`/api/v1/auth/admin/login/` refused only when
`role != 'admin' AND not is_staff AND not is_superuser` -- an "any of three" gate.
`is_staff` alone was sufficient, so a rider carrying `is_staff` could mint an
operator JWT from an endpoint that was also completely unthrottled. Four separate
definitions of "operator" existed; an attacker needs only the most permissive one.
"""

import time

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from servers.driver.models import Driver
from servers.rider.models import Rider

User = get_user_model()

pytestmark = pytest.mark.django_db

OTP_URL = '/api/v1/auth/otp/'
LOGIN_URL = '/api/v1/auth/login/'
ADMIN_LOGIN_URL = '/api/v1/auth/admin/login/'

PROBE_PHONE = '+919521000001'
PROBE_OTP = '313131'
OPERATOR_PASSWORD = 'operator-console-not-a-real-secret'

PRIVILEGED = [
    ('driver KYC queue', 'get', '/api/v1/driver/admin/'),
    ('payout queue', 'get', '/api/v1/driver/admin/withdrawals/'),
    ('all trips', 'get', '/api/v1/ride/admin/trips/'),
    ('live driver locations', 'get', '/api/v1/ride/admin/live-locations/'),
    ('operations dashboard', 'get', '/api/v1/ride/admin/dashboard/'),
    ('all users', 'get', '/api/v1/auth/admin/users/'),
]

REFUSED = (401, 403)


@pytest.fixture(autouse=True)
def clean(settings):
    settings.TEST_PHONE_NUMBERS = {PROBE_PHONE: PROBE_OTP}
    settings.OPS_LOGIN_MAX_ATTEMPTS = 8
    settings.OPS_LOGIN_MAX_ATTEMPTS_PER_IP = 100
    cache.clear()
    yield
    cache.clear()


def _bearer(token):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    return c


def _for(user):
    return _bearer(AccessToken.for_user(user))


@pytest.fixture
def rider():
    u = User.objects.create_user(phone_number='+919522000001', role='rider',
                                 username='+919522000001')
    Rider.objects.create(user_id=u)
    return u


@pytest.fixture
def driver():
    u = User.objects.create_user(phone_number='+919622000001', role='driver',
                                 username='+919622000001')
    Driver.objects.create(user_id=u)
    return u


@pytest.fixture
def operator():
    """What `bootstrap_qa_operator` produces: role admin AND is_staff."""
    return User.objects.create_user(
        phone_number='+919722000001', role='admin', username='+919722000001',
        is_staff=True, password=OPERATOR_PASSWORD,
    )


# ===========================================================================
# Path 1 — the OTP step
# ===========================================================================

@pytest.mark.parametrize('role', [
    'admin', 'staff', 'superuser', 'operator', 'support', 'ops',
    'ADMIN', 'Admin', 'aDmIn', ' admin', 'admin ', 'admin\n',
    'rider,admin', 'admin;rider', ['admin'], {'role': 'admin'}, 1, True,
])
def test_no_privileged_role_can_be_requested_at_the_otp_step(role):
    """Strings, casings, whitespace, injections and wrong types.

    The check is an allow-list of two values, and this is what demonstrates that
    rather than a deny-list of the obvious spelling.
    """
    resp = APIClient().post(
        OTP_URL, {'phone_number': PROBE_PHONE, 'role': role}, format='json')

    assert resp.status_code == 400, (
        f'role={role!r} was accepted at the OTP step (HTTP {resp.status_code})'
    )
    assert not User.objects.filter(phone_number=PROBE_PHONE).exists()


# ===========================================================================
# Path 2 — the login step
# ===========================================================================

def test_a_role_in_the_login_body_is_inert(settings):
    """`role` is read from the OTP cache, not the login body. Proven, not assumed.

    If it were read from the body, the OTP-step fix would be trivially bypassed by
    requesting a rider OTP and then declaring admin at login.
    """
    client = APIClient()
    client.post(OTP_URL, {'phone_number': PROBE_PHONE, 'role': 'rider'},
                format='json')

    resp = client.post(LOGIN_URL, {
        'phone_number': PROBE_PHONE, 'otp': PROBE_OTP,
        'role': 'admin', 'is_staff': True, 'is_superuser': True,
    }, format='json')

    assert resp.status_code == 200, resp.content[:200]
    user = User.objects.get(phone_number=PROBE_PHONE)
    assert user.role == 'rider', f'login body set role to {user.role!r}'
    assert not user.is_staff, 'login body set is_staff'
    assert not user.is_superuser, 'login body set is_superuser'


def test_the_account_minted_by_login_reaches_nothing_privileged(settings):
    """The full chain, ending at the endpoints the escalation actually reached."""
    client = APIClient()
    client.post(OTP_URL, {'phone_number': PROBE_PHONE, 'role': 'rider'},
                format='json')
    login = client.post(LOGIN_URL, {'phone_number': PROBE_PHONE,
                                    'otp': PROBE_OTP}, format='json')
    assert login.status_code == 200
    api = _bearer(login.json()['data']['token'])

    for label, verb, url in PRIVILEGED:
        resp = getattr(api, verb)(url)
        assert resp.status_code in REFUSED, (
            f'a self-registered account reached {label}: HTTP {resp.status_code}'
        )


# ===========================================================================
# Path 3 — forging the token
# ===========================================================================

def test_a_forged_role_claim_in_the_jwt_is_ignored(rider):
    """Authorization must read the database row, not the token.

    The token is HS256-signed so its claims cannot be altered without the key --
    but a check that trusted a `role` claim would be wrong even so, because the
    claim is minted at login and the account can be demoted afterwards. This adds
    the claim with a VALID signature and asserts nothing honours it.
    """
    token = AccessToken.for_user(rider)
    token['role'] = 'admin'
    token['is_staff'] = True
    token['is_superuser'] = True
    api = _bearer(str(token))

    for label, verb, url in PRIVILEGED:
        resp = getattr(api, verb)(url)
        assert resp.status_code in REFUSED, (
            f'a forged role claim reached {label}: HTTP {resp.status_code}'
        )


def test_an_expired_token_is_refused(rider, settings):
    """Failure must be closed. An expired token that still works is no expiry."""
    token = AccessToken.for_user(rider)
    token.set_exp(from_time=token.current_time - token.lifetime * 2)
    api = _bearer(str(token))

    resp = api.get(PRIVILEGED[0][2])
    assert resp.status_code in REFUSED


@pytest.mark.parametrize('bad', [
    '', 'not-a-token', 'a.b.c', 'Bearer', '.....',
    'eyJhbGciOiJub25lIn0.eyJyb2xlIjoiYWRtaW4ifQ.',   # alg=none, role=admin
])
def test_a_malformed_token_is_refused_not_accepted(bad):
    """Including an `alg: none` token claiming admin, which is the classic attempt."""
    api = _bearer(bad)

    resp = api.get(PRIVILEGED[0][2])
    assert resp.status_code in REFUSED, (
        f'token {bad!r} was accepted with HTTP {resp.status_code}'
    )


# ===========================================================================
# Path 4 — the separate password endpoint, which had the weakest gate
# ===========================================================================

def test_a_rider_carrying_is_staff_cannot_mint_an_operator_token(rider):
    """The fourth gate. `is_staff` ALONE used to be sufficient here.

    This is the shape that made four divergent definitions dangerous: the account
    is refused by `IsAdmin` everywhere, and this one endpoint would still hand it a
    token.
    """
    User.objects.filter(pk=rider.pk).update(is_staff=True)
    rider.set_password(OPERATOR_PASSWORD)
    rider.save(update_fields=['password'])

    resp = APIClient().post(ADMIN_LOGIN_URL, {
        'phone_number': rider.phone_number, 'password': OPERATOR_PASSWORD,
    }, format='json')

    assert resp.status_code in REFUSED, (
        f'a rider with is_staff minted an operator token: HTTP '
        f'{resp.status_code} {resp.content[:200]}'
    )


def test_role_admin_without_staff_cannot_mint_an_operator_token():
    """The other half of the same gate."""
    u = User.objects.create_user(
        phone_number='+919523000001', role='admin', username='+919523000001',
        is_staff=False, password=OPERATOR_PASSWORD)

    resp = APIClient().post(ADMIN_LOGIN_URL, {
        'phone_number': u.phone_number, 'password': OPERATOR_PASSWORD,
    }, format='json')

    assert resp.status_code in REFUSED, (
        f'role=admin without is_staff minted a token: HTTP {resp.status_code}'
    )


def test_the_password_endpoint_does_not_enumerate(operator):
    """Three failures that used to be distinguishable must now be identical.

    unknown phone      -> issue 'User not found'
    wrong password     -> issue 'Incorrect password'
    non-operator       -> 403 AUTH_NOT_ADMIN

    The first two are an oracle for which numbers hold accounts; the third told an
    attacker which of those are operators.
    """
    rider = User.objects.create_user(
        phone_number='+919524000001', role='rider', username='+919524000001',
        password=OPERATOR_PASSWORD)
    Rider.objects.create(user_id=rider)

    unknown = APIClient().post(ADMIN_LOGIN_URL, {
        'phone_number': '+919529999999', 'password': 'whatever'}, format='json')
    wrong_pw = APIClient().post(ADMIN_LOGIN_URL, {
        'phone_number': operator.phone_number, 'password': 'wrong'}, format='json')
    non_operator = APIClient().post(ADMIN_LOGIN_URL, {
        'phone_number': rider.phone_number, 'password': OPERATOR_PASSWORD,
    }, format='json')

    shapes = {
        'unknown phone': (unknown.status_code, unknown.json()['error']['code'],
                          unknown.json()['error']['message']),
        'wrong password': (wrong_pw.status_code, wrong_pw.json()['error']['code'],
                           wrong_pw.json()['error']['message']),
        'non-operator': (non_operator.status_code,
                         non_operator.json()['error']['code'],
                         non_operator.json()['error']['message']),
    }
    distinct = set(shapes.values())
    assert len(distinct) == 1, (
        f'the password endpoint distinguishes its failure modes, which enumerates '
        f'accounts and identifies operators: {shapes}'
    )


def test_the_password_endpoint_is_throttled(operator, settings):
    """It had no throttle at all.

    The console form was measured at 12 failed logins in 7.8 s before its guard
    existed; this endpoint had the same exposure and nothing in front of it. Both
    operator sign-in surfaces must have the same resistance, or the weaker one is
    the real policy.
    """
    settings.OPS_LOGIN_MAX_ATTEMPTS = 4
    for i in range(settings.OPS_LOGIN_MAX_ATTEMPTS):
        APIClient().post(ADMIN_LOGIN_URL, {
            'phone_number': operator.phone_number, 'password': f'wrong-{i}',
        }, format='json')

    # Past the threshold the CORRECT password must also be refused.
    resp = APIClient().post(ADMIN_LOGIN_URL, {
        'phone_number': operator.phone_number, 'password': OPERATOR_PASSWORD,
    }, format='json')

    assert resp.status_code in REFUSED, (
        'the password endpoint accepted a login after repeated failures, so it is '
        'still an unmetered password oracle'
    )


# ===========================================================================
# Paths 5 and 6 — ordinary tokens, straight at the privileged API
# ===========================================================================

@pytest.mark.parametrize('label,verb,url', PRIVILEGED)
def test_a_rider_token_is_refused(rider, label, verb, url):
    resp = getattr(_for(rider), verb)(url)
    assert resp.status_code in REFUSED, f'{label}: HTTP {resp.status_code}'


@pytest.mark.parametrize('label,verb,url', PRIVILEGED)
def test_a_driver_token_is_refused(driver, label, verb, url):
    resp = getattr(_for(driver), verb)(url)
    assert resp.status_code in REFUSED, f'{label}: HTTP {resp.status_code}'


@pytest.mark.parametrize('label,verb,url', PRIVILEGED)
def test_an_anonymous_caller_is_refused(label, verb, url):
    resp = getattr(APIClient(), verb)(url)
    assert resp.status_code in REFUSED, f'{label}: HTTP {resp.status_code}'


# ===========================================================================
# The other half — a legitimate operator must be allowed
# ===========================================================================

@pytest.mark.parametrize('label,verb,url', PRIVILEGED)
def test_a_legitimate_operator_is_allowed(operator, label, verb, url):
    """Without this, an `is_operator` that returned False unconditionally would
    pass every other test in this file, and the result would be an outage."""
    resp = getattr(_for(operator), verb)(url)

    assert resp.status_code == 200, (
        f'a legitimate operator was refused {label}: HTTP {resp.status_code} '
        f'{resp.content[:200]}'
    )


def test_a_legitimate_operator_can_sign_in_with_a_password(operator):
    """The authorised half of the password endpoint."""
    resp = APIClient().post(ADMIN_LOGIN_URL, {
        'phone_number': operator.phone_number, 'password': OPERATOR_PASSWORD,
    }, format='json')

    assert resp.status_code == 200, resp.content[:300]
    assert resp.json()['data']['token']


def test_the_token_that_endpoint_issues_actually_works(operator):
    """An issued token that is refused by the API would be a different bug."""
    login = APIClient().post(ADMIN_LOGIN_URL, {
        'phone_number': operator.phone_number, 'password': OPERATOR_PASSWORD,
    }, format='json')
    api = _bearer(login.json()['data']['token'])

    resp = api.get(PRIVILEGED[0][2])
    assert resp.status_code == 200, resp.content[:200]


def test_a_successful_operator_login_clears_the_failure_counter(operator, settings):
    """A busy operator who mistypes twice a day must not lock themselves out."""
    settings.OPS_LOGIN_MAX_ATTEMPTS = 4
    for i in range(settings.OPS_LOGIN_MAX_ATTEMPTS - 1):
        APIClient().post(ADMIN_LOGIN_URL, {
            'phone_number': operator.phone_number, 'password': f'wrong-{i}',
        }, format='json')

    ok = APIClient().post(ADMIN_LOGIN_URL, {
        'phone_number': operator.phone_number, 'password': OPERATOR_PASSWORD,
    }, format='json')
    assert ok.status_code == 200, ok.content[:200]

    for i in range(settings.OPS_LOGIN_MAX_ATTEMPTS - 1):
        APIClient().post(ADMIN_LOGIN_URL, {
            'phone_number': operator.phone_number, 'password': f'again-{i}',
        }, format='json')
    again = APIClient().post(ADMIN_LOGIN_URL, {
        'phone_number': operator.phone_number, 'password': OPERATOR_PASSWORD,
    }, format='json')

    assert again.status_code == 200, (
        'the earlier success did not clear the counter'
    )


# ===========================================================================
# One definition, everywhere
# ===========================================================================

def test_all_four_authorization_sites_now_agree():
    """There were four answers to "is this an operator", and an attacker needs the
    most permissive one. This asserts there is now exactly one."""
    from base.permissions import IsAdmin, is_operator
    from servers.admin_dashboard.views import is_operator as console_is_operator

    assert console_is_operator is is_operator, (
        'the console has its own copy again; they will diverge'
    )

    class _Req:
        def __init__(self, user):
            self.user = user

    class _User:
        is_authenticated = True

        def __init__(self, role, is_staff, is_superuser):
            self.role, self.is_staff, self.is_superuser = (
                role, is_staff, is_superuser)

    cases = [
        ('operator',      ('admin', True, False),  True),
        ('role only',     ('admin', False, False), False),
        ('staff only',    ('rider', True, False),  False),
        ('superuser',     ('rider', False, True),  True),
        ('plain rider',   ('rider', False, False), False),
        ('staff + super', ('rider', True, True),   True),
    ]
    for name, attrs, expected in cases:
        u = _User(*attrs)
        assert is_operator(u) is expected, f'is_operator: {name}'
        assert IsAdmin().has_permission(_Req(u), None) is expected, f'IsAdmin: {name}'
