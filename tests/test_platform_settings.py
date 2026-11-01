"""The Platform Settings key/value editor, and the retirement of the Django routes (module 15)."""

import ast
import pathlib

import pytest
from django.contrib import admin
from django.test import Client
from django.urls import NoReverseMatch, reverse

from servers.admin_audit.models import AdminAuditLog
from servers.approvals.models import ApprovalRequest
from servers.pricing.models import PlatformSettings
from servers.platform_ops import settings_editor
from test_sos_support_zones_matrix import geo, operator  # noqa: F401 - fixtures
from platform_support import (
    BASE, approve, data, error_code, post, put, two_super_admins,
)

pytestmark = pytest.mark.django_db
URL = f'{BASE}/settings/'


@pytest.fixture(autouse=True)
def policies(db):
    from io import StringIO

    from django.core.management import call_command

    from conftest import seed_actor

    call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=StringIO())


def create(client, key='TEST_LIMIT', value='5', setting_type='integer', **extra):
    return post(client, URL, {'key': key, 'value': value, 'setting_type': setting_type,
                              'description': 'a test setting', 'reason': 'adding a test setting',
                              **extra})


class TestValidation:
    @pytest.mark.parametrize('setting_type,value,stored', [
        ('integer', '42', '42'), ('integer', 7, '7'), ('decimal', '12.50', '12.50'),
        ('boolean', 'yes', 'true'), ('boolean', False, 'false'),
        ('json', '{"b": 1, "a": [1, 2]}', '{"a": [1, 2], "b": 1}'), ('string', ' hello ', 'hello'),
    ])
    def test_a_valid_value_is_stored_in_its_canonical_form(self, geo, setting_type, value, stored):
        (maker, _), _ = two_super_admins(geo)
        response = create(maker, value=value, setting_type=setting_type)
        assert response.status_code == 200, response.content
        assert PlatformSettings.objects.get(key='TEST_LIMIT').value == stored

    @pytest.mark.parametrize('setting_type,value', [
        ('integer', '4.5'), ('integer', 'abc'), ('decimal', 'NaN'), ('decimal', 'x'),
        ('boolean', 'maybe'), ('json', '{broken'), ('string', 'x' * 2001), ('color', 'red'),
    ])
    def test_an_invalid_value_is_refused_and_nothing_is_stored(self, geo, setting_type, value):
        (maker, _), _ = two_super_admins(geo)
        response = create(maker, value=value, setting_type=setting_type)
        assert response.status_code == 400
        assert error_code(response) in ('INVALID_VALUE', 'INVALID_TYPE')
        assert not PlatformSettings.objects.filter(key='TEST_LIMIT').exists()

    @pytest.mark.parametrize('key', ['lower_case', '1START', 'HAS SPACE', 'AB', 'X' * 129])
    def test_a_bad_key_is_refused(self, geo, key):
        (maker, _), _ = two_super_admins(geo)
        assert error_code(create(maker, key=key)) == 'INVALID_KEY'


class TestAuditedWrites:
    def test_a_change_is_audited_with_before_and_after_and_listed_with_who_and_when(self, geo):
        (maker, user), _ = two_super_admins(geo)
        create(maker)
        response = put(maker, f'{URL}TEST_LIMIT/', {'value': '9', 'reason': 'raise the limit'})
        assert response.status_code == 200 and data(response)['value'] == '9'

        row = AdminAuditLog.objects.filter(action='platform_setting_changed').latest('id')
        assert row.before['value'] == '5' and row.after['value'] == '9'
        assert row.reason == 'raise the limit' and row.actor_id == user.id
        listed = {r['key']: r for r in data(maker.get(URL))['results']}['TEST_LIMIT']
        assert listed['updated_by'] and listed['updated_at'] and listed['controls'] == []
        assert listed['editable'] is True

    def test_an_unchanged_value_writes_no_audit_row(self, geo):
        (maker, _), _ = two_super_admins(geo)
        create(maker)
        before = AdminAuditLog.objects.filter(action='platform_setting_changed').count()
        put(maker, f'{URL}TEST_LIMIT/', {'value': '5', 'reason': 'the same again'})
        assert AdminAuditLog.objects.filter(action='platform_setting_changed').count() == before


class TestSensitiveKeys:
    def test_a_sensitive_key_changes_only_through_a_checker_and_is_never_shown(self, geo):
        (maker, _), (checker, _) = two_super_admins(geo)
        response = create(maker, key='PARTNER_API_SECRET', value='zzzzzzzzzzzz-secret',
                          setting_type='string')
        assert response.status_code == 202, response.content
        assert not PlatformSettings.objects.filter(key='PARTNER_API_SECRET').exists()
        rid = data(response)['approval_request']['id']
        assert approve(maker, rid).status_code == 403

        # Neither the approvals list nor the platform list shows the value to anybody.
        listing = maker.get('/api/v1/approvals/admin/requests/').content.decode()
        assert 'zzzzzzzzzzzz-secret' not in listing
        assert approve(checker, rid).status_code == 200

        row = PlatformSettings.objects.get(key='PARTNER_API_SECRET')
        assert row.is_sensitive and row.value == 'zzzzzzzzzzzz-secret'
        text = maker.get(URL).content.decode()
        assert 'zzzzzzzzzzzz-secret' not in text and settings_editor.MASK in text
        for entry in AdminAuditLog.objects.filter(target_id='PARTNER_API_SECRET'):
            assert 'zzzzzzzzzzzz-secret' not in str(entry.before) + str(entry.after) + entry.reason

    def test_the_name_alone_makes_a_key_sensitive(self, geo):
        (maker, _), _ = two_super_admins(geo)
        for key in ('MY_TOKEN', 'DB_PASSWORD', 'SIGNING_KEY', 'VENDOR_CREDENTIAL'):
            assert create(maker, key=key, value='x', setting_type='string').status_code == 202

    def test_a_sensitive_key_cannot_be_quietly_made_visible(self, geo):
        (maker, _), (checker, _) = two_super_admins(geo)
        rid = data(create(maker, key='VENDOR_SECRET', value='x', setting_type='string'
                          ))['approval_request']['id']
        approve(checker, rid)
        response = put(maker, f'{URL}VENDOR_SECRET/', {'value': 'y', 'is_sensitive': False,
                                                       'reason': 'make it visible'})
        assert response.status_code == 202            # still a checker, still sensitive
        assert PlatformSettings.objects.get(key='VENDOR_SECRET').is_sensitive is True


class TestDeprecatedKeys:
    def test_the_commission_key_is_read_only_and_points_at_pricing(self, geo):
        (maker, _), _ = two_super_admins(geo)
        PlatformSettings.objects.create(key='PLATFORM_COMMISSION_PERCENT', value='18',
                                        setting_type='decimal')
        row = {r['key']: r for r in data(maker.get(URL))['results']}['PLATFORM_COMMISSION_PERCENT']
        assert row['deprecated'] is True and row['editable'] is False
        assert row['deprecated_href'] == '/pricing' and 'versioned' in row['deprecated_note']

        response = put(maker, f'{URL}PLATFORM_COMMISSION_PERCENT/', {'value': '44',
                                                                     'reason': 'set it please'})
        assert response.status_code == 409 and error_code(response) == 'DEPRECATED_KEY'
        assert PlatformSettings.objects.get(key='PLATFORM_COMMISSION_PERCENT').value == '18'


class TestWhatKeysControl:
    def test_the_reader_has_no_caller_so_no_key_claims_a_control(self):
        """`READERS` must name every key the code reads. Today the one reader has no caller; when
        one appears this fails until READERS says what it controls."""
        root = pathlib.Path(__file__).resolve().parents[1]
        callers = []
        for top in ('servers', 'base'):
            for path in (root / top).rglob('*.py'):
                if 'migrations' in path.parts or 'tests' in path.parts:
                    continue
                tree = ast.parse(path.read_text(encoding='utf-8'))
                for node in ast.walk(tree):
                    if (isinstance(node, ast.Call) and getattr(node.func, 'id', '') ==
                            'get_platform_setting'):
                        callers.append((path.relative_to(root).as_posix(), node.lineno))
        assert callers == [] or settings_editor.READERS, (
            'get_platform_setting now has a caller; record what the key controls in '
            f'settings_editor.READERS: {callers}')
        assert settings_editor.READERS == {} or callers


class TestPermissions:
    def test_only_platform_settings_may_read_or_write(self, geo):
        for role in ('city_ops_manager', 'finance', 'marketing', 'analyst', 'support_agent',
                     'support_lead', 'safety_officer', 'kyc_verifier'):
            client, _ = operator(role, geo)
            assert client.get(URL).status_code == 403, role
            assert put(client, f'{URL}TEST_LIMIT/', {'value': '1', 'reason': 'trying it'}
                       ).status_code == 403, role


class TestDjangoSurfaceRetired:
    def test_the_global_config_route_is_gone_and_404s_even_for_a_super_admin(self, geo):
        client, user = operator('super_admin', geo)
        django_client = Client()
        django_client.force_login(user)
        assert django_client.get('/api/global-config/?zone_id=1').status_code == 404
        assert django_client.post('/api/global-config/', {}).status_code == 404
        with pytest.raises(NoReverseMatch):
            reverse('update_global_config')

    def test_it_is_not_in_the_inventory_and_cannot_be_allowlisted(self):
        from servers.admin_dashboard import inventory

        assert 'update_global_config' not in inventory.known_routes()
        assert inventory.configuration_faults(['update_global_config'])
        assert 'admin:pricing_platformsettings_changelist' not in inventory.known_routes()

    def test_the_platform_settings_model_admin_is_unregistered(self):
        assert PlatformSettings not in admin.site._registry
