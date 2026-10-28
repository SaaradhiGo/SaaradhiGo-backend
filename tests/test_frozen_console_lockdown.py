"""The frozen Django console is reachable by a Super Admin, on allowlisted routes, once audited.

THE DECISION UNDER TEST
-----------------------
AUDIT.md section 10, item 1: Next.js is the admin; `servers/admin_dashboard`
and the Django admin site are frozen, restricted to Super Admin plus a config
allowlist, retired screen by screen. Item (f) of the plan offered two ways to
honour that for the console -- retrofit city scoping and PII masking into it,
or restrict its access. This is the restriction, and these tests are what
"restricted" means.

WHY THE ROUTES COME FROM THE RESOLVER
-------------------------------------
A hand-written list of console routes protects the routes somebody
remembered. Every parametrised test below is built from the live URL
resolver, filtered by the same `is_frozen_route` the middleware uses, so a
route added to the console or a model registered with the admin site is
tested the moment it is routed. Separately, the resolver's set of frozen
route names must equal the static inventory in
`servers/admin_dashboard/inventory.py` -- the parity table and the boot
guard's vocabulary -- so nothing can be routed without a line saying what it
shows and what replaces it.

404 IS THE REFUSAL
------------------
Not 403. A retired screen must not advertise itself; to anyone short of a
Super Admin on an allowlisted route it looks exactly like a URL that was
never mounted. The one exception is a Super Admin whose session has not yet
presented its second factor, who is sent to the challenge: that redirect
tells somebody who already proved a Super Admin's password nothing new.
"""

import re

import pytest
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import Resolver404, get_resolver, resolve, reverse
from django.urls.resolvers import URLPattern, URLResolver
from django_otp import DEVICE_ID_SESSION_KEY

from base import ops_mfa
from conftest import grant_legacy_equivalent
from servers.admin_audit.models import AdminAuditLog
from servers.admin_dashboard import inventory, lockdown

User = get_user_model()

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Walking the resolver
# ---------------------------------------------------------------------------

def _clean(pattern):
    return str(pattern).lstrip('^').rstrip('$')


def _all_routes():
    """`(route pattern, namespaced view name)` for every route that ships."""
    found = []

    def walk(patterns, prefix, namespaces):
        for entry in patterns:
            if isinstance(entry, URLResolver):
                walk(
                    entry.url_patterns, prefix + _clean(entry.pattern),
                    namespaces + ([entry.namespace] if entry.namespace else []),
                )
            elif isinstance(entry, URLPattern):
                name = ':'.join(namespaces + [entry.name]) if entry.name else ''
                found.append((prefix + _clean(entry.pattern), name))

    walk(get_resolver().url_patterns, '', [])
    return found


_CONVERTER = re.compile(r'<(?:\w+:)?\w+>')
_REGEX_GROUP = re.compile(r'\(\?P<\w+>([^)|]+)(?:\|[^)]*)?\)')


def _concrete(route):
    """A URL that resolves to `route`: converters become 1, a regex
    alternation becomes its first alternative."""
    path = _CONVERTER.sub('1', route)
    path = _REGEX_GROUP.sub(lambda m: m.group(1), path)
    return '/' + path


def frozen_routes():
    """`[(url, view name)]` for every route the middleware treats as frozen,
    decided by resolving a concrete URL exactly as a request would."""
    out = []
    for route, name in _all_routes():
        url = _concrete(route)
        try:
            match = resolve(url)
        except Resolver404:
            continue
        if lockdown.is_frozen_route(match):
            out.append((url, name))
    return out


FROZEN = frozen_routes()
GATED = [(url, name) for url, name in FROZEN if name not in inventory.ENTRY_ROUTES]
NAMED_GATED = [(url, name) for url, name in GATED if name]
ENTRY = [(url, name) for url, name in FROZEN if name in inventory.ENTRY_ROUTES]

_ids = [name or f'(unnamed) {url}' for url, name in GATED]
_named_ids = [name for _, name in NAMED_GATED]


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    """The admin's templates call `{% static %}`; the deployment's manifest
    storage raises without a collected manifest, which no test run has. No
    test here is about static files, so the admin renders through the plain
    storage. Nothing else in the suite renders a Django admin page, which is
    why this never came up before."""
    settings.STORAGES = {
        **settings.STORAGES,
        'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    }


def _operator(phone, **extra):
    return User.objects.create_user(
        phone_number=phone, username=phone, role='admin', is_staff=True, **extra,
    )


@pytest.fixture
def rider():
    return User.objects.create_user(phone_number='+919770000001', role='rider')


@pytest.fixture
def unmigrated_operator():
    """Staff, role admin, no assignment: what every operator was before the
    access model. Not a Super Admin."""
    return _operator('+919770000002')


@pytest.fixture
def scoped_operator():
    """A Support Agent at Global: an operator, migrated, not a Super Admin."""
    user = _operator('+919770000003')
    grant_legacy_equivalent(user, 'support')
    return user


@pytest.fixture
def super_admin():
    """`super_admin` at the Global node, through an assignment -- NOT a Django
    superuser, so the test proves the assignment is what the gate reads."""
    user = _operator('+919770000004')
    grant_legacy_equivalent(user, 'admin')
    return user


@pytest.fixture
def superuser():
    """The documented break-glass shape. Counts as Super Admin by
    `access.is_super_admin`, and the gate honours that."""
    return _operator('+919770000005', is_superuser=True)


def _session_for(user):
    client = Client()
    client.force_login(user)
    return client


def _audit_rows():
    return AdminAuditLog.objects.filter(action=lockdown.AUDIT_ACTION)


# ---------------------------------------------------------------------------
# The inventory is the resolver, and the resolver is the inventory
# ---------------------------------------------------------------------------

def test_the_walk_finds_the_frozen_surface():
    """A refactor that hid routes from the walk would make every test below
    vacuous."""
    names = {name for _, name in FROZEN}
    assert len(FROZEN) >= 150, len(FROZEN)
    for expected in ('fleet_monitor', 'driver_profile', 'admin_notifications_feed',
                     'admin:index', 'admin:driver_withdrawalrequest_change',
                     'admin:pricing_ratecard_add', 'admin:app_list'):
        assert expected in names, f'{expected} not treated as frozen'
    # And the rest of the site is NOT swept in.
    assert not lockdown.is_frozen_route(resolve('/healthz'))
    assert not lockdown.is_frozen_route(resolve('/api/v1/config/'))


def test_every_frozen_route_has_a_parity_line_and_vice_versa():
    """A new console view or a newly registered ModelAdmin fails here until
    `inventory.py` says what it shows and which Next.js module replaces it.
    A line for a route that no longer ships fails too, so the parity table
    cannot keep a retired screen alive on paper."""
    routed = {name for _, name in FROZEN if name}
    listed = inventory.known_routes()

    assert routed - listed == set(), (
        f'routed but not in the inventory: {sorted(routed - listed)}'
    )
    assert listed - routed == set(), (
        f'in the inventory but not routed: {sorted(listed - routed)}'
    )


def test_unnamed_frozen_routes_are_only_the_admin_sites_plumbing():
    """The admin's per-object redirect and its catch-all have no name and so
    can never be allowlisted. Anything else unnamed on the frozen surface
    would be a route nobody can switch on but also nobody has listed."""
    unnamed = [url for url, name in FROZEN if not name]
    assert unnamed, 'the admin site always mounts unnamed routes'
    assert all(url.startswith('/admin/') for url in unnamed), unnamed


def test_every_parity_line_names_a_module_and_a_purpose():
    for route in inventory.all_routes():
        assert route.shows.strip(), route.name
        assert route.replaced_by.strip(), route.name


def test_the_policy_sets_are_consistent():
    """Entry routes and never-allowed routes must exist; nothing can be both;
    the default allowlist is one route and that route is harmless."""
    known = inventory.known_routes()
    assert inventory.ENTRY_ROUTES <= known
    assert inventory.NEVER_ALLOWED <= known
    assert not (inventory.ENTRY_ROUTES & inventory.NEVER_ALLOWED)
    assert set(inventory.DEFAULT_ALLOWED_ROUTES) <= known
    assert inventory.DEFAULT_ALLOWED_ROUTES == ('admin:index',)
    assert not set(inventory.DEFAULT_ALLOWED_ROUTES) & inventory.NEVER_ALLOWED


# ---------------------------------------------------------------------------
# Who is refused: everybody but a Super Admin, and even then only on the list
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('url,name', GATED, ids=_ids)
def test_anonymous_gets_404(url, name):
    assert Client().get(url).status_code == 404, f'{name} {url}'


@pytest.mark.parametrize('url,name', GATED, ids=_ids)
def test_a_rider_gets_404(url, name, rider, frozen_console_open):
    """Even with every route allowlisted: the allowlist widens what a Super
    Admin may reach, never who counts as one."""
    assert _session_for(rider).get(url).status_code == 404, f'{name} {url}'


@pytest.mark.parametrize('url,name', GATED, ids=_ids)
def test_an_operator_without_super_admin_gets_404(
        url, name, unmigrated_operator, scoped_operator, frozen_console_open):
    """Neither the unmigrated operator (who, under the bridge, still holds
    every legacy capability) nor a migrated one with a real role gets in.
    Super Admin at Global is the bar, not "is an operator"."""
    assert _session_for(unmigrated_operator).get(url).status_code == 404, f'{name} {url}'
    assert _session_for(scoped_operator).get(url).status_code == 404, f'{name} {url}'


@pytest.mark.parametrize(
    'url,name',
    [(u, n) for u, n in GATED if n not in inventory.DEFAULT_ALLOWED_ROUTES],
    ids=[i for i, (u, n) in zip(_ids, GATED, strict=True) if n not in inventory.DEFAULT_ALLOWED_ROUTES],
)
def test_a_super_admin_gets_404_off_the_allowlist(url, name, super_admin):
    """The shipped default: the admin index and nothing else. A Super Admin
    on any other frozen route sees the same 404 a rider does."""
    assert _session_for(super_admin).get(url).status_code == 404, f'{name} {url}'


@pytest.mark.parametrize('name', sorted(inventory.NEVER_ALLOWED))
def test_never_allowed_routes_stay_404_even_when_configured(name, superuser, settings):
    """`NEVER_ALLOWED` is subtracted from whatever the setting says, so QA
    cannot rehearse a door production refuses to boot with."""
    settings.FROZEN_CONSOLE_ALLOWED_ROUTES = [name]
    url = next(u for u, n in FROZEN if n == name)

    assert _session_for(superuser).get(url).status_code == 404
    assert name not in lockdown.allowed_routes()


# ---------------------------------------------------------------------------
# Who is admitted, and the audit row that proves it
# ---------------------------------------------------------------------------

def test_a_super_admin_reaches_an_allowlisted_route_and_is_audited_once_per_session(super_admin):
    client = _session_for(super_admin)
    assert _audit_rows().count() == 0

    first = client.get(reverse('admin:index'))

    assert first.status_code == 200
    row = _audit_rows().get()
    assert row.actor_id == super_admin.pk
    assert row.target_type == lockdown.AUDIT_TARGET_TYPE
    assert row.target_id == 'admin:index'
    assert row.after['path'] == '/admin/'

    # Same session, same route: no new row. A page that polls would
    # otherwise bury the log.
    second = client.get(reverse('admin:index'))
    assert second.status_code == 200
    assert _audit_rows().count() == 1


def test_a_second_route_in_the_same_session_is_its_own_row(super_admin, settings):
    settings.FROZEN_CONSOLE_ALLOWED_ROUTES = ['admin:index', 'admin:app_list']
    client = _session_for(super_admin)

    client.get(reverse('admin:index'))
    client.get(reverse('admin:app_list', args=['driver']))
    client.get(reverse('admin:index'))

    assert sorted(_audit_rows().values_list('target_id', flat=True)) == [
        'admin:app_list', 'admin:index',
    ]


def test_a_new_session_for_the_same_person_is_audited_again(super_admin):
    """Dedupe is per session, deliberately: "who opened which retired screen,
    and when" has a different answer for each sign-in."""
    _session_for(super_admin).get(reverse('admin:index'))
    _session_for(super_admin).get(reverse('admin:index'))

    assert _audit_rows().count() == 2


def test_a_django_superuser_is_break_glass_and_admitted(superuser):
    """`is_super_admin` counts the superuser flag; the gate uses that one
    definition rather than inventing a stricter one that would lock the
    documented break-glass account out of the console kept for it."""
    response = _session_for(superuser).get(reverse('admin:index'))

    assert response.status_code == 200
    assert _audit_rows().count() == 1


def test_an_allowlisted_console_screen_renders_for_a_super_admin(super_admin, settings):
    """The console half of the surface, not only the admin site."""
    settings.FROZEN_CONSOLE_ALLOWED_ROUTES = ['stale_rides']
    client = _session_for(super_admin)

    assert client.get(reverse('stale_rides')).status_code == 200
    assert client.get(reverse('fleet_monitor')).status_code == 404
    assert _audit_rows().get().target_id == 'stale_rides'


def test_the_setting_accepts_the_environments_comma_separated_string(super_admin, settings):
    settings.FROZEN_CONSOLE_ALLOWED_ROUTES = ' admin:index , stale_rides,, '

    assert lockdown.allowed_routes() == {'admin:index', 'stale_rides'}
    assert _session_for(super_admin).get(reverse('stale_rides')).status_code == 200


# ---------------------------------------------------------------------------
# The second factor is the session's, not the account's
# ---------------------------------------------------------------------------

def test_an_unverified_session_is_refused_until_it_presents_its_factor(super_admin):
    """A Super Admin with an authenticator enrolled holds a session cookie
    from a password alone. That session is sent to the challenge, and is
    admitted only once the session itself has verified -- which is what
    `console_access_allowed` means."""
    device, _uri = ops_mfa.enroll_totp(super_admin)
    device.confirmed = True
    device.save(update_fields=['confirmed'])
    client = _session_for(super_admin)

    refused = client.get(reverse('admin:index'))

    assert refused.status_code == 302
    assert refused['Location'].endswith(reverse('ops_mfa_challenge'))
    assert _audit_rows().count() == 0, 'a refused request must not be audited as access'

    # Mark THIS session verified the way django-otp does, then try again.
    session = client.session
    session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    session.save()

    admitted = client.get(reverse('admin:index'))

    assert admitted.status_code == 200
    assert _audit_rows().count() == 1


def test_mfa_refusal_is_not_offered_to_a_non_super_admin(scoped_operator, frozen_console_open):
    """The redirect to the challenge is a courtesy for a proven Super Admin.
    Anyone else, enrolled or not, gets the 404 -- the gate must not leak
    "this route exists" through the MFA step."""
    device, _uri = ops_mfa.enroll_totp(scoped_operator)
    device.confirmed = True
    device.save(update_fields=['confirmed'])

    assert _session_for(scoped_operator).get(reverse('admin:index')).status_code == 404


# ---------------------------------------------------------------------------
# The doors stay open: sign-in, challenge, sign-out
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('url,name', ENTRY, ids=[n for _, n in ENTRY])
def test_entry_routes_are_never_hidden(url, name):
    """They have their own guards; the gate's job is not to stand in front
    of the only way to become a verified Super Admin, or to stop being one."""
    anonymous = Client().get(url)
    assert anonymous.status_code != 404, f'{name} {url}'
    assert anonymous.status_code in (200, 302, 405), f'{name} {url} -> {anonymous.status_code}'


def test_the_entry_set_is_exactly_the_doors():
    assert inventory.ENTRY_ROUTES == {'login', 'ops_mfa_challenge', 'logout', 'admin:logout'}


def test_the_sign_in_lands_a_super_admin_on_an_allowed_page(super_admin, settings):
    """Before the lockdown the sign-in redirected to the fleet monitor. With
    the fleet monitor off the list that is a 404 the moment somebody signs
    in, which reads as broken rather than restricted."""
    assert lockdown.landing_url() == reverse('admin:index')

    settings.FROZEN_CONSOLE_ALLOWED_ROUTES = ['stale_rides', 'fleet_monitor']
    assert lockdown.landing_url() == reverse('fleet_monitor')

    settings.FROZEN_CONSOLE_ALLOWED_ROUTES = ['driver_profile', 'stale_rides']
    assert lockdown.landing_url() == reverse('stale_rides')

    # Nothing allowed: a dead end, never the sign-in, which would loop.
    settings.FROZEN_CONSOLE_ALLOWED_ROUTES = []
    assert lockdown.landing_url() == reverse('fleet_monitor')
    assert lockdown.landing_url() != reverse('login')


def test_a_verified_super_admin_signing_in_is_redirected_to_the_landing_page(settings):
    password = 'frozen-console-not-a-real-secret'
    user = _operator('+919770000006', password=password)
    grant_legacy_equivalent(user, 'admin')

    response = Client().post(reverse('login'), {'username': '+919770000006', 'password': password})

    assert response.status_code == 302
    assert response['Location'] == reverse('admin:index')


# ---------------------------------------------------------------------------
# The rest of the site is untouched
# ---------------------------------------------------------------------------

def test_the_gate_does_not_touch_the_api_or_the_probes(rider):
    assert Client().get('/healthz').status_code == 200
    assert Client().get('/api/v1/config/').status_code == 200
    # An operator API route still answers for itself (401 to no token), not 404.
    assert Client().get('/api/v1/driver/admin/').status_code in (401, 403)


# ---------------------------------------------------------------------------
# The admin site is read-only
# ---------------------------------------------------------------------------

REGISTRY = sorted(admin.site._registry.items(), key=lambda kv: kv[0]._meta.label_lower)
_registry_ids = [model._meta.label_lower for model, _ in REGISTRY]


def test_the_admin_site_is_the_frozen_one():
    """If INSTALLED_APPS went back to plain `django.contrib.admin`, every
    registration would arrive writable again and only this would notice."""
    assert isinstance(admin.site, lockdown.FrozenAdminSite)
    assert len(REGISTRY) >= 30, _registry_ids
    assert inventory.ADMIN_SITE_MODELS and len(REGISTRY) == len(inventory.ADMIN_SITE_MODELS)


@pytest.mark.parametrize('model,model_admin', REGISTRY, ids=_registry_ids)
def test_every_model_admin_refuses_writes_and_allows_reads(model, model_admin, rf, superuser):
    """Parametrised over the live registry: a ModelAdmin registered tomorrow,
    by this repository or by a dependency, is checked the moment it exists.
    The superuser is the strongest possible caller; if add / change / delete
    are refused to them they are refused to everyone."""
    request = rf.get('/admin/')
    request.user = superuser

    assert isinstance(model_admin, lockdown.ReadOnlyModelAdminMixin), type(model_admin)
    assert model_admin.has_add_permission(request) is False
    assert model_admin.has_change_permission(request) is False
    assert model_admin.has_delete_permission(request) is False
    assert model_admin.has_view_permission(request) is True
    assert model_admin.has_module_permission(request) is True
    assert 'delete_selected' not in model_admin.get_actions(request)


def test_the_writable_allowlist_reopens_exactly_the_named_model(rf, superuser, settings):
    settings.FROZEN_CONSOLE_WRITABLE_MODELS = ['driver.vehicletype']
    request = rf.get('/admin/')
    request.user = superuser
    registry = {m._meta.label_lower: ma for m, ma in REGISTRY}

    assert registry['driver.vehicletype'].has_add_permission(request) is True
    assert registry['driver.vehicletype'].has_change_permission(request) is True
    assert registry['driver.driver'].has_add_permission(request) is False
    assert registry['driver.withdrawalrequest'].has_change_permission(request) is False


def test_an_admin_write_is_refused_over_http_while_a_read_is_served(superuser, frozen_console_open):
    """End to end through the gate: the changelist renders for a break-glass
    superuser and a POST to the add form is refused by the admin itself."""
    from servers.driver.models import VehicleType

    client = _session_for(superuser)
    before = VehicleType.objects.count()

    listing = client.get(reverse('admin:driver_vehicletype_changelist'))
    assert listing.status_code == 200

    add_form = client.get(reverse('admin:driver_vehicletype_add'))
    assert add_form.status_code == 403

    posted = client.post(reverse('admin:driver_vehicletype_add'), {'type': 'frozen-test'})
    assert posted.status_code == 403
    assert VehicleType.objects.count() == before
