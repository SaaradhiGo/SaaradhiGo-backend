"""The operations console login must not be brute-forceable.

Measured against live QA before this existed: **12 consecutive failed logins in 7.8
seconds, with no throttling of any kind.** That console approves driver KYC and
releases payouts, and it sits at the backend root.

Tested at the HTTP boundary, through the real login form with a real CSRF token,
because that is the surface an attacker uses. A service-layer test of the guard
functions would have passed against the unguarded view -- which is precisely how the
gap survived this long.

The properties held here:

  * repeated failures eventually refuse, and the refusal text is indistinguishable
    from an ordinary wrong password, so a lockout cannot be used to discover which
    phone numbers are real;
  * a correct password still works before the threshold, and clears the counter;
  * a valid password on a NON-operator account still counts as a failure, or a rider
    account becomes an unthrottled oracle for password guessing;
  * the lockout is bounded in time, not permanent;
  * a cache failure allows the login rather than locking every operator out during an
    infrastructure incident -- deliberate, and asserted so the choice is explicit.
"""

import re
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache

from servers.rider.models import Rider

User = get_user_model()

pytestmark = pytest.mark.django_db

LOGIN_URL = '/login/'
OPERATOR_PHONE = '+919577000001'
OPERATOR_PASSWORD = 'ops-console-not-a-real-secret'
RIDER_PHONE = '+919577000002'
RIDER_PASSWORD = 'rider-account-not-a-real-secret'


@pytest.fixture(autouse=True)
def _clear_guard_state(settings):
    settings.OPS_LOGIN_MAX_ATTEMPTS = 5
    settings.OPS_LOGIN_MAX_ATTEMPTS_PER_IP = 50      # isolate the per-phone tests
    settings.OPS_LOGIN_LOCKOUT_SECONDS = 900
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def operator():
    return User.objects.create_user(
        phone_number=OPERATOR_PHONE, role='admin', is_staff=True,
        is_superuser=True, password=OPERATOR_PASSWORD,
    )


@pytest.fixture
def plain_rider():
    u = User.objects.create_user(
        phone_number=RIDER_PHONE, role='rider', password=RIDER_PASSWORD)
    Rider.objects.create(user_id=u)
    return u


def _csrf(client):
    resp = client.get(LOGIN_URL)
    assert resp.status_code == 200, resp.status_code
    m = re.search(rb'name="csrfmiddlewaretoken" value="([^"]+)"', resp.content)
    return m.group(1).decode() if m else client.cookies['csrftoken'].value


def _attempt(client, phone, password):
    return client.post(LOGIN_URL, {
        'csrfmiddlewaretoken': _csrf(client),
        'username': phone,
        'password': password,
    })


def _error_text(resp):
    body = resp.content.decode('utf-8', 'ignore')
    found = re.findall(
        r'(Invalid phone number or password\.?|do not have admin privileges'
        r'|Phone number and password are required\.?)', body)
    return found[0] if found else ''


# ---------------------------------------------------------------------------
# The gap that was measured on QA
# ---------------------------------------------------------------------------

def test_repeated_failures_are_eventually_refused(client, operator, settings):
    """The finding: this used to be unbounded."""
    for i in range(settings.OPS_LOGIN_MAX_ATTEMPTS):
        resp = _attempt(client, OPERATOR_PHONE, f'wrong-{i}')
        assert resp.status_code == 200

    # One past the threshold: even the CORRECT password must now be refused.
    resp = _attempt(client, OPERATOR_PHONE, OPERATOR_PASSWORD)

    assert resp.status_code == 200, 'expected the form again, not a redirect'
    assert '_auth_user_id' not in client.session, (
        'the correct password was accepted while the account was locked out'
    )


def test_a_correct_password_works_before_the_threshold(client, operator):
    """The control. A guard that blocks legitimate operators is not a guard."""
    _attempt(client, OPERATOR_PHONE, 'wrong-once')

    resp = _attempt(client, OPERATOR_PHONE, OPERATOR_PASSWORD)

    assert resp.status_code in (301, 302), (
        f'a valid operator login returned {resp.status_code}'
    )
    assert '_auth_user_id' in client.session


def test_a_successful_login_clears_the_counter(client, operator, settings):
    """Otherwise a busy operator who mistypes twice a day locks themselves out."""
    for i in range(settings.OPS_LOGIN_MAX_ATTEMPTS - 1):
        _attempt(client, OPERATOR_PHONE, f'wrong-{i}')

    ok = _attempt(client, OPERATOR_PHONE, OPERATOR_PASSWORD)
    assert ok.status_code in (301, 302)

    client.logout()
    for i in range(settings.OPS_LOGIN_MAX_ATTEMPTS - 1):
        _attempt(client, OPERATOR_PHONE, f'again-{i}')
    resp = _attempt(client, OPERATOR_PHONE, OPERATOR_PASSWORD)

    assert resp.status_code in (301, 302), (
        'the failure counter was not cleared by the earlier success'
    )


# ---------------------------------------------------------------------------
# No enumeration
# ---------------------------------------------------------------------------

def test_the_lockout_message_is_the_ordinary_refusal(client, operator, settings):
    """A distinct "locked" message for real accounts only would be an oracle."""
    wrong_password_error = _error_text(_attempt(client, OPERATOR_PHONE, 'wrong-1'))

    for i in range(settings.OPS_LOGIN_MAX_ATTEMPTS + 2):
        resp = _attempt(client, OPERATOR_PHONE, f'wrong-more-{i}')
    locked_error = _error_text(resp)

    assert locked_error == wrong_password_error, (
        f'locked message {locked_error!r} differs from wrong-password message '
        f'{wrong_password_error!r}, which distinguishes real accounts'
    )


def test_an_unknown_phone_looks_the_same_as_a_known_one(client, operator):
    """Verified on QA too: the form already did not enumerate. It must stay that way."""
    known = _error_text(_attempt(client, OPERATOR_PHONE, 'wrong'))
    cache.clear()
    unknown = _error_text(_attempt(client, '+919577009999', 'wrong'))

    assert known == unknown
    assert known, 'no error was surfaced at all, which is its own problem'


# ---------------------------------------------------------------------------
# A non-operator account must not be a free oracle
# ---------------------------------------------------------------------------

def test_a_valid_password_on_a_rider_account_still_counts_as_a_failure(
    client, plain_rider, settings,
):
    """Otherwise the guard is trivially bypassed.

    The rider's real password is accepted by authenticate() and only then rejected
    for lacking the operator role. If that path did not count, an attacker could
    guess passwords against any non-operator account without limit.
    """
    for i in range(settings.OPS_LOGIN_MAX_ATTEMPTS):
        resp = _attempt(client, RIDER_PHONE, RIDER_PASSWORD)
        assert '_auth_user_id' not in client.session

    # The counter must have advanced -- the next attempt is refused generically
    # rather than with the privileges message.
    resp = _attempt(client, RIDER_PHONE, RIDER_PASSWORD)
    assert _error_text(resp) == 'Invalid phone number or password.', (
        'a non-operator account with a correct password was not throttled'
    )


# ---------------------------------------------------------------------------
# Bounded, not permanent
# ---------------------------------------------------------------------------

def test_the_lockout_expires(client, operator, settings):
    """A permanent lock would be a denial of service against the operator."""
    for i in range(settings.OPS_LOGIN_MAX_ATTEMPTS + 1):
        _attempt(client, OPERATOR_PHONE, f'wrong-{i}')
    assert _attempt(client, OPERATOR_PHONE, OPERATOR_PASSWORD).status_code == 200

    # Simulate the window elapsing. Clearing the cache is what expiry does.
    cache.clear()

    resp = _attempt(client, OPERATOR_PHONE, OPERATOR_PASSWORD)
    assert resp.status_code in (301, 302), 'the lockout did not expire'


# ---------------------------------------------------------------------------
# Per-IP bound
# ---------------------------------------------------------------------------

def test_one_source_working_through_many_phones_is_stopped(client, settings):
    """Per-phone counting alone would miss a host trying a list of numbers."""
    settings.OPS_LOGIN_MAX_ATTEMPTS = 100        # take per-phone out of the way
    settings.OPS_LOGIN_MAX_ATTEMPTS_PER_IP = 6

    refused_generically = 0
    for i in range(10):
        resp = _attempt(client, f'+9195770100{i:02d}', 'wrong')
        if _error_text(resp) == 'Invalid phone number or password.':
            refused_generically += 1

    from servers.admin_dashboard import login_guard
    phone_fails, ip_fails = login_guard.failure_count(
        type('R', (), {'META': {'REMOTE_ADDR': '127.0.0.1'}})(), None)
    assert ip_fails >= settings.OPS_LOGIN_MAX_ATTEMPTS_PER_IP, (
        f'per-IP failures only reached {ip_fails}; one host can work through a '
        'list of phone numbers unchecked'
    )


# ---------------------------------------------------------------------------
# The deliberate fail-open, asserted so it is a choice and not an accident
# ---------------------------------------------------------------------------

def test_a_cache_failure_allows_login_rather_than_locking_everyone_out(
    client, operator,
):
    """Deliberate, and the less safe direction.

    The cache is Redis. Failing closed would lock every operator out of the console
    during exactly the incident when they most need to get in, which is worse for a
    ten-driver pilot than temporarily losing brute-force protection. Asserted here
    so the tradeoff is explicit rather than discovered later.
    """
    with mock.patch('servers.admin_dashboard.login_guard.cache') as fake:
        fake.get.side_effect = RuntimeError('redis unreachable')
        fake.set.side_effect = RuntimeError('redis unreachable')
        fake.delete.side_effect = RuntimeError('redis unreachable')

        resp = _attempt(client, OPERATOR_PHONE, OPERATOR_PASSWORD)

    assert resp.status_code in (301, 302), (
        'a cache outage blocked a valid operator login'
    )


def test_the_guard_never_raises_into_the_login_view(client, operator):
    """A guard that 500s the login page is worse than no guard."""
    with mock.patch('servers.admin_dashboard.login_guard.cache') as fake:
        fake.get.side_effect = RuntimeError('boom')
        fake.set.side_effect = RuntimeError('boom')

        resp = _attempt(client, OPERATOR_PHONE, 'wrong')

    assert resp.status_code == 200
