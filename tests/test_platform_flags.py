"""Feature flags (module 15): resolution order, the change path, the checker for money-affecting
and safety flags, versioned history and the audit trail. A TEST-ONLY flag is used throughout:
no real flag is seeded because nothing in the platform reads one yet.
"""

import pytest

from servers.admin_audit.models import AdminAuditLog
from servers.approvals.models import ApprovalRequest
from servers.platform_ops import flags
from servers.platform_ops.models import FeatureFlag, FeatureFlagOverride
from test_sos_support_zones_matrix import geo, operator  # noqa: F401 - fixtures
from platform_support import (
    BASE, approve, data, error_code, post, put, two_super_admins,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def policies(db):
    from io import StringIO

    from django.core.management import call_command

    from conftest import seed_actor

    call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=StringIO())


def make(geo, key='test.flag', **extra):
    (maker, _), _ = two_super_admins(geo)
    body = {'key': key, 'description': 'a test-only flag', **extra}
    response = post(maker, f'{BASE}/flags/', body)
    assert response.status_code == 201, response.content
    return response


class TestResolution:
    def test_city_beats_country_beats_default(self, geo):
        flag = FeatureFlag.objects.create(key='res.flag', description='x', default_enabled=False)
        hyd, india, austin = geo['IN-TG-HYD'], geo['IN'], geo['US-TX-AUS']

        assert flags.flag_enabled('res.flag', city=hyd) is False                  # default
        FeatureFlagOverride.objects.create(flag=flag, node=india, enabled=True)
        assert flags.flag_enabled('res.flag', city=hyd) is True                   # country
        assert flags.flag_enabled('res.flag', country='IN') is True
        assert flags.flag_enabled('res.flag', city=austin) is False               # other country
        FeatureFlagOverride.objects.create(flag=flag, node=hyd, enabled=False)
        assert flags.flag_enabled('res.flag', city=hyd) is False                  # city wins
        assert flags.flag_enabled('res.flag', city='IN-TG-HYD') is False          # by code
        assert flags.flag_enabled('res.flag', city=geo['IN-TG-WGL']) is True      # country again

    def test_an_unknown_or_archived_flag_answers_the_default_argument(self, geo):
        assert flags.flag_enabled('no.such.flag') is False
        assert flags.flag_enabled('no.such.flag', default=True) is True
        FeatureFlag.objects.create(key='old.flag', description='x', default_enabled=True,
                                   is_archived=True)
        assert flags.flag_enabled('old.flag') is False

    def test_nothing_is_seeded(self, geo):
        assert not FeatureFlag.objects.exists()


class TestChangeWithoutAChecker:
    def test_an_ordinary_flag_takes_effect_immediately_with_a_version_and_an_audit_row(self, geo):
        make(geo)
        (maker, _), _ = two_super_admins(geo)

        response = put(maker, f'{BASE}/flags/test.flag/', {
            'default_enabled': True, 'overrides': {'IN-TG-HYD': False}, 'reason': 'turn it on'})

        assert response.status_code == 200, response.content
        body = data(response)
        assert body['state'] == 'applied' and body['version'] == 2
        assert body['default_enabled'] is True and body['overrides'] == {'IN-TG-HYD': False}
        row = AdminAuditLog.objects.filter(action='feature_flag_changed').latest('id')
        assert row.before == {'default': False, 'overrides': {}}
        assert row.after['default'] is True and row.reason == 'turn it on'
        history = data(maker.get(f'{BASE}/flags/test.flag/'))['history']
        assert [h['version'] for h in history] == [2, 1]
        assert history[0]['snapshot']['overrides'] == {'IN-TG-HYD': False}

    def test_removing_an_override_and_a_no_op(self, geo):
        make(geo)
        (maker, _), _ = two_super_admins(geo)
        put(maker, f'{BASE}/flags/test.flag/', {'overrides': {'IN': True}, 'reason': 'country on'})
        again = put(maker, f'{BASE}/flags/test.flag/', {'overrides': {'IN': True},
                                                        'reason': 'same again'})
        assert data(again)['version'] == 2                        # nothing changed, no new version
        gone = put(maker, f'{BASE}/flags/test.flag/', {'overrides': {'IN': None},
                                                       'reason': 'country off'})
        assert data(gone)['overrides'] == {} and data(gone)['version'] == 3

    @pytest.mark.parametrize('body,code', [
        ({'reason': 'no change given'}, 'NOTHING_TO_CHANGE'),
        ({'overrides': {'IN-TG': True}, 'reason': 'a state'}, 'INVALID_SCOPE'),
        ({'overrides': {'IN': 'yes'}, 'reason': 'not a bool'}, 'INVALID_VALUE'),
        ({'default_enabled': True, 'reason': 'x'}, 'REASON_REQUIRED'),
    ])
    def test_bad_changes_are_refused(self, geo, body, code):
        make(geo)
        (maker, _), _ = two_super_admins(geo)
        response = put(maker, f'{BASE}/flags/test.flag/', body)
        assert response.status_code == 400 and error_code(response) == code


@pytest.mark.usefixtures('policies')
class TestChangeWithAChecker:
    @pytest.mark.parametrize('kind', ['money_affecting', 'safety'])
    def test_a_money_affecting_or_safety_flag_needs_a_checker(self, geo, kind):
        make(geo, **{kind: True})
        (maker, _), (checker, _) = two_super_admins(geo)

        response = put(maker, f'{BASE}/flags/test.flag/', {'default_enabled': True,
                                                           'reason': 'switch it on'})

        assert response.status_code == 202, response.content
        assert FeatureFlag.objects.get(key='test.flag').default_enabled is False
        rid = data(response)['approval_request']['id']
        assert approve(maker, rid).status_code == 403
        assert approve(checker, rid).status_code == 200
        flag = FeatureFlag.objects.get(key='test.flag')
        assert flag.default_enabled is True and flag.version == 2
        assert flags.history(flag)[0]['approval_request_id'] == rid
        assert AdminAuditLog.objects.filter(action='feature_flag_changed').exists()

    def test_a_request_made_against_an_old_version_is_refused_at_approval(self, geo):
        make(geo, money_affecting=True)
        (maker, _), (checker, _) = two_super_admins(geo)
        rid = data(put(maker, f'{BASE}/flags/test.flag/', {
            'default_enabled': True, 'reason': 'switch it on'}))['approval_request']['id']
        flags.apply_change('test.flag', None, {'IN': True}, actor=None, reason='moved meanwhile')

        approve(checker, rid)

        request = ApprovalRequest.objects.get(pk=rid)
        assert request.status == 'failed' and request.error_code == 'STALE_REQUEST'
        assert FeatureFlag.objects.get(key='test.flag').default_enabled is False

    def test_the_policy_is_seeded_for_both_countries(self, geo):
        from servers.approvals.service import active_policy

        for country in ('IN', 'US'):
            assert active_policy(country, 'platform.flag_change').limit_kind == 'always'


class TestCreateAndScope:
    def test_create_validates_and_audits(self, geo):
        (maker, _), _ = two_super_admins(geo)
        assert post(maker, f'{BASE}/flags/', {'key': 'Bad Key', 'description': 'x'}
                    ).status_code == 400
        make(geo, key='good.key')
        assert post(maker, f'{BASE}/flags/', {'key': 'good.key', 'description': 'x'}
                    ).status_code == 409
        assert AdminAuditLog.objects.filter(action='feature_flag_created',
                                            target_id='good.key').exists()

    def test_city_ops_can_read_flags_but_never_change_them(self, geo):
        make(geo)
        ops, _ = operator('city_ops_manager', geo)
        assert ops.get(f'{BASE}/flags/').status_code == 200
        assert put(ops, f'{BASE}/flags/test.flag/', {'default_enabled': True,
                                                     'reason': 'let me'}).status_code == 403
        assert post(ops, f'{BASE}/flags/', {'key': 'ops.flag', 'description': 'x'}
                    ).status_code == 403

    def test_roles_without_platform_read_are_refused(self, geo):
        for role in ('finance', 'marketing', 'analyst', 'support_agent', 'support_lead',
                     'safety_officer', 'kyc_verifier'):
            client, _ = operator(role, geo)
            assert client.get(f'{BASE}/flags/').status_code == 403, role
