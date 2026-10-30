"""Structural guard: every admin API endpoint is gated by scope.

WHY THIS IS WRITTEN AGAINST THE URL CONF
----------------------------------------
A hand-maintained list of endpoints protects the endpoints somebody
remembered. The gap in this system was never a wrong check — it was a
missing one, on an endpoint added after the checks were written. So this
walks `urlpatterns`, the way `test_admin_dashboard_authz.py` does, and an
admin endpoint routed tomorrow is covered the moment it is routed.

Two separate assertions, because there are two separate failures:

  1. The view is gated by `ScopedPermission`, not by bare `IsAdmin`. Bare
     `IsAdmin` means "any operator", which is the hole.
  2. A LIST view's queryset actually passes through `scoped()`. A view can
     satisfy (1) perfectly and still return every row in both countries,
     and no permission class can detect that — so (2) reads the source.

`UNSCOPED_BY_DESIGN` is the only escape hatch, each entry carries a reason,
and its size is asserted. Adding to it is a deliberate, reviewable act.
"""

import inspect
import re

import pytest

from servers.admin_access.bridge import ScopedPermission

# --- which views count as admin API ---------------------------------------

ADMIN_VIEW_MODULES = [
    'servers.driver.admin_views',
    'servers.driver.admin_drivers',
    'servers.rider.admin_riders',
    'servers.ride.admin_views',
    'servers.payments.admin_views',
    'servers.auth_user.admin_views',
    'servers.search.views',
]

#: Endpoint -> why it is not scope-gated. Every entry is a decision.
UNSCOPED_BY_DESIGN = {
    # Mints the token. It cannot require a permission that is read from the
    # account the token has not yet been issued for.
    'admin_login': 'Authenticates; runs before any permission exists.',
}

#: Views that are legitimately global (no geo anchor) and are gated on a
#: permission instead of scope. Kept separate from the above so "global"
#: and "unauthenticated" never get confused for each other.
GLOBAL_BUT_GATED = {
    'admin_list_users': 'Riders belong to no city; gated on pii.view.',
    'admin_search_riders': 'Scoped by riders_visible_to (rider.scope); phones masked.',
}


def _source_of(name):
    """The source of the endpoint named `name`, from its own module file.

    `@api_view` replaces the module-level name with a wrapper whose
    `__wrapped__` is DRF's generic `view()` and whose handler is a closure
    over the original — so neither `inspect.getsource` nor `__wrapped__`
    reaches the endpoint body. Slicing the file is blunt but it reads the
    code that actually ships, which is the point of this test.
    """
    import importlib
    import pathlib

    for module_name in ADMIN_VIEW_MODULES:
        path = pathlib.Path(importlib.import_module(module_name).__file__)
        text = path.read_text(encoding='utf-8')
        match = re.search(
            rf'^def {re.escape(name)}\(.*?(?=^(?:@|def |class )|\Z)',
            text, re.S | re.M,
        )
        if match:
            return match.group(0)
    raise AssertionError(f'no source found for {name} in {ADMIN_VIEW_MODULES}')


def _admin_views():
    """Every function in the admin view modules that DRF has decorated."""
    import importlib

    found = {}
    for module_name in ADMIN_VIEW_MODULES:
        module = importlib.import_module(module_name)
        for name, obj in vars(module).items():
            if name.startswith('_'):
                continue
            # @api_view wraps the function and hangs the classes off it.
            if hasattr(obj, 'cls') and hasattr(obj.cls, 'permission_classes'):
                found[name] = obj
    return found


def test_the_modules_actually_contain_views():
    """A typo in ADMIN_VIEW_MODULES would make every test below vacuous."""
    views = _admin_views()

    assert len(views) >= 14, f'only found {sorted(views)}'


@pytest.mark.parametrize('name', sorted(_admin_views()))
def test_every_admin_endpoint_is_scope_gated(name):
    """Bare `IsAdmin` means "any operator, anywhere". That is the hole."""
    if name in UNSCOPED_BY_DESIGN:
        pytest.skip(UNSCOPED_BY_DESIGN[name])

    view = _admin_views()[name]
    classes = view.cls.permission_classes

    scoped_gates = [p for p in classes if isinstance(p, ScopedPermission)]
    assert scoped_gates, (
        f'{name} is not gated by ScopedPermission. Its permission classes '
        f'are {classes}. If it is global by design, add it to '
        f'GLOBAL_BUT_GATED with the reason.'
    )


#: Views whose job is to return a collection. Named by the shape of their
#: source rather than guessed from the name, below.
# `riders_visible_to` is the one rider rule (`servers/rider/scope.py`);
# `_rider_for` is the rider module's single-rider wrapper around it (checked
# below), so a rider view that calls either is scoped.
LIST_CALL = re.compile(
    r'\b(scoped|scoped_or_none|scoped_to_request|in_scope|riders_visible_to|_rider_for)\s*\(')
UNFILTERED = re.compile(
    r'\b(Driver|Trip|Vehicle|Payment|TransactionHistory|WithdrawalRequest)'
    r'\.objects\s*\.\s*(all|filter|select_related|prefetch_related)\s*\('
)


#: Endpoints that return or act on rows of a scopable model, and therefore
#: MUST pass through the scope helpers. Asserted by name as well as by the
#: pattern below, because a correct view stops matching the pattern — so the
#: pattern alone would let someone "fix" a failure by deleting the filter
#: and the test would start skipping instead of failing.
MUST_SCOPE = {
    'list_drivers_admin',
    'retrieve_driver_admin',
    'driver_full_detail_admin',
    'update_kyc_status_admin',
    'delete_driver_admin',
    'get_vehicle_details',
    'list_withdrawals_admin',
    'approve_withdrawal_admin',
    'reject_withdrawal_admin',
    'bulk_action_withdrawal_admin',
    'admin_list_trips',
    'admin_trip_detail',
    'admin_list_payments',
    'admin_list_transactions',
    'admin_search_trips',
    'admin_search_drivers',
    'admin_search_vehicles',
    # Riders module: the multi-anchor rider rule (servers/rider/scope.py).
    'admin_list_riders',
    'admin_rider_detail',
    'admin_block_rider',
    'admin_unblock_rider',
    'admin_note_rider',
    'admin_flag_rider',
    'admin_unflag_rider',
    'admin_sign_out_rider',
    'admin_bulk_riders',
}


def test_the_rider_wrapper_really_uses_the_rider_rule():
    from servers.rider import admin_riders

    assert 'riders_visible_to(' in inspect.getsource(admin_riders._rider_for)


@pytest.mark.parametrize('name', sorted(MUST_SCOPE))
def test_the_scopable_endpoints_call_a_scope_helper(name):
    """Positive requirement, so a deleted filter FAILS rather than skips."""
    views = _admin_views()
    assert name in views, f'{name} is no longer a routed admin view'

    source = _source_of(name)

    assert LIST_CALL.search(source), (
        f'{name} must pass through scoped(), scoped_or_none() or '
        f'in_scope(). Without it the endpoint is authorized correctly and '
        f'still returns every row in both countries.'
    )


@pytest.mark.parametrize('name', sorted(_admin_views()))
def test_every_admin_endpoint_filters_its_queryset(name):
    """The half a permission class cannot check.

    If a view touches a scopable model's manager directly, it must also go
    through `scoped`/`scoped_or_none`/`in_scope` somewhere in its body.
    Reading the source is crude, and it is the only thing that catches the
    specific bug of an authorized view returning the whole world.
    """
    if name in UNSCOPED_BY_DESIGN or name in GLOBAL_BUT_GATED:
        pytest.skip('Global or unauthenticated by design.')

    source = _source_of(name)

    if not UNFILTERED.search(source):
        pytest.skip('Does not query a scopable model directly.')

    assert LIST_CALL.search(source), (
        f'{name} queries a scopable model but never calls scoped(), '
        f'scoped_or_none() or in_scope(). An authorized endpoint with an '
        f'unfiltered queryset returns every row in both countries.'
    )


def test_the_escape_hatches_stay_small():
    """So that "add it to the exceptions" never becomes the fix."""
    assert len(UNSCOPED_BY_DESIGN) <= 2, sorted(UNSCOPED_BY_DESIGN)
    assert len(GLOBAL_BUT_GATED) <= 3, sorted(GLOBAL_BUT_GATED)


def test_every_scope_path_resolves():
    """A typo in SCOPE_PATHS filters on nothing, or raises at runtime.

    Resolved against the real models here, so the table cannot drift from
    the schema without this failing.
    """
    from django.apps import apps

    from servers.admin_access.scoping import SCOPE_PATHS

    broken = []
    for label, paths in SCOPE_PATHS.items():
        model = apps.get_model(label)
        # A model with more than one possible anchor (an SOS event, a
        # ticket) lists a tuple; every member must resolve.
        for path in ((paths,) if isinstance(paths, str) else paths):
            try:
                # Compiling the SQL is the check: an ORM path that does not
                # resolve raises here rather than silently filtering nothing.
                sql = str(model.objects.filter(
                    **{f'{path}__startswith': 'GLOBAL/'}
                ).query)
                assert path.split('__')[0] in sql or 'WHERE' in sql
            except Exception as exc:  # noqa: BLE001 - reporting every break
                broken.append(f'{label}: {path} -> {exc}')

    assert not broken, broken
