"""25 Nov (owner), console capabilities, Govern: approvals send back and the 3-day escalation;
staff two-step reset; My security sessions."""

from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from base import ops_mfa
from servers.admin_audit.models import AdminAuditLog
from servers.approvals import escalation, service
from servers.approvals.models import DEFAULT_TTL_HOURS, RETURNED, ApprovalRequest
from servers.command_centre.models import APPROVAL_ESCALATED, OperatorAlert
from test_maker_checker import (  # noqa: F401 - fixtures
    _reset_calls, _stand_in_executors, actions_for, assign, checker_roles, client, geo, hyd_checker,
    hyd_support, make_admin, pending, policies, refund,
)

pytestmark = pytest.mark.django_db
API = '/api/v1/approvals/admin'


class TestSendBack:
    def test_needs_a_comment_runs_nothing_and_frees_the_key(self, pending, hyd_checker, hyd_support, geo):
        c = client(hyd_checker)
        bare = c.post(f'{API}/requests/{pending.id}/send-back/', {'reason': 'no'}, format='json')
        assert bare.status_code == 400 and bare.json()['error']['code'] == 'REASON_REQUIRED'

        r = c.post(f'{API}/requests/{pending.id}/send-back/', {'reason': 'Attach the trip receipt'}, format='json')
        assert r.status_code == 200, r.content
        assert r.json()['data']['status'] == RETURNED
        pending.refresh_from_db()
        assert pending.decision_reason == 'Attach the trip receipt' and pending.checker_id == hyd_checker.pk
        assert actions_for(pending)[-1] == 'approval_sent_back'
        # The maker may raise it again, changed.
        again = refund(hyd_support, 75_000, node=geo['IN-TG-HYD'], key='refund:pending')
        assert not again.duplicate

    def test_the_maker_cannot_send_back_their_own(self, pending, hyd_support):
        r = client(hyd_support).post(f'{API}/requests/{pending.id}/send-back/', {'reason': 'Attach it'}, format='json')
        assert r.status_code == 403


class TestEscalation:
    def test_requests_wait_a_week_before_they_lapse(self):
        assert DEFAULT_TTL_HOURS == 168

    def test_after_three_days_super_admin_is_alerted_once_and_cleared_on_decision(self, pending, hyd_checker):
        ApprovalRequest.objects.filter(pk=pending.pk).update(requested_at=timezone.now() - timedelta(days=4))
        assert escalation.sweep() == (1, 0)
        assert escalation.sweep() == (0, 0)  # one alert, one audit row
        alert = OperatorAlert.objects.get(kind=APPROVAL_ESCALATED, source_id=str(pending.pk))
        assert 'waiting 4 days' in alert.title
        assert AdminAuditLog.objects.filter(action='approval_escalated', target_id=str(pending.pk)).count() == 1
        assert client(hyd_checker).get(f'{API}/requests/{pending.id}/').json()['data']['escalated'] is True

        service.reject(pending, hyd_checker, reason='not owed')
        assert escalation.sweep() == (0, 1)
        alert.refresh_from_db()
        assert alert.acknowledged_at is not None

    def test_a_fresh_request_is_not_escalated(self, pending):
        assert escalation.sweep() == (0, 0)


@pytest.fixture
def owner(geo):
    u = make_admin('owner.govern@saaradhigo.test')
    assign(u, 'super_admin', geo['GLOBAL'])
    return u


class TestStaffMfaReset:
    URL = '/api/v1/admin-access/staff/{}/reset-mfa/'

    def test_resets_ends_sessions_and_is_audited(self, owner, geo):
        hire = make_admin('lost.phone@saaradhigo.test')
        assign(hire, 'support_agent', geo['IN-TG-HYD'])
        device, _ = ops_mfa.enroll_totp(hire)
        device.confirmed = True
        device.save()
        RefreshToken.for_user(hire)
        r = client(owner).post(self.URL.format(hire.pk), {'reason': 'Lost phone, confirmed by call'}, format='json')
        assert r.status_code == 200, r.content
        assert not ops_mfa.has_device(hire) and ops_mfa.pending_totp_device(hire) is not None
        row = AdminAuditLog.objects.get(action='staff_mfa_reset', target_id=str(hire.pk))
        assert row.reason == 'Lost phone, confirmed by call' and row.after['sessions_ended'] >= 1

    def test_needs_a_reason_a_fresh_factor_and_another_person(self, owner, geo):
        hire = make_admin('x.phone@saaradhigo.test')
        assign(hire, 'support_agent', geo['IN-TG-HYD'])
        ops_mfa.enroll_totp(hire)
        assert client(owner).post(self.URL.format(hire.pk), {'reason': 'short'}, format='json').status_code == 400
        with mock.patch('base.ops_mfa.console_access_allowed', return_value=False):
            r = client(owner).post(self.URL.format(hire.pk), {'reason': 'Lost phone, confirmed by call'}, format='json')
        assert r.status_code == 401
        assert client(owner).post(self.URL.format(owner.pk), {'reason': 'Lost phone, confirmed'},
                                  format='json').status_code == 403

    def test_a_city_role_cannot_reset(self, owner, geo, hyd_support):
        hire = make_admin('y.phone@saaradhigo.test')
        r = client(hyd_support).post(self.URL.format(hire.pk), {'reason': 'Lost phone, confirmed by call'}, format='json')
        assert r.status_code == 403


class TestMySessions:
    def test_lists_and_signs_out_the_others(self, owner):
        here = RefreshToken.for_user(owner)
        RefreshToken.for_user(owner)
        RefreshToken.for_user(owner)
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(owner)}', HTTP_X_SG_SESSION=here['jti'])
        listed = c.get('/api/v1/auth/sessions/').json()['data']
        assert listed['count'] == 3 and listed['this_session_known']
        out = c.post('/api/v1/auth/sessions/sign-out-others/', {}, format='json')
        assert out.status_code == 200 and out.json()['data']['sessions_ended'] == 2
        after = c.get('/api/v1/auth/sessions/').json()['data']
        assert after['count'] == 1 and after['sessions'][0]['current']
        assert AdminAuditLog.objects.filter(action='staff_signed_out_other_sessions').exists()

    def test_an_unknown_session_is_refused(self, owner):
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(owner)}', HTTP_X_SG_SESSION='nope')
        assert c.post('/api/v1/auth/sessions/sign-out-others/', {}, format='json').status_code == 409


class TestWhoCanApprove:
    def test_each_action_lists_the_roles_that_may_check_it(self, owner, checker_roles, hyd_checker):
        data = client(owner).get(f'{API}/policies/').json()['data']
        refund = next(a for a in data['actions'] if a['key'] == 'support.refund')
        names = {c['role']: c['people'] for c in refund['checkers']}
        assert 'support_checker' in names
        assert 'support_agent' not in names  # no approval.check
