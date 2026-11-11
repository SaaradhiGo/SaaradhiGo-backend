"""Who may call each new endpoint (module 15), and the seeded approval policies.

The matrix is data: one row per endpoint, the roles that may call it. Every other role must get 403.
Out of scope is 404 (covered per module); here the door is the capability.
"""

from io import StringIO

import pytest
from django.core.management import call_command

from servers.admin_access.roles import ALL_PERMISSIONS, ROLE_TEMPLATES
from servers.approvals.actions import ACTIONS
from servers.approvals.models import ApprovalPolicy
from servers.approvals.management.commands.seed_approval_policies import (
    INDIA_DEFAULTS, US_DEFAULTS,
)
from test_sos_support_zones_matrix import geo, operator  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db

BASE = '/api/v1/platform/admin'
SA, OPS = 'super_admin', 'city_ops_manager'
ROLES = [t['code'] for t in ROLE_TEMPLATES]

#: (method, path, body, roles allowed through the door)
MATRIX = [
    ('get', f'{BASE}/markets/', None, {SA, OPS}),
    ('get', f'{BASE}/markets/IN-TG-HYD/readiness/', None, {SA, OPS}),
    ('post', f'{BASE}/markets/IN-TG-HYD/status/', {'status': 'paused', 'reason': 'x' * 8},
     {SA, OPS}),
    ('post', f'{BASE}/markets/nodes/', {}, {SA}),
    ('get', f'{BASE}/flags/', None, {SA, OPS}),
    ('post', f'{BASE}/flags/', {}, {SA}),
    ('get', f'{BASE}/flags/none.such/', None, {SA, OPS}),
    ('put', f'{BASE}/flags/none.such/', {}, {SA}),
    ('get', f'{BASE}/app-versions/', None, {SA, OPS}),
    ('put', f'{BASE}/app-versions/rider/android/', {}, {SA}),
    ('get', f'{BASE}/notices/', None, {SA, OPS}),
    ('post', f'{BASE}/notices/', {}, {SA}),
    ('put', f'{BASE}/notices/1/', {}, {SA}),
    ('post', f'{BASE}/notices/1/cancel/', {}, {SA}),
    ('get', f'{BASE}/settings/', None, {SA}),
    ('post', f'{BASE}/settings/', {}, {SA}),
    ('put', f'{BASE}/settings/SOME_KEY/', {}, {SA}),
    ('get', f'{BASE}/integrations/', None, {SA}),
    ('get', f'{BASE}/options/', None, {SA, OPS}),
    ('get', '/api/v1/admin-audit/admin/entries/', None, {SA, OPS}),
    ('get', '/api/v1/admin-audit/admin/entries/1/', None, {SA, OPS}),
    ('get', '/api/v1/admin-audit/admin/entries/export/', None, {SA, OPS}),
]


def call(client, method, path, body):
    fn = getattr(client, method)
    return fn(path) if body is None else fn(path, body, format='json')


@pytest.mark.parametrize('role', ROLES)
def test_the_door_for_every_endpoint_and_every_role(geo, role):
    client, _ = operator(role, geo)
    wrong = []
    for method, path, body, allowed in MATRIX:
        got = call(client, method, path, body).status_code
        if role in allowed and got == 403:
            wrong.append((method, path, 'refused', got))
        if role not in allowed and got != 403:
            wrong.append((method, path, 'let in', got))
    assert wrong == []


def test_city_ops_cannot_write_flags_notices_or_launch(geo):
    client, _ = operator(OPS, geo)
    from servers.platform_ops.models import FeatureFlag

    FeatureFlag.objects.create(key='door.flag', description='x')
    assert client.put(f'{BASE}/flags/door.flag/', {'default_enabled': True, 'reason': 'let me'},
                      format='json').status_code == 403
    assert client.post(f'{BASE}/notices/', {'message': 'x'}, format='json').status_code == 403
    assert client.post(f'{BASE}/markets/IN-TG-HYD/status/',
                       {'status': 'live', 'reason': 'launch it'}, format='json'
                       ).status_code in (403, 409)


def test_the_new_capabilities_exist_and_super_admin_holds_them_all():
    new = {'platform.read', 'platform.city_pause', 'platform.integrations.read', 'audit.read',
           'platform.settings'}
    assert new <= ALL_PERMISSIONS
    by_role = {t['code']: set(t['permissions']) for t in ROLE_TEMPLATES}
    assert new <= by_role[SA]
    assert {p for p in new if p in by_role[OPS]} == {'platform.read', 'platform.city_pause',
                                                      'audit.read'}
    for role in ROLES:
        if role not in (SA, OPS):
            assert not (new & by_role[role]), role


class TestApprovalPolicies:
    ACTIONS_15 = ('platform.city_status', 'platform.flag_change', 'platform.app_version',
                  'platform.setting_change')

    def seed(self):
        from conftest import seed_actor

        call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=StringIO())

    def test_the_four_actions_are_seeded_for_both_countries_and_always_need_a_checker(self, geo):
        self.seed()
        for action in self.ACTIONS_15:
            assert action in ACTIONS
            for country in ('IN', 'US'):
                policy = ApprovalPolicy.objects.get(country=country, action_key=action,
                                                    is_active=True)
                assert policy.limit_kind == 'always'

    def test_the_count_and_idempotency(self, geo):
        self.seed()
        total = ApprovalPolicy.objects.filter(is_active=True).count()
        assert total == len(INDIA_DEFAULTS) + len(US_DEFAULTS)
        self.seed()
        assert ApprovalPolicy.objects.filter(is_active=True).count() == total
        assert ApprovalPolicy.objects.filter(
            is_active=True, action_key__in=self.ACTIONS_15).count() == 8

    def test_every_action_with_a_policy_row_has_one_in_each_country(self, geo):
        self.seed()
        for action in ACTIONS.values():
            if action.always_checker:
                continue
            for country in ('IN', 'US'):
                assert ApprovalPolicy.objects.filter(country=country, action_key=action.key,
                                                     is_active=True).exists(), (country, action.key)
