"""Security regression: every admin-dashboard route must refuse anonymous callers.

Three routed views (`payment_dashboard`, `executive_revenue`,
`dispute_support`) shipped without the `@admin_required` decorator that
every other view in `servers/admin_dashboard/views.py` carries. Because the
dashboard is mounted at the site root (`path('', include(admin_urls))` in
`base/urls.py`), that made the Cashfree gateway ledger, rider payments,
driver payout transactions, the webhook log and the GMV/revenue figures
readable by any unauthenticated caller who knew the path.

These tests are deliberately written against `urlpatterns` rather than
against a hand-maintained list of paths, so a view added to the admin
dashboard tomorrow is covered the moment it is routed. A new route is
guarded, or this suite fails — there is no third outcome, and nobody has to
remember to extend a fixture.

`PUBLIC_ROUTE_NAMES` is the only escape hatch and it is asserted to stay
small: adding a name to it is the explicit, reviewable act of declaring a
route public.
"""

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import NoReverseMatch, reverse

from servers.admin_dashboard.urls import urlpatterns as admin_urlpatterns

User = get_user_model()


# Routes that are unauthenticated *by design*. `login` has to be reachable
# before you have a session; `logout` must work even from a broken one.
# Anything else belongs behind the guard.
PUBLIC_ROUTE_NAMES = frozenset({'login', 'logout'})

# Status codes that count as "refused". 302 is what `admin_required` does
# (redirect to the login page); `staff_member_required` also redirects.
# 401/403 are accepted so a future move to a DRF-style permission or a
# hard deny does not require editing this test.
REFUSING_STATUS = frozenset({301, 302, 303, 307, 308, 401, 403})


def _route_names():
    """Every named route the admin dashboard mounts."""
    names = []
    for pattern in admin_urlpatterns:
        name = getattr(pattern, 'name', None)
        if name:
            names.append(name)
    return names


def _resolve(name):
    """Concrete URL for a route name, filling any captured args with 1.

    Returns None when the route cannot be reversed with a single positional
    argument, which would mean a signature this helper does not understand
    yet; `test_every_route_is_reversible` fails loudly in that case rather
    than letting the route slip through unchecked.
    """
    for args in ((), (1,)):
        try:
            return reverse(name, args=args)
        except NoReverseMatch:
            continue
    return None


ROUTE_NAMES = _route_names()
GUARDED_ROUTE_NAMES = [n for n in ROUTE_NAMES if n not in PUBLIC_ROUTE_NAMES]


def test_admin_dashboard_exposes_routes():
    """Guard against the enumeration silently going empty.

    If `urlpatterns` were refactored into an `include()` this helper would
    stop seeing routes, and every parametrised test below would vacuously
    pass. Fail here instead.
    """
    assert len(ROUTE_NAMES) >= 20, (
        f'Only {len(ROUTE_NAMES)} admin routes discovered; the enumeration in '
        'this test is probably no longer reading the real urlpatterns.'
    )
    assert GUARDED_ROUTE_NAMES, 'No guarded routes discovered at all.'


def test_public_route_allowlist_stays_minimal():
    """The allowlist is the only way to opt out, so it must stay tiny.

    Widening it is a security decision. Making that decision break a test
    forces it to be deliberate and reviewed rather than incidental.
    """
    assert PUBLIC_ROUTE_NAMES == frozenset({'login', 'logout'}), (
        'PUBLIC_ROUTE_NAMES changed. Every name here is an admin-dashboard '
        'route served to anonymous callers — justify it in review.'
    )
    # And every allowlisted name must actually exist, so a rename does not
    # leave a stale entry quietly excusing a real route.
    for name in PUBLIC_ROUTE_NAMES:
        assert name in ROUTE_NAMES, f'Allowlisted route {name!r} is no longer routed.'


@pytest.mark.parametrize('name', ROUTE_NAMES)
def test_every_route_is_reversible(name):
    """Every route must be resolvable so no route escapes the checks below."""
    assert _resolve(name) is not None, (
        f'Route {name!r} could not be reversed with 0 or 1 positional args, so '
        'it is not being checked for anonymous access. Teach _resolve() about it.'
    )


@pytest.mark.django_db
@pytest.mark.parametrize('name', GUARDED_ROUTE_NAMES)
def test_anonymous_cannot_reach_admin_route(name):
    """An unauthenticated GET must be refused, never served."""
    url = _resolve(name)
    response = Client().get(url)

    assert response.status_code != 200, (
        f'{name!r} ({url}) served HTTP 200 to an anonymous caller. Add '
        '@admin_required to the view, or add the route to '
        'PUBLIC_ROUTE_NAMES if it is genuinely public.'
    )
    assert response.status_code in REFUSING_STATUS, (
        f'{name!r} ({url}) returned HTTP {response.status_code} to an anonymous '
        f'caller; expected one of {sorted(REFUSING_STATUS)}.'
    )


@pytest.mark.django_db
@pytest.mark.parametrize('name', GUARDED_ROUTE_NAMES)
def test_authenticated_non_admin_cannot_reach_admin_route(name):
    """A logged-in rider is not an admin.

    The anonymous test alone would pass for a view guarded only by
    `login_required`, which would still hand the payment ledger to any
    rider with an account.
    """
    rider = User.objects.create_user(phone_number='+919000000001', role='rider')
    client = Client()
    client.force_login(rider)

    url = _resolve(name)
    response = client.get(url)

    assert response.status_code != 200, (
        f'{name!r} ({url}) served HTTP 200 to an authenticated rider. The view '
        'is missing a role check, not just an authentication check.'
    )
    assert response.status_code in REFUSING_STATUS, (
        f'{name!r} ({url}) returned HTTP {response.status_code} to a rider; '
        f'expected one of {sorted(REFUSING_STATUS)}.'
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    'name', ['payment_dashboard', 'executive_revenue', 'dispute_support'],
)
def test_regression_the_three_leaked_routes_are_guarded(name):
    """Named regression for the specific routes found open in the audit.

    The parametrised tests above already cover these. This test exists so
    the failure message names the incident directly if one is ever reopened.
    """
    assert name in GUARDED_ROUTE_NAMES, f'{name!r} must not be treated as public.'

    url = _resolve(name)
    response = Client().get(url)
    assert response.status_code in REFUSING_STATUS, (
        f'REGRESSION: {name!r} ({url}) is reachable without authentication '
        f'again (HTTP {response.status_code}). This route renders financial '
        'data — Cashfree ledger, payouts, webhook log or GMV.'
    )
