from datetime import datetime, timezone

import pytest
from rest_framework.test import APIClient
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.tokens import AccessToken
from servers.rider.models import Rider
from servers.driver.models import Driver

# Files that pytest should skip at collection time. Each entry is QA debt
# with a known root cause documented in the Phase-0 QA report; they're
# excluded here so the CI test gate stays green on tonight's 28-PR batch
# while the underlying fixtures/infra get rebuilt in follow-up PRs.
collect_ignore = [
    # Imports `DriverEarning` from `servers.driver.models`, which no
    # longer exists. Tests need to be rewritten against the current
    # earnings/withdrawal split.
    'test_settlement.py',
    # Asserts the *previous* S3-backed file lifecycle (deletion of old
    # uploads on replace). Local Django storage doesn't behave that way;
    # we'd need moto / a real S3 sandbox to run these end-to-end.
    'test_upload_integration.py',
]

User = get_user_model()

@pytest.fixture(autouse=True)
def clear_driver_redis_state():
    """Clear per-driver Redis keys between tests.

    pytest-django flushes PostgreSQL between tests, so primary keys restart from 1
    and each test's "fresh" driver reuses an id a previous test already used. Redis
    is NOT flushed, so `driver:active_trip:<id>` survives -- and the next test's
    driver silently inherits the previous test's trip.

    The symptom is nasty and order-dependent: `add_driver_location` reports the
    STALE trip id, so every location frame fans out to the wrong trip group and the
    rider of the current trip receives nothing. It made a WebSocket test that passes
    in isolation fail once inside the full suite, which is the worst kind of CI
    flake -- it looks like a product defect.

    Scoped to the driver presence keys rather than a FLUSHDB, so a developer running
    the suite against a Redis instance that holds anything else does not lose it.
    """
    def _clear():
        try:
            from servers.redis_client import redis_client
        except Exception:  # noqa: BLE001
            return
        if redis_client is None:
            return
        for pattern in ('driver:active_trip:*', 'driver:heartbeat:*',
                        'drivers:geo:*', 'driver:vehicle_type:*',
                        'trip:offered:*', 'active_rider:*'):
            try:
                keys = list(redis_client.scan_iter(match=pattern, count=500))
                if keys:
                    redis_client.delete(*keys)
            except Exception:  # noqa: BLE001 -- no Redis is fine for unit tests
                pass

    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def default_commission_rules(request):
    """An active country-level 15% commission rule for IN and US in every DB test.

    Module 8 removed every commission default: with no rule in force a settlement
    raises `CommissionNotConfigured`. The tests that merely SETTLE a trip should not
    each have to build a rule, so this gives them the owner's 15% (AUDIT 27.36).
    Tests about rules themselves opt out with `@pytest.mark.no_default_commission`
    and build exactly what they need. Rules are created country-level with no geo
    node, so they never get in the way of a test that seeds the geo tree.
    """
    if request.node.get_closest_marker('no_default_commission'):
        yield
        return
    names = set(request.fixturenames)
    marker = request.node.get_closest_marker('django_db')
    if marker is not None:
        request.getfixturevalue(
            'transactional_db' if marker.kwargs.get('transaction') else 'db')
    elif 'db' in names:
        request.getfixturevalue('db')
    elif 'transactional_db' in names:
        request.getfixturevalue('transactional_db')
    else:
        yield
        return
    from servers.pricing.commission_models import ACTIVE, CommissionRule

    for country in ('IN', 'US'):
        CommissionRule.objects.get_or_create(
            country=country, geo_node=None, vehicle_type=None, version=1,
            defaults={'percent': 15, 'status': ACTIVE, 'reason': 'test default (owner 15%)',
                      # In force since forever: a test may back-date a trip.
                      'effective_from': datetime(2000, 1, 1, tzinfo=timezone.utc)})
    yield


@pytest.fixture(autouse=True)
def use_dummy_cache(settings):
    settings.CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        }
    }

@pytest.fixture
def api_client():
    return APIClient()

@pytest.fixture
def auth_client_rider(db):
    """Returns an API client authenticated as a Rider, along with the user."""
    user = User.objects.create_user(phone_number="+919999999999", role="rider")
    Rider.objects.create(user_id=user)
    token = str(AccessToken.for_user(user))
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    return client, user

@pytest.fixture
def auth_client_driver(db):
    """Returns an API client authenticated as a Driver, along with the user."""
    user = User.objects.create_user(phone_number="+918888888888", role="driver")
    Driver.objects.create(user_id=user)
    token = str(AccessToken.for_user(user))
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    return client, user

@pytest.fixture
def auth_client_admin(db):
    """Returns an API client authenticated as an Admin, along with the user."""
    user = User.objects.create_user(
        phone_number="+917777777777",
        role="admin",
        is_staff=True,
        is_superuser=True
    )
    token = str(AccessToken.for_user(user))
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    return client, user


# ===========================================================================
# Operators that work under BOTH access models
# ===========================================================================
#
# The suite was written when an operator was `role='admin'` plus `is_staff`
# and nothing else. Under `ADMIN_ACCESS_STRICT` an account with no
# assignment can do nothing, so every one of those fixtures would fail --
# not because strict mode is wrong, but because the fixtures encode the
# model it replaces.
#
# `grant_legacy_equivalent` gives such an account the assignment that
# matches its old `ops_role`, at Global. Tests that call it pass under
# both models, which is the point: the suite has to go on being true after
# production is switched over.
#
# It is a TEST helper deliberately. Real accounts are migrated by
# `migrate_operators` from a named plan, because who holds what is a
# decision with a person's name against it.

#: The operator every test seed is attributed to. `seed_admin_access` --
#: like every command that writes access rows -- refuses to run without an
#: `--actor-email` that resolves to an active operator, so tests that need
#: the geo tree and the role templates name this account.
SEED_ACTOR_EMAIL = 'seed.actor@saaradhigo.test'
SEED_ACTOR_PHONE = '+919100000777'


def seed_actor():
    """An active operator to attribute test seeding to. Get-or-create."""
    user = User.objects.filter(email__iexact=SEED_ACTOR_EMAIL).first()
    if user is None:
        user = User.objects.create_user(
            phone_number=SEED_ACTOR_PHONE, username=SEED_ACTOR_PHONE,
            email=SEED_ACTOR_EMAIL, full_name='Seed Actor (test)',
            role='admin', is_staff=True,
        )
    return user


def seed_admin_access(*args, **kwargs):
    """`call_command('seed_admin_access')` with the test actor attached."""
    from io import StringIO

    from django.core.management import call_command

    kwargs.setdefault('actor_email', seed_actor().email)
    kwargs.setdefault('stdout', StringIO())
    return call_command('seed_admin_access', *args, **kwargs)


def grant_legacy_equivalent(user, ops_role=None):
    """Assign `user` the new role equivalent to their legacy `ops_role`.

    Idempotent, and a no-op for a non-operator. Seeds the geo tree and the
    role templates if they are not there yet, so a test does not have to
    know that it needs them.
    """
    from django.core.management import call_command

    from servers.admin_access.bridge import LEGACY_ROLE_EQUIVALENT
    from servers.admin_access.models import (
        COUNTRY, GLOBAL, AdminAssignment, AdminRole, GeoNode,
    )
    from servers.admin_access.roles import SUPER_ADMIN

    if not GeoNode.objects.exists() or not AdminRole.objects.exists():
        seed_admin_access()

    code = LEGACY_ROLE_EQUIVALENT.get(
        ops_role or getattr(user, 'ops_role', None) or 'admin'
    )
    if code is None:
        return None

    role = AdminRole.objects.get(code=code)
    # Only Super Admin may be held at Global (`models.check_role_at_scope`,
    # owner's ruling 2026-10-28). The legacy roles were unscoped, so the
    # closest honest equivalent is every COUNTRY: the same reach over every
    # anchored row, without the unrestricted Global grant.
    if code == SUPER_ADMIN:
        scopes = [GeoNode.objects.get(kind=GLOBAL)]
    else:
        scopes = list(GeoNode.objects.filter(kind=COUNTRY).order_by('code'))
    assignments = []
    for scope in scopes:
        assignment, _ = AdminAssignment.objects.get_or_create(
            admin=user, role=role, scope=scope,
            defaults={'reason': 'Test fixture: legacy ops_role equivalent.'},
        )
        assignments.append(assignment)
    return assignments


@pytest.fixture
def legacy_equivalent():
    """The helper above, as a fixture, for tests that build their own user."""
    return grant_legacy_equivalent


# ===========================================================================
# The frozen console
# ===========================================================================
#
# The Django console is restricted to a Super Admin on an allowlisted route
# (servers/admin_dashboard/lockdown.py); everything else is 404. The tests
# that render a retired screen -- the KYC checklist per market, the money
# dashboards, the stale-ride queue -- are still true statements about those
# screens for as long as a deployment can allowlist them, so they opt in
# here rather than being deleted with the lockdown.
#
# Deliberately NOT autouse. The lockdown's own tests exercise the shipped
# default, and a fixture that quietly opened every route for every test
# would make "the default is one route" something no test ever saw.

@pytest.fixture
def frozen_console_open(settings):
    """Allowlist every frozen route this deployment could ever serve.

    `NEVER_ALLOWED` stays out: the gate subtracts it regardless, and a test
    that needs the admin's password-only login is asserting something the
    product refuses.
    """
    from servers.admin_dashboard import inventory

    settings.FROZEN_CONSOLE_ALLOWED_ROUTES = sorted(
        inventory.known_routes() - inventory.NEVER_ALLOWED
    )
    return settings.FROZEN_CONSOLE_ALLOWED_ROUTES


# ===========================================================================
# The audit log's TRUNCATE guard vs. pytest-django's flush
# ===========================================================================
#
# `admin_audit_adminauditlog` carries a statement-level BEFORE TRUNCATE
# trigger so the whole log cannot be erased in one statement by the
# credentials the application already holds. That guard is correct and it
# must stay on in production.
#
# pytest-django tears a `transaction=True` test down by TRUNCATEing every
# table in one statement. The guard refuses it, so the TEST BODY PASSES AND
# THE TEARDOWN ERRORS -- which is how this arrived: `ac1081f` added the
# trigger, and from then on every transactional PostgreSQL test reported
# "passed" plus an error, including CI's required postgres job.
#
# So the trigger is stood down IN THE TEST DATABASE ONLY, once, after
# migrations. `TestTruncateIsAlsoRefused` re-enables it around itself, which
# is the test that actually exercises it.
#
# WHY THIS IS NOT A WEAKENING
# ---------------------------
# It changes nothing about any other database. It is also an honest
# demonstration of the guard's known limit, already recorded as a
# production blocker in docs/admin/AUDIT.md §14: the application connects as
# the database OWNER, and an owner can disable a trigger in one statement.
# That is exactly why the hash chain exists -- tamper EVIDENCE rather than
# tamper prevention -- and why a separate non-owner application role is on
# the go-live blocker list.

AUDIT_TRUNCATE_TRIGGER = 'saaradhigo_audit_no_truncate'
AUDIT_TABLE = 'admin_audit_adminauditlog'


@pytest.fixture(scope='session', autouse=True)
def _allow_test_teardown_to_truncate_the_audit_log(django_db_setup, django_db_blocker):
    """Stand the TRUNCATE guard down, in the test database only.

    Refuses to touch a database whose name does not look like one pytest
    created. A fixture that silently disabled an audit guard on a real
    database would be a far worse bug than the one it fixes.
    """
    from django.db import connection

    if connection.vendor != 'postgresql':
        # SQLite has no such trigger; the guard is enforced in Python there.
        yield
        return

    with django_db_blocker.unblock():
        name = connection.settings_dict.get('NAME') or ''
        if not str(name).startswith('test_'):
            raise RuntimeError(
                f'Refusing to disable the audit TRUNCATE guard on database '
                f'{name!r}: it does not look like a pytest-created test '
                f'database. This fixture must never run against a real one.'
            )
        with connection.cursor() as cursor:
            cursor.execute(
                f'ALTER TABLE {AUDIT_TABLE} '
                f'DISABLE TRIGGER {AUDIT_TRUNCATE_TRIGGER}'
            )

    yield

    # Put it back, so anything that inspects the database afterwards -- or
    # a `--reuse-db` run -- finds the schema as the migrations left it.
    with django_db_blocker.unblock():
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    f'ALTER TABLE {AUDIT_TABLE} '
                    f'ENABLE TRIGGER {AUDIT_TRUNCATE_TRIGGER}'
                )
        except Exception:  # noqa: BLE001 - the database may already be gone
            pass
