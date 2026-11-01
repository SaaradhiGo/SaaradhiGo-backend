"""App version policy and maintenance notices (module 15)."""

from datetime import timedelta

import pytest
from django.utils import timezone

from servers.admin_audit.models import AdminAuditLog
from servers.platform_ops import notices, versions
from servers.platform_ops.models import AppVersionPolicy, MaintenanceNotice
from test_sos_support_zones_matrix import geo, operator  # noqa: F401 - fixtures
from platform_support import (
    BASE, approve, data, error_code, post, put, two_super_admins,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def policies(db):
    from io import StringIO

    from django.core.management import call_command

    from conftest import seed_actor

    call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=StringIO())


def slot(app='rider', platform='android'):
    return f'{BASE}/app-versions/{app}/{platform}/'


class TestVersionParsing:
    @pytest.mark.parametrize('text,parsed', [
        ('1.4.2', (1, 4, 2, 0)), ('1.4', (1, 4, 0, 0)), ('2', (2, 0, 0, 0)),
        ('1.4.2+17', (1, 4, 2, 0)), ('1.4.2-beta', (1, 4, 2, 0)), ('10.0.0', (10, 0, 0, 0)),
    ])
    def test_it_reads_a_version(self, text, parsed):
        assert versions.parse_version(text) == parsed

    @pytest.mark.parametrize('text', ['', None, 'abc', '1.x', '1..2', 'v1.2'])
    def test_it_refuses_a_non_version(self, text):
        assert versions.parse_version(text) is None

    def test_it_compares_numerically_not_as_text(self):
        assert versions.parse_version('1.10.0') > versions.parse_version('1.9.0')


class TestPolicyChange:
    def test_lowering_or_setting_the_first_minimum_zero_and_moving_latest_is_immediate(self, geo):
        (maker, _), _ = two_super_admins(geo)
        response = put(maker, slot(), {
            'minimum_version': '0.0.0', 'latest_version': '1.5.0',
            'store_url': 'https://play.test/rider', 'message': 'New version out',
            'reason': 'publish the latest version'})
        assert response.status_code == 200, response.content
        assert data(response)['latest_version'] == '1.5.0' and data(response)['state'] == 'applied'
        row = AdminAuditLog.objects.filter(action='app_version_policy_changed').latest('id')
        assert row.after['latest_version'] == '1.5.0' and row.target_id == 'rider:android'

    def test_raising_the_minimum_needs_a_checker(self, geo):
        (maker, _), (checker, _) = two_super_admins(geo)
        put(maker, slot(), {'minimum_version': '0.0.0', 'latest_version': '1.5.0',
                            'reason': 'publish the latest version'})

        response = put(maker, slot(), {'minimum_version': '1.4.0', 'reason': 'force the update'})

        assert response.status_code == 202, response.content
        assert AppVersionPolicy.objects.get(app='rider', platform='android'
                                            ).minimum_version == '0.0.0'
        rid = data(response)['approval_request']['id']
        assert approve(maker, rid).status_code == 403
        assert approve(checker, rid).status_code == 200
        assert AppVersionPolicy.objects.get(app='rider', platform='android'
                                            ).minimum_version == '1.4.0'

    def test_lowering_the_minimum_again_is_immediate(self, geo):
        (maker, _), (checker, _) = two_super_admins(geo)
        AppVersionPolicy.objects.create(app='rider', platform='android', minimum_version='2.0.0',
                                        latest_version='2.0.0')
        response = put(maker, slot(), {'minimum_version': '1.0.0', 'reason': 'roll it back'})
        assert response.status_code == 200 and data(response)['minimum_version'] == '1.0.0'

    @pytest.mark.parametrize('body,code', [
        ({'minimum_version': '2.0.0', 'latest_version': '1.0.0'}, 'LATEST_BELOW_MINIMUM'),
        ({'minimum_version': 'two'}, 'INVALID_VERSION'),
        ({'latest_version': '1.0.0', 'store_url': 'http://insecure.test'}, 'INVALID_URL'),
        ({'latest_version': '1.0.0', 'message': 'x' * 301}, 'TOO_LONG'),
    ])
    def test_bad_input_is_refused(self, geo, body, code):
        (maker, _), _ = two_super_admins(geo)
        response = put(maker, slot(), {**body, 'reason': 'trying it'})
        assert response.status_code == 400 and error_code(response) == code

    def test_unknown_app_or_platform(self, geo):
        (maker, _), _ = two_super_admins(geo)
        assert put(maker, slot('console'), {'latest_version': '1.0.0', 'reason': 'nope!'}
                   ).status_code == 400
        assert put(maker, slot('rider', 'windows'), {'latest_version': '1.0.0', 'reason': 'nope!'}
                   ).status_code == 400

    def test_only_platform_settings_may_change_it_and_city_ops_may_read(self, geo):
        ops, _ = operator('city_ops_manager', geo)
        assert ops.get(f'{BASE}/app-versions/').status_code == 200
        assert put(ops, slot(), {'latest_version': '1.0.0', 'reason': 'let me in'}
                   ).status_code == 403

    def test_the_table_always_shows_all_four_slots(self, geo):
        (maker, _), _ = two_super_admins(geo)
        rows = data(maker.get(f'{BASE}/app-versions/'))['results']
        assert {(r['app'], r['platform']) for r in rows} == {
            ('rider', 'android'), ('rider', 'ios'), ('driver', 'android'), ('driver', 'ios')}


class TestNotices:
    def body(self, **extra):
        now = timezone.now()
        return {'audience': 'rider', 'starts_at': (now - timedelta(minutes=5)).isoformat(),
                'ends_at': (now + timedelta(hours=2)).isoformat(),
                'message': 'Short planned maintenance tonight', 'severity': 'info', **extra}

    def test_create_change_cancel_each_audited_and_never_a_checker(self, geo):
        (maker, _), _ = two_super_admins(geo)
        created = post(maker, f'{BASE}/notices/', self.body(scope='IN'))
        assert created.status_code == 201, created.content
        nid = data(created)['id']
        assert data(created)['state'] == 'active'

        changed = put(maker, f'{BASE}/notices/{nid}/', {'message': 'Maintenance moved',
                                                        'severity': 'warning'})
        assert changed.status_code == 200 and data(changed)['severity'] == 'warning'
        cancelled = post(maker, f'{BASE}/notices/{nid}/cancel/', {'reason': 'not needed'})
        assert data(cancelled)['state'] == 'cancelled'
        actions = list(AdminAuditLog.objects.filter(target_type='maintenance_notice')
                       .order_by('id').values_list('action', flat=True))
        assert actions == ['maintenance_notice_created', 'maintenance_notice_changed',
                           'maintenance_notice_cancelled']
        assert put(maker, f'{BASE}/notices/{nid}/', {'message': 'again'}).status_code == 409

    def test_the_window_decides_what_is_active(self, geo):
        now = timezone.now()
        MaintenanceNotice.objects.create(audience='rider', starts_at=now - timedelta(hours=2),
                                         ends_at=now - timedelta(hours=1), message='past')
        MaintenanceNotice.objects.create(audience='rider', starts_at=now + timedelta(hours=1),
                                         ends_at=now + timedelta(hours=2), message='future')
        MaintenanceNotice.objects.create(audience='rider', starts_at=now - timedelta(hours=1),
                                         ends_at=now + timedelta(hours=1), message='now')
        MaintenanceNotice.objects.create(audience='rider', starts_at=now - timedelta(hours=1),
                                         ends_at=now + timedelta(hours=1), message='cancelled',
                                         cancelled_at=now)
        assert [n.message for n in notices.active_for('rider')] == ['now']
        edge = MaintenanceNotice.objects.create(audience='rider', starts_at=now,
                                                ends_at=now + timedelta(seconds=1), message='edge')
        assert edge.active_at(now) and not edge.active_at(now + timedelta(seconds=1))

    def test_scope_global_country_city(self, geo):
        now = timezone.now()
        kw = dict(audience='rider', starts_at=now - timedelta(hours=1),
                  ends_at=now + timedelta(hours=1))
        MaintenanceNotice.objects.create(message='global', **kw)
        MaintenanceNotice.objects.create(message='india', scope_node=geo['IN'], **kw)
        MaintenanceNotice.objects.create(message='hyderabad', scope_node=geo['IN-TG-HYD'], **kw)
        MaintenanceNotice.objects.create(message='austin', scope_node=geo['US-TX-AUS'], **kw)

        assert {n.message for n in notices.active_for('rider', geo['IN-TG-HYD'])} == {
            'global', 'india', 'hyderabad'}
        assert {n.message for n in notices.active_for('rider', geo['IN-TG-WGL'])} == {
            'global', 'india'}
        assert {n.message for n in notices.active_for('rider', geo['US-TX-AUS'])} == {
            'global', 'austin'}

    @pytest.mark.parametrize('change,code', [
        ({'message': ''}, 'INVALID_MESSAGE'),
        ({'message': 'x' * 281}, 'INVALID_MESSAGE'),
        ({'message': '<script>alert(1)</script>'}, 'INVALID_MESSAGE'),
        ({'audience': 'press'}, 'INVALID_AUDIENCE'),
        ({'severity': 'critical'}, 'INVALID_SEVERITY'),
        ({'scope': 'IN-TG'}, 'INVALID_SCOPE'),
    ])
    def test_a_bad_notice_is_refused(self, geo, change, code):
        (maker, _), _ = two_super_admins(geo)
        response = post(maker, f'{BASE}/notices/', self.body(**change))
        assert response.status_code == 400 and error_code(response) == code

    def test_the_end_must_follow_the_start_and_the_window_is_bounded(self, geo):
        (maker, _), _ = two_super_admins(geo)
        now = timezone.now()
        backwards = self.body(starts_at=now.isoformat(), ends_at=(now - timedelta(hours=1)
                                                                  ).isoformat())
        assert error_code(post(maker, f'{BASE}/notices/', backwards)) == 'INVALID_WINDOW'
        forever = self.body(ends_at=(now + timedelta(days=400)).isoformat())
        assert error_code(post(maker, f'{BASE}/notices/', forever)) == 'INVALID_WINDOW'

    def test_the_console_banner_comes_with_who_am_i_and_respects_scope(self, geo):
        now = timezone.now()
        kw = dict(audience='console', starts_at=now - timedelta(hours=1),
                  ends_at=now + timedelta(hours=1), severity='warning')
        MaintenanceNotice.objects.create(message='everyone', **kw)
        MaintenanceNotice.objects.create(message='hyderabad only', scope_node=geo['IN-TG-HYD'], **kw)
        MaintenanceNotice.objects.create(message='austin only', scope_node=geo['US-TX-AUS'], **kw)
        MaintenanceNotice.objects.create(message='riders', audience='rider',
                                         starts_at=now - timedelta(hours=1),
                                         ends_at=now + timedelta(hours=1))
        ops, _ = operator('city_ops_manager', geo, scope_code='IN-TG-HYD')
        shown = {n['message'] for n in data(ops.get('/api/v1/admin-access/me/'))['notices']}
        assert shown == {'everyone', 'hyderabad only'}
        sa, _ = operator('super_admin', geo)
        assert {n['message'] for n in data(sa.get('/api/v1/admin-access/me/'))['notices']} == {
            'everyone', 'hyderabad only', 'austin only'}

    def test_city_ops_reads_notices_but_cannot_post(self, geo):
        ops, _ = operator('city_ops_manager', geo)
        assert ops.get(f'{BASE}/notices/').status_code == 200
        assert post(ops, f'{BASE}/notices/', self.body()).status_code == 403
