"""Structural guard: every route in the admin namespace carries a capability.

WHY A URL WALK AND NOT A MODULE LIST
------------------------------------
`test_admin_api_is_scoped.py` checks the view MODULES somebody listed. The
three endpoints this guard was written for -- the SOS queue, the support
ticket list, the zone and rate-card viewsets -- were in modules nobody had
listed, gated on bare `IsAdmin` or on "any operator", and no test noticed
because no test was looking there. A module list protects the modules
somebody remembered.

So this walks the URL RESOLVER. Every route that ships is visited, the view
behind it is resolved, and the view must carry a `ScopedPermission` with a
non-empty capability. An admin route added tomorrow is covered the moment
it is routed, in whatever module it lives.

WHICH ROUTES COUNT AS "ADMIN NAMESPACE"
---------------------------------------
Three rules, applied to the full path as the resolver composes it:

  1. under `api/v1/` and containing an `admin/` segment -- that is the
     operator API's naming convention (`driver/admin/`, `sos/admin/`,
     `pricing/admin/zones/`, `auth/admin/login/`);
  2. under `api/v1/admin-access/` -- the access-model API itself;
  3. ANY other route whose callback is the same object as a route matched
     by (1) or (2). The console still calls `/sos/<id>/acknowledge/`; those
     aliases share their view with `/sos/admin/<id>/acknowledge/` and are
     checked as that view, so an alias cannot be used to reach an admin
     view around its path.

Excluded by construction: the Django admin site at `admin/` (not under
`api/v1/`, and not DRF) and the session console at the site root, which
has its own guard in `test_admin_dashboard_authz.py`.

`ALLOWLIST` is the only escape hatch. Each entry names the route and the
reason, every entry is asserted to still exist AND still be ungated (so a
stale entry fails rather than silently widening), and its size is pinned.

PROVING THE GUARD BITES
-----------------------
A guard that has never failed has never been shown to work. Two proofs:

  * `test_a_planted_ungated_view_is_reported` builds a URLconf with one
    extra admin route gated on bare `IsAdmin` and asserts the check
    function names it.
  * `test_the_guard_fails_the_run_when_a_view_is_planted` runs this file in
    a subprocess with `SAARADHIGO_PLANT_UNGATED_ADMIN_VIEW=1`, which makes
    the module import below append the same planted route to the LIVE
    URLconf, and asserts the exit code is non-zero -- and zero without the
    variable. The exit code is what CI gates on, so that is what is proved.
"""

import os
import re
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.urls import clear_url_caches, get_resolver, path
from django.urls.resolvers import URLPattern, URLResolver
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base.permissions import IsAdmin
from servers.admin_access.bridge import ScopedPermission

PLANT_ENV = 'SAARADHIGO_PLANT_UNGATED_ADMIN_VIEW'
PLANTED_PATH = 'api/v1/planted/admin/ungated/'


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsAdmin])
def planted_ungated_admin_view(request):  # pragma: no cover - never called
    """An admin route gated on "any operator". Exactly the hole."""
    raise AssertionError('planted view must never be executed')


if os.environ.get(PLANT_ENV) == '1':
    # Plant into the LIVE URLconf so the real test below sees a real route.
    # Only ever set by the subprocess proof at the bottom of this file.
    import base.urls

    base.urls.urlpatterns.append(path(PLANTED_PATH, planted_ungated_admin_view))
    clear_url_caches()


# ---------------------------------------------------------------------------
# The allowlist
# ---------------------------------------------------------------------------

#: route -> why it legitimately carries no ScopedPermission. Every entry is a
#: decision with a reason; anything found beyond these fails the build.
ALLOWLIST = {
    # Mints the operator token. A permission is read from the account the
    # token has not yet been issued for, so nothing can gate it; its own
    # gates (is_operator, password, MFA, throttle) run inside the view.
    'api/v1/auth/admin/login/': 'Authenticates; runs before any permission exists.',
    # Returns the caller's OWN roles, permissions and scopes so the console
    # can draw itself. It must answer for an operator with no assignment yet
    # (the console shows "not yet migrated"), so no capability can gate it;
    # it refuses non-operators inside and returns nothing about anyone else.
    'api/v1/admin-access/me/': 'Self-description for the console; is_operator checked inside.',
    # `api/v1/auth/admin/invite/` is NOT here any more (owner's ruling,
    # 2026-10-28: no allowlist). Operator invites carry `admin.manage`;
    # rider/driver test-account creation moved to `admin/invite/test-rider/`
    # and `test-driver/` (`rider.write` / `driver.write`), which are routed
    # outside production only -- `test_production_boot_guards` proves the
    # URL does not resolve in a production boot.
}

#: Methods a view answers to that count as endpoints. OPTIONS/HEAD/TRACE
#: are DRF plumbing and are never the thing being protected.
METHODS = ('get', 'post', 'put', 'patch', 'delete')

ADMIN_SEGMENT = re.compile(r'(^|/)admin/')


# ---------------------------------------------------------------------------
# Walking the resolver
# ---------------------------------------------------------------------------

def _clean(pattern):
    """A route's pattern as a path fragment, regex anchors stripped."""
    text = str(pattern)
    return text.lstrip('^').rstrip('$')


def all_routes(urlconf=None):
    """Every `(path, callback)` the resolver knows, resolvers flattened."""
    found = []

    def walk(patterns, prefix):
        for entry in patterns:
            if isinstance(entry, URLResolver):
                walk(entry.url_patterns, prefix + _clean(entry.pattern))
            elif isinstance(entry, URLPattern):
                found.append((prefix + _clean(entry.pattern), entry.callback))

    walk(get_resolver(urlconf).url_patterns, '')
    return found


def is_admin_path(route):
    if route.startswith('api/v1/admin-access/'):
        return True
    return route.startswith('api/v1/') and bool(ADMIN_SEGMENT.search(route))


def admin_routes(urlconf=None):
    """The admin namespace: rules (1), (2) and the alias rule (3)."""
    routes = all_routes(urlconf)
    by_path = [(r, cb) for r, cb in routes if is_admin_path(r)]
    admin_callbacks = {id(cb) for _, cb in by_path}
    aliases = [
        (r, cb) for r, cb in routes
        if not is_admin_path(r) and id(cb) in admin_callbacks
    ]
    return by_path + aliases


def _view_class(callback):
    """The APIView / ViewSet class behind a DRF callback, or None.

    `@api_view` and `ViewSet.as_view()` both hang the class off the
    callable as `.cls`. A plain Django view has none, and is reported as
    ungated: an admin route that is not even a DRF view is not something
    this guard can vouch for.
    """
    return getattr(callback, 'cls', None)


def gates_for(callback):
    """`{METHOD: [permission instances]}` for every method the view serves.

    Function views: the permission classes DRF stored on the wrapper class.
    ViewSets and other APIViews: `get_permissions()` is called with a stub
    request per method, because a viewset may (and ours do) choose its
    gate by method -- `zone.read` to look, `pricing.write` to change.
    """
    cls = _view_class(callback)
    if cls is None:
        return {}

    actions = getattr(callback, 'actions', None)  # viewset routes carry these
    if actions:
        methods = [m for m in actions if m in METHODS]
    else:
        methods = [m for m in getattr(cls, 'http_method_names', METHODS) if m in METHODS]

    gates = {}
    for method in methods:
        view = cls()
        view.request = SimpleNamespace(method=method.upper(), user=None)
        view.action = actions.get(method) if actions else None
        view.format_kwarg = None
        try:
            permissions = view.get_permissions()
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            permissions = [exc]
        gates[method.upper()] = permissions
    return gates


def _is_scoped_gate(permission):
    return (
        isinstance(permission, ScopedPermission)
        and bool(getattr(permission, 'permission', ''))
    )


def ungated_admin_routes(urlconf=None, allowlist=ALLOWLIST):
    """`{route: reason}` for every admin route lacking a ScopedPermission.

    This is the check. The tests below assert it is empty against the real
    URLconf and non-empty against one with a planted hole.
    """
    problems = {}
    for route, callback in admin_routes(urlconf):
        if route in allowlist:
            continue
        gates = gates_for(callback)
        if not gates:
            problems[route] = (
                f'{getattr(callback, "__name__", callback)!r} is not a DRF view '
                f'(no permission classes to inspect)'
            )
            continue
        for method, permissions in gates.items():
            if not any(_is_scoped_gate(p) for p in permissions):
                problems[route] = (
                    f'{method} {route} -> {_view_class(callback).__name__}: '
                    f'permissions {permissions!r} contain no ScopedPermission '
                    f'with a capability'
                )
                break
    return problems


# ---------------------------------------------------------------------------
# The guard itself
# ---------------------------------------------------------------------------

def test_the_walk_finds_the_admin_namespace():
    """A refactor that hid routes from the walk would make the guard vacuous."""
    routes = {r for r, _ in admin_routes()}

    assert len(routes) >= 30, sorted(routes)
    for expected in (
        'api/v1/driver/admin/',
        'api/v1/ride/admin/trips/',
        'api/v1/sos/admin/',
        'api/v1/support/admin/tickets/',
        'api/v1/pricing/admin/zones/',
        'api/v1/pricing/admin/rate-cards/',
        'api/v1/auth/admin/login/',
        # The alias rule: the console's path to the same view.
        'api/v1/sos/<int:sos_id>/acknowledge/',
    ):
        assert expected in routes, f'{expected} not visited'


def test_every_admin_route_is_gated():
    """The guard. Bare `IsAdmin` means "any operator, anywhere"; that is the hole."""
    problems = ungated_admin_routes()

    assert problems == {}, '\n'.join(
        f'  {route}: {why}' for route, why in sorted(problems.items())
    )


def test_the_allowlist_is_exactly_what_it_says():
    """Every allowlisted route must exist and must actually be ungated.

    A route that gained a gate must leave the list (or the list quietly
    grows a dead entry somebody later copies); a route that was removed
    must leave it too. And it stays small: adding to it is a reviewed act.
    """
    routes = dict(admin_routes())
    for route, reason in ALLOWLIST.items():
        assert reason.strip(), f'{route} has no reason'
        assert route in routes, f'allowlisted {route} is not routed any more'
        gates = gates_for(routes[route])
        assert gates, f'allowlisted {route} is not a DRF view'
        assert not all(
            any(_is_scoped_gate(p) for p in permissions)
            for permissions in gates.values()
        ), f'{route} is gated now; remove it from ALLOWLIST'

    assert len(ALLOWLIST) <= 2, sorted(ALLOWLIST)


def test_viewset_gates_are_read_per_method():
    """The pricing viewsets choose their gate by method; the guard must see both.

    If `gates_for` only read `permission_classes` it would miss a viewset
    whose `get_permissions` dropped the gate for writes.
    """
    routes = dict(admin_routes())
    gates = gates_for(routes['api/v1/pricing/admin/zones/'])

    assert {p.permission for p in gates['GET'] if _is_scoped_gate(p)} == {'zone.read'}
    assert {p.permission for p in gates['POST'] if _is_scoped_gate(p)} == {'pricing.write'}


# ---------------------------------------------------------------------------
# Proof that it bites
# ---------------------------------------------------------------------------

def test_a_planted_ungated_view_is_reported():
    """One extra admin route gated on bare IsAdmin, in a throwaway URLconf."""
    import importlib

    root = importlib.import_module(settings.ROOT_URLCONF)
    planted = types.ModuleType('tests_planted_urlconf')
    planted.urlpatterns = [path(PLANTED_PATH, planted_ungated_admin_view)] + list(root.urlpatterns)
    sys.modules[planted.__name__] = planted
    try:
        problems = ungated_admin_routes(urlconf=planted.__name__)
    finally:
        sys.modules.pop(planted.__name__, None)
        clear_url_caches()

    assert PLANTED_PATH in problems, problems
    assert 'planted_ungated_admin_view' in problems[PLANTED_PATH]
    assert 'IsAdmin' in problems[PLANTED_PATH]
    # And nothing else: the plant is the only hole.
    assert set(problems) == {PLANTED_PATH}, problems


def _run_guard(extra_env):
    """`pytest` on the guard test alone, in a fresh interpreter."""
    env = dict(os.environ)
    env.pop(PLANT_ENV, None)
    env.update(extra_env)
    completed = subprocess.run(  # noqa: S603 - fixed argv, our own interpreter
        [
            sys.executable, '-m', 'pytest', str(Path(__file__)),
            '-q', '-p', 'no:cacheprovider', '--no-header',
            '-k', 'test_every_admin_route_is_gated',
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        env=env, capture_output=True, text=True, timeout=600,
    )
    return completed.returncode, completed.stdout + completed.stderr


def test_the_guard_fails_the_run_when_a_view_is_planted():
    """The exit code is what CI gates on, so the exit code is what is proved.

    Two subprocesses: with the plant the run must FAIL and name the route;
    without it the same run must pass. Asserting only the first would be
    satisfied by a guard that fails for an unrelated reason.
    """
    if os.environ.get(PLANT_ENV) == '1':
        pytest.skip('inside the planted subprocess')

    rc_planted, out_planted = _run_guard({PLANT_ENV: '1'})
    assert rc_planted != 0, f'planted view was NOT caught (exit 0):\n{out_planted[-3000:]}'
    assert PLANTED_PATH in out_planted, out_planted[-3000:]

    rc_clean, out_clean = _run_guard({})
    assert rc_clean == 0, f'guard fails without a plant (exit {rc_clean}):\n{out_clean[-3000:]}'
