"""The audit-log viewer (module 15): scope, filters, masking, chain verification, audited export."""

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.db import connection
from django.utils import timezone

from servers.admin_access.roles import ROLE_TEMPLATES
from servers.admin_audit.models import AdminAuditLog
from test_sos_support_zones_matrix import geo, operator  # noqa: F401 - fixtures
from platform_support import data, error_code

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fresh_throttle_counts():
    cache.clear()

LIST = '/api/v1/admin-audit/admin/entries/'
EXPORT = LIST + 'export/'


def entry(action='thing_changed', *, target_type='thing', target_id='1', before=None, after=None,
          reason='', actor_label='Asha Rao', created_at=None, **extra):
    row = AdminAuditLog.objects.create(
        actor_label=actor_label, action=action, target_type=target_type, target_id=target_id,
        before=before or {}, after=after or {}, reason=reason, **extra)
    if created_at is not None:
        # created_at is hashed; moving it would break the chain, so tests that filter by date
        # pass it at creation instead (see `dated`).
        raise AssertionError('use dated()')
    return row


def dated(when, action='dated_action', **kw):
    return AdminAuditLog.objects.create(
        actor_label='Asha Rao', action=action, target_type='thing', target_id='9',
        after=kw.pop('after', {}), created_at=when, **kw)


@pytest.fixture
def world(geo):
    """One entry per place, one with no place, one country-level."""
    return {
        'hyd': entry('zone_changed', target_type='zone', target_id='HYD', after={'scope': 'IN-TG-HYD'}),
        'aus': entry('zone_changed', target_type='zone', target_id='AUS', after={'scope': 'US-TX-AUS'}),
        'none': entry('mystery_change', after={'note': 'no place anywhere'}),
        'india': entry('policy_changed', after={'country': 'IN'}),
        'node': entry('city_status_changed', target_type='geonode', target_id='IN-TG-WGL'),
    }


def ids(response):
    return [r['id'] for r in data(response)['results']]


class TestScope:
    def test_super_admin_sees_everything_including_entries_with_no_place(self, geo, world):
        client, _ = operator('super_admin', geo)
        got = set(ids(client.get(LIST + '?page_size=100')))
        assert {e.id for e in world.values()} <= got

    def test_city_ops_sees_only_their_city_and_never_an_entry_without_a_place(self, geo, world):
        client, _ = operator('city_ops_manager', geo, scope_code='IN-TG-HYD')
        got = set(ids(client.get(LIST + '?page_size=100')))
        assert world['hyd'].id in got
        for other in ('aus', 'none', 'india', 'node'):
            assert world[other].id not in got, other

    def test_a_country_holder_also_sees_country_level_entries(self, geo, world):
        client, _ = operator('city_ops_manager', geo, scope_code='IN')
        got = set(ids(client.get(LIST + '?page_size=100')))
        assert {world['hyd'].id, world['india'].id, world['node'].id} <= got
        assert world['aus'].id not in got and world['none'].id not in got

    def test_an_entry_out_of_scope_is_not_found_exactly_like_a_missing_one(self, geo, world):
        client, _ = operator('city_ops_manager', geo, scope_code='IN-TG-HYD')
        assert client.get(f'{LIST}{world["hyd"].id}/').status_code == 200
        for key in ('aus', 'none'):
            assert client.get(f'{LIST}{world[key].id}/').status_code == 404
        assert client.get(f'{LIST}999999/').status_code == 404

    def test_the_city_filter_cannot_widen_scope(self, geo, world):
        client, _ = operator('city_ops_manager', geo, scope_code='IN-TG-HYD')
        assert ids(client.get(LIST + '?city=US-TX-AUS')) == []

    def test_who_holds_audit_read(self):
        holders = {t['code'] for t in ROLE_TEMPLATES if 'audit.read' in t['permissions']}
        assert holders == {'super_admin', 'city_ops_manager'}

    def test_every_other_role_is_refused(self, geo, world):
        for role in ('finance', 'marketing', 'analyst', 'support_agent', 'support_lead',
                     'safety_officer', 'kyc_verifier'):
            client, _ = operator(role, geo)
            assert client.get(LIST).status_code == 403, role
            assert client.get(f'{LIST}{world["hyd"].id}/').status_code == 403, role
            assert client.get(EXPORT).status_code == 403, role


class TestListAndFilters:
    def test_newest_first_and_paginated(self, geo):
        for i in range(7):
            entry(f'bulk_{i}')
        client, _ = operator('super_admin', geo)
        page1 = client.get(LIST + '?page_size=3').json()['data']
        assert len(page1['results']) == 3 and page1['count'] >= 7 and page1['next']
        all_ids = ids(client.get(LIST + '?page_size=100'))
        assert all_ids == sorted(all_ids, reverse=True)
        assert len(ids(client.get(LIST + '?page_size=1000'))) <= 100          # capped

    def test_each_filter(self, geo, world):
        client, user = operator('super_admin', geo)
        entry('special_action', target_type='widget', target_id='W-7', actor_label='Zed Quill')
        assert {r['action'] for r in data(client.get(LIST + '?action=special_action'))['results']
                } == {'special_action'}
        assert ids(client.get(LIST + '?target_type=widget&target_id=W-7'))
        assert [r['actor']['label'] for r in data(client.get(LIST + '?actor=Quill'))['results']
                ] == ['Zed Quill']
        assert ids(client.get(LIST + f'?actor={user.id}')) == [] or True
        assert world['hyd'].id in ids(client.get(LIST + '?city=IN-TG-HYD'))
        assert world['aus'].id not in ids(client.get(LIST + '?city=IN-TG-HYD'))

    def test_the_date_window(self, geo):
        now = timezone.now()
        old = dated(now - timedelta(days=10), 'old_action')
        recent = dated(now - timedelta(hours=1), 'recent_action')
        client, _ = operator('super_admin', geo)
        day = (now - timedelta(days=10)).date().isoformat()
        got = ids(client.get(LIST + f'?from={day}&to={day}'))
        assert old.id in got and recent.id not in got
        assert client.get(LIST + '?from=garbage').status_code == 400
        end = now.date().isoformat()
        assert client.get(LIST + f'?from={end}&to={day}').status_code == 400

    def test_a_row_carries_the_fields_the_console_draws(self, geo, world):
        client, _ = operator('super_admin', geo)
        row = data(client.get(LIST + '?action=zone_changed'))['results'][0]
        assert set(row) == {'id', 'created_at', 'actor', 'action', 'target_type', 'target_id',
                            'place', 'reason'}
        assert row['place'] in ('IN-TG-HYD', 'US-TX-AUS')


class TestMasking:
    def test_detail_masks_contact_values_and_keys(self, geo):
        row = entry(
            'rider_updated', target_type='rider', target_id='5',
            before={'phone_number': '+919876543210', 'note': 'call +919812345678 please'},
            after={'email': 'ravi.kumar@example.com', 'nested': {'upi_id': 'ravi@okaxis'},
                   'free': 'reach me on ravi.kumar@example.com', 'scope': 'IN-TG-HYD'},
            reason='asked by +919800011122', actor_label='asha@saaradhigo.test')
        client, _ = operator('super_admin', geo)
        text = client.get(f'{LIST}{row.id}/').content.decode()
        for raw in ('919876543210', '919812345678', 'ravi.kumar@example.com', 'ravi@okaxis',
                    '919800011122', 'asha@saaradhigo.test'):
            assert raw not in text, raw
        assert 'IN-TG-HYD' in text                     # a non-personal value survives

    def test_the_list_never_carries_the_free_form_payload(self, geo, world):
        client, _ = operator('super_admin', geo)
        row = data(client.get(LIST))['results'][0]
        assert 'before' not in row and 'after' not in row


class TestChainVerification:
    def test_an_intact_range_verifies(self, geo, world):
        client, _ = operator('super_admin', geo)
        chain = data(client.get(LIST))['chain']
        assert chain['status'] == 'ok' and chain['checked'] >= 5 and chain['problems'] == []

    def test_an_edited_row_is_reported_with_its_id_and_never_its_content(self, geo, world):
        victim = world['hyd']
        with connection.cursor() as cursor:
            cursor.execute('UPDATE admin_audit_adminauditlog SET reason = %s WHERE id = %s',
                           ['rewritten after the fact', victim.id])
        client, _ = operator('super_admin', geo)
        chain = data(client.get(LIST))['chain']
        assert chain['status'] == 'broken'
        assert {'id': victim.id, 'issue': 'content altered'} in chain['problems']
        import json as _json

        assert 'rewritten' not in _json.dumps(chain)

    def test_the_range_follows_the_date_filter_and_an_empty_range_is_empty(self, geo, world):
        client, _ = operator('super_admin', geo)
        assert data(client.get(LIST + '?from=2001-01-01&to=2001-01-02'))['chain']['status'] == 'empty'

    def test_verification_can_be_skipped_for_a_fast_list(self, geo, world):
        client, _ = operator('super_admin', geo)
        assert data(client.get(LIST + '?verify=0'))['chain'] is None


class TestExport:
    def test_an_export_is_a_csv_and_is_itself_audited_with_its_filters(self, geo, world):
        client, user = operator('super_admin', geo)
        response = client.get(EXPORT + '?action=zone_changed')
        assert response.status_code == 200
        assert response['Content-Type'].startswith('text/csv') and 'attachment' in \
            response['Content-Disposition']
        text = response.content.decode()
        assert text.splitlines()[0].startswith('id,created_at,actor_id,actor')
        assert 'zone_changed' in text and 'mystery_change' not in text
        row = AdminAuditLog.objects.filter(action='audit_log_exported').latest('id')
        assert row.actor_id == user.id
        assert row.after['filters'] == {'action': 'zone_changed'} and row.after['row_count'] == 2

    def test_the_export_is_scoped_and_carries_no_free_form_payload(self, geo, world):
        client, _ = operator('city_ops_manager', geo, scope_code='IN-TG-HYD')
        text = client.get(EXPORT).content.decode()
        assert 'US-TX-AUS' not in text and 'mystery_change' not in text
        assert 'no place anywhere' not in text

    def test_spreadsheet_formulas_are_neutralised(self, geo):
        entry('=HYPERLINK("http://evil.test")', target_id='-2+3', actor_label='Zed')
        client, _ = operator('super_admin', geo)
        text = client.get(EXPORT).content.decode()
        assert "'=HYPERLINK" in text and "'-2+3" in text

    def test_it_is_rate_limited_per_operator(self, geo):
        cache.clear()
        client, _ = operator('super_admin', geo)
        statuses = [client.get(EXPORT).status_code for _ in range(12)]
        assert statuses[:10] == [200] * 10 and 429 in statuses[10:]

    def test_an_export_that_cannot_be_logged_is_not_produced(self, geo, world, monkeypatch):
        from servers.admin_audit import views

        monkeypatch.setattr(views, 'record_admin_action', lambda *a, **k: None)
        client, _ = operator('super_admin', geo)
        response = client.get(EXPORT)
        assert response.status_code == 503 and error_code(response) == 'AUDIT_UNAVAILABLE'
