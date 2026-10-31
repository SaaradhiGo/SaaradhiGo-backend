"""Module 11: the Notifications console API. Capabilities, scope, maker-checker, audit.

WHAT THESE PROVE
----------------
  * CAPABILITIES. `notify.read` (Super Admin, City Ops, Support, Marketing) opens the delivery log,
    templates, campaigns and providers; `notify.template` and `notify.campaign` (Super Admin and
    Marketing) draft and submit. Analyst, KYC and Finance hold none.
  * SCOPE. Out of scope is a 404 identical to a missing row; in scope without the capability is
    403. Delivery logs are scoped by the RECIPIENT's city.
  * MAKER-CHECKER. Activating a template (`notification.template_change`) and sending a campaign
    (`notification.campaign_send`) are requests a DIFFERENT admin holding approval.check AND the
    same capability approves; nothing changes before that; a rejected request returns the draft.
  * THE INDIA SMS RULE is refused at submit, with the reason, and again at approval.
  * EVERY WRITE IS AUDITED, and no response carries a raw phone, email or token (every call goes
    through the PII sweep helper).
"""

from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone

from finance_world import client_for, operator
from notifications_support import (  # noqa: F401
    block_the_network, fake_credentials, geo, make_driver_user, make_rider_user, make_template,
)
from servers.admin_access.models import AdminAssignment, AdminRole, GeoNode
from servers.admin_audit.models import AdminAuditLog
from servers.approvals.models import ApprovalRequest
from servers.notifications import campaign_workflow, preferences
from servers.notifications.models import (
    ACTIVE, ARCHIVED, CAMPAIGN_CANCELLED, CAMPAIGN_DRAFT, CAMPAIGN_PENDING, CAMPAIGN_SCHEDULED,
    CAMPAIGN_SENT, DRAFT, PENDING_APPROVAL, Campaign, NotificationMessage, NotificationTemplate,
)
from test_finance_actions import approve_request, body_of, checker, error_code, get, post

pytestmark = [pytest.mark.django_db]

API = '/api/v1/notifications'
ADM = f'{API}/admin'
APPROVALS = '/api/v1/approvals/admin/requests'


@pytest.fixture(autouse=True)
def no_celery(monkeypatch):
    import servers.notifications.tasks as tasks

    for task in (tasks.send_message, tasks.run_campaign, tasks.dispatch_queued):
        monkeypatch.setattr(task, 'delay', lambda *a, **k: None)
    monkeypatch.setattr(tasks.send_message, 'apply_async', lambda *a, **k: None)


def put(client, path, body=None, expect=None):
    from test_finance_actions import swept

    response = swept(client.put(path, body or {}, format='json'))
    if expect is not None:
        assert response.status_code == expect, (path, response.status_code, response.content[:500])
    return response


def patch(client, path, body=None, expect=None):
    from test_finance_actions import swept

    response = swept(client.patch(path, body or {}, format='json'))
    if expect is not None:
        assert response.status_code == expect, (path, response.status_code, response.content[:500])
    return response


def raw(client, path, expect=200):
    """The response itself (for a test that reads the body text), swept for raw PII."""
    from test_finance_actions import swept

    response = swept(client.get(path))
    assert response.status_code == expect, (path, response.status_code, response.content[:300])
    return response


def rid(response):
    return body_of(response)['approval_request']['id']


def approve(request_id):
    chk, _ = checker()
    response = approve_request(chk, request_id)
    assert response.status_code == 200, response.content[:400]
    return response


EMAIL = {'key': 'ride.test_mail', 'channel': 'email', 'country': 'IN',
         'category': 'transactional', 'subject': 'Hello {name}', 'body': 'Dear {name}, hello.',
         'variables': ['name'], 'personal_variables': ['name'],
         'sample_values': {'name': 'Asha'}, 'reason': 'New email copy'}

SMS = {'key': 'ride.test_sms', 'channel': 'sms', 'country': 'IN', 'category': 'transactional',
       'body': 'SaaradhiGo: pay {amount} by {date}.', 'variables': ['amount', 'date'],
       'sample_values': {'amount': '10', 'date': '1 Jan'}, 'reason': 'New SMS copy'}
REGISTRATION = {'provider': 'msg91', 'sender_id': 'SGOTST', 'dlt_template_id': 'DLT-TEST-1',
                'provider_template_id': 'flow-1',
                'registered_text': 'SaaradhiGo: pay {#var#} by {#var#}.'}


@pytest.fixture
def world(geo):
    from types import SimpleNamespace

    return SimpleNamespace(
        geo=geo,
        mkt=operator('marketing', geo, 'IN'),
        mkt_hyd=operator('marketing', geo, 'IN-TG-HYD'),
        mkt_us=operator('marketing', geo, 'US'),
        support=operator('support_agent', geo, 'IN-TG-HYD'),
        ops=operator('city_ops_manager', geo, 'IN-TG-HYD'),
        analyst=operator('analyst', geo, 'IN-TG-HYD'),
        finance=operator('finance', geo, 'IN-TG-HYD'),
        kyc=operator('kyc_verifier', geo, 'IN-TG-HYD'),
        super=operator('super_admin', geo),
    )


# ===========================================================================
# CAPABILITIES
# ===========================================================================

class TestCapabilities:
    def test_the_seeded_roles_hold_exactly_what_the_brief_says(self):
        from servers.admin_access.roles import ROLE_TEMPLATES
        held = {t['code']: {p for p in t['permissions'] if p.startswith('notify.')}
                for t in ROLE_TEMPLATES}
        full = {'notify.send', 'notify.read', 'notify.template', 'notify.campaign'}
        assert held['super_admin'] == full
        assert held['marketing'] == {'notify.send', 'notify.read', 'notify.template',
                                     'notify.campaign'}
        for code in ('city_ops_manager', 'support_agent', 'support_lead'):
            assert held[code] == {'notify.read'}, code
        for code in ('analyst', 'kyc_verifier', 'finance', 'safety_officer'):
            assert held[code] == set(), code

    @pytest.mark.parametrize('who', ['analyst', 'finance', 'kyc'])
    def test_roles_without_notify_read_get_403_everywhere(self, world, who):
        client, _ = getattr(world, who)
        for path in ('templates/', 'campaigns/', 'messages/', 'providers/'):
            assert raw(client, f'{ADM}/{path}', expect=403).status_code == 403

    @pytest.mark.parametrize('who', ['support', 'ops'])
    def test_read_only_roles_can_look_but_not_draft(self, world, who):
        client, _ = getattr(world, who)
        for path in ('templates/', 'campaigns/', 'messages/', 'providers/'):
            get(client, f'{ADM}/{path}')
        post(client, f'{ADM}/templates/', EMAIL, expect=403)
        post(client, f'{ADM}/campaigns/', {'name': 'x'}, expect=403)
        post(client, f'{ADM}/campaigns/audience-preview/', {}, expect=403)

    def test_anonymous_and_non_operators_are_refused(self, world):
        from rest_framework.test import APIClient
        assert APIClient().get(f'{ADM}/templates/').status_code in (401, 403)
        rider = make_rider_user(world.geo)
        assert client_for(rider).get(f'{ADM}/templates/').status_code == 403


# ===========================================================================
# TEMPLATES: drafting and maker-checker
# ===========================================================================

class TestTemplates:
    def draft(self, world, **over):
        client, _ = world.mkt
        return post(client, f'{ADM}/templates/', {**EMAIL, **over}, expect=201)

    def test_a_draft_is_created_edited_and_audited(self, world):
        data = body_of(self.draft(world))
        assert data['status'] == DRAFT and data['version'] == 1 and data['country'] == 'IN'
        client, _ = world.mkt
        edited = body_of(patch(client, f'{ADM}/templates/{data["id"]}/',
                               {'body': 'Dear {name}, hi.'}, expect=200))
        assert edited['body'] == 'Dear {name}, hi.'
        actions = list(AdminAuditLog.objects.filter(target_type='notification_template').order_by('id')
                       .values_list('action', flat=True))
        assert actions == ['notification_template_created', 'notification_template_updated']

    def test_a_reason_and_a_known_channel_and_country_are_required(self, world):
        client, _ = world.mkt
        post(client, f'{ADM}/templates/', {**EMAIL, 'reason': ''}, expect=400)
        post(client, f'{ADM}/templates/', {**EMAIL, 'channel': 'pigeon'}, expect=400)
        assert error_code(post(client, f'{ADM}/templates/', {**EMAIL, 'country': ''})) == 'COUNTRY_REQUIRED'
        assert error_code(post(client, f'{ADM}/templates/', {**EMAIL, 'country': 'FR'})) == 'COUNTRY_REQUIRED'

    def test_a_country_outside_the_makers_scope_is_refused(self, world):
        client, _ = world.mkt_us
        assert error_code(post(client, f'{ADM}/templates/', EMAIL, expect=403)) == 'SCOPE_NOT_HELD'

    def test_a_new_version_copies_the_registrations(self, world):
        first = body_of(self.draft(world))
        client, _ = world.mkt
        put(client, f'{ADM}/templates/{first["id"]}/registrations/',
            {'registrations': [{'provider': 'ses', 'sender_id': 'x'}]}, expect=200)
        second = body_of(post(client, f'{ADM}/templates/', {
            'from_template_id': first['id'], 'reason': 'v2', 'body': 'Dear {name}, hello again.'},
            expect=201))
        assert second['version'] == 2 and second['key'] == first['key']
        assert [r['provider'] for r in second['registrations']] == ['ses']

    def test_only_a_draft_can_be_edited(self, world):
        t = make_template(world.geo, channel='email', subject='s', body='b')
        client, _ = world.mkt
        assert error_code(patch(client, f'{ADM}/templates/{t.id}/', {'body': 'x'},
                                expect=409)) == 'TEMPLATE_NOT_DRAFT'
        put(client, f'{ADM}/templates/{t.id}/registrations/', {'registrations': []}, expect=409)

    def test_the_preview_renders_sample_values_and_reports_problems(self, world):
        client, _ = world.mkt
        data = body_of(post(client, f'{ADM}/templates/preview/', {
            'body': 'Hi {name}, {extra}', 'variables': ['name'], 'values': {'name': 'Asha'}}, expect=200))
        assert data['missing'] == ['extra'] and data['body'] == ''
        assert any('undeclared' in p for p in data['problems'])
        ok = body_of(post(client, f'{ADM}/templates/preview/', {
            'body': 'Hi {name}', 'variables': ['name'], 'values': {'name': 'Asha'}}, expect=200))
        assert ok['body'] == 'Hi Asha' and ok['problems'] == [] and ok['missing'] == []

    def test_the_stored_preview_uses_sample_values_and_the_dlt_check(self, world):
        client, _ = world.mkt
        t = body_of(post(client, f'{ADM}/templates/', SMS, expect=201))
        data = body_of(post(client, f'{ADM}/templates/{t["id"]}/preview/', {}, expect=200))
        assert data['body'] == 'SaaradhiGo: pay 10 by 1 Jan.'
        assert data['registration_problems'][0]['code'] == 'DLT_REGISTRATION_REQUIRED'

    # --- the India SMS rule --------------------------------------------------

    def test_an_india_sms_cannot_be_submitted_without_dlt_ids(self, world):
        client, _ = world.mkt
        t = body_of(post(client, f'{ADM}/templates/', SMS, expect=201))
        response = post(client, f'{ADM}/templates/{t["id"]}/submit/', {'reason': 'go'}, expect=422)
        error = response.json()['error']
        assert error['code'] == 'DLT_REGISTRATION_REQUIRED'
        assert 'DLT template ID and sender ID and msg91 flow ID' in error['message']
        assert NotificationTemplate.objects.get(pk=t['id']).status == DRAFT
        assert not ApprovalRequest.objects.exists()

    @pytest.mark.parametrize('missing', ['dlt_template_id', 'sender_id', 'provider_template_id'])
    def test_each_missing_id_blocks_activation(self, world, missing):
        client, _ = world.mkt
        t = body_of(post(client, f'{ADM}/templates/', SMS, expect=201))
        put(client, f'{ADM}/templates/{t["id"]}/registrations/',
            {'registrations': [{**REGISTRATION, missing: ''}]}, expect=200)
        response = post(client, f'{ADM}/templates/{t["id"]}/submit/', {'reason': 'go'}, expect=422)
        assert response.json()['error']['code'] == 'DLT_REGISTRATION_REQUIRED'

    def test_placeholders_must_map_one_to_one_onto_the_dlt_slots(self, world):
        client, _ = world.mkt
        t = body_of(post(client, f'{ADM}/templates/', SMS, expect=201))
        for text, fragment in (
                ('SaaradhiGo: pay {#var#}.', '2 placeholder(s) but the DLT text has 1'),
                ('SaaradhiGo: please pay {#var#} by {#var#}.', 'does not match the DLT-approved text')):
            put(client, f'{ADM}/templates/{t["id"]}/registrations/',
                {'registrations': [{**REGISTRATION, 'registered_text': text}]}, expect=200)
            response = post(client, f'{ADM}/templates/{t["id"]}/submit/', {'reason': 'go'}, expect=422)
            assert response.json()['error']['code'] == 'DLT_PLACEHOLDER_MISMATCH'
            assert fragment in response.json()['error']['message']

    def test_a_complete_india_sms_registration_is_accepted_and_goes_live_via_a_checker(self, world):
        client, _ = world.mkt
        t = body_of(post(client, f'{ADM}/templates/', SMS, expect=201))
        put(client, f'{ADM}/templates/{t["id"]}/registrations/',
            {'registrations': [REGISTRATION]}, expect=200)
        request_id = rid(post(client, f'{ADM}/templates/{t["id"]}/submit/',
                              {'reason': 'go'}, expect=202))
        approve(request_id)
        assert NotificationTemplate.objects.get(pk=t['id']).status == ACTIVE

    def test_the_rule_is_checked_again_when_the_checker_approves(self, world):
        client, _ = world.mkt
        t = body_of(post(client, f'{ADM}/templates/', SMS, expect=201))
        put(client, f'{ADM}/templates/{t["id"]}/registrations/',
            {'registrations': [REGISTRATION]}, expect=200)
        request_id = rid(post(client, f'{ADM}/templates/{t["id"]}/submit/',
                             {'reason': 'go'}, expect=202))
        # The registration is wiped while the request waits (by another path, not the API).
        NotificationTemplate.objects.get(pk=t['id']).registrations.update(dlt_template_id='')
        chk, _ = checker()
        response = approve_request(chk, request_id)
        row = ApprovalRequest.objects.get(pk=request_id)
        assert row.status == 'failed' and row.error_code == 'DLT_REGISTRATION_REQUIRED'
        assert NotificationTemplate.objects.get(pk=t['id']).status == PENDING_APPROVAL
        assert response.status_code in (200, 409)

    def test_us_sms_needs_no_dlt(self, world):
        client, _ = world.mkt_us
        t = body_of(post(client, f'{ADM}/templates/', {**SMS, 'country': 'US'}, expect=201))
        post(client, f'{ADM}/templates/{t["id"]}/submit/', {'reason': 'go'}, expect=202)

    # --- maker-checker ---------------------------------------------------------

    def test_submitting_raises_a_request_and_changes_nothing_until_a_checker_approves(self, world):
        t = body_of(self.draft(world))
        client, maker = world.mkt
        response = post(client, f'{ADM}/templates/{t["id"]}/submit/', {'reason': 'go live'}, expect=202)
        data = body_of(response)
        assert data['state'] == 'pending_approval'
        request = ApprovalRequest.objects.get(pk=rid(response))
        assert request.action_key == 'notification.template_change' and request.country == 'IN'
        assert request.maker_id == maker.pk and request.scope_node.code == 'IN'
        assert NotificationTemplate.objects.get(pk=t['id']).status == PENDING_APPROVAL
        assert not NotificationTemplate.objects.filter(status=ACTIVE).exists()

    def test_the_maker_cannot_approve_their_own_template(self, world):
        t = body_of(self.draft(world))
        client, _ = world.mkt
        request_id = rid(post(client, f'{ADM}/templates/{t["id"]}/submit/', {'reason': 'go'}, expect=202))
        assert approve_request(client, request_id).status_code == 403

    def test_a_checker_needs_approval_check_and_notify_template(self, world):
        t = body_of(self.draft(world))
        client, _ = world.mkt
        request_id = rid(post(client, f'{ADM}/templates/{t["id"]}/submit/', {'reason': 'go'}, expect=202))
        only_check = AdminRole.objects.create(code='check_only', name='Check only',
                                              permissions=['approval.check'])
        only_notify = AdminRole.objects.create(code='notify_only', name='Notify only',
                                               permissions=['notify.template'])
        results = {}
        for role in (only_check, only_notify):
            c, user = operator('analyst', world.geo, 'IN')
            AdminAssignment.objects.filter(admin=user).update(role=role)
            results[role.code] = approve_request(c, request_id)
        assert results['check_only'].status_code == 403
        assert error_code(results['check_only']) == 'CHECKER_LACKS_ACTION_CAPABILITY'
        assert results['notify_only'].status_code == 403          # no approval.check at all
        assert NotificationTemplate.objects.get(pk=t['id']).status == PENDING_APPROVAL

    def test_approval_activates_and_archives_the_previous_version_and_audits(self, world):
        first = body_of(self.draft(world))
        client, _ = world.mkt
        approve(rid(post(client, f'{ADM}/templates/{first["id"]}/submit/',
                         {'reason': 'go'}, expect=202)))
        second = body_of(post(client, f'{ADM}/templates/', {
            'from_template_id': first['id'], 'reason': 'v2', 'body': 'Dear {name}, hello v2.'},
            expect=201))
        approve(rid(post(client, f'{ADM}/templates/{second["id"]}/submit/',
                         {'reason': 'v2 live'}, expect=202)))
        statuses = dict(NotificationTemplate.objects.values_list('version', 'status'))
        assert statuses == {1: ARCHIVED, 2: ACTIVE}
        detail = (get(client, f'{ADM}/templates/{second["id"]}/'))
        assert [v['version'] for v in detail['versions']] == [2, 1]
        assert AdminAuditLog.objects.filter(action='notification_template_activated').count() == 2

    def test_a_rejected_request_returns_the_draft(self, world):
        t = body_of(self.draft(world))
        client, _ = world.mkt
        request_id = rid(post(client, f'{ADM}/templates/{t["id"]}/submit/', {'reason': 'go'}, expect=202))
        chk, _ = checker()
        assert post(chk, f'{APPROVALS}/{request_id}/reject/', {'reason': 'no'}, expect=200)
        assert NotificationTemplate.objects.get(pk=t['id']).status == DRAFT

    def test_a_cancelled_request_returns_the_draft(self, world):
        t = body_of(self.draft(world))
        client, _ = world.mkt
        request_id = rid(post(client, f'{ADM}/templates/{t["id"]}/submit/', {'reason': 'go'}, expect=202))
        assert post(client, f'{APPROVALS}/{request_id}/cancel/', {'reason': 'oops'}, expect=200)
        assert NotificationTemplate.objects.get(pk=t['id']).status == DRAFT

    def test_an_invalid_template_is_refused_at_submit(self, world):
        t = body_of(self.draft(world, body='Dear {name} and {other}.'))
        client, _ = world.mkt
        response = post(client, f'{ADM}/templates/{t["id"]}/submit/', {'reason': 'go'}, expect=422)
        assert response.json()['error']['code'] == 'TEMPLATE_INVALID'
        assert 'undeclared variables: other' in response.json()['error']['message']

    def test_an_email_needs_a_subject_and_a_push_needs_a_title(self, world):
        client, _ = world.mkt
        e = body_of(self.draft(world, subject=''))
        assert post(client, f'{ADM}/templates/{e["id"]}/submit/', {'reason': 'go'},
                    expect=422).json()['error']['code'] == 'TEMPLATE_INVALID'
        p = body_of(self.draft(world, key='ride.test_push', channel='push', title='', subject=''))
        assert post(client, f'{ADM}/templates/{p["id"]}/submit/', {'reason': 'go'},
                    expect=422).json()['error']['code'] == 'TEMPLATE_INVALID'

    def test_retiring_an_active_template_is_a_request_too(self, world):
        t = make_template(world.geo, channel='email', subject='s', body='b', variables=())
        client, _ = world.mkt
        request_id = rid(post(client, f'{ADM}/templates/{t.id}/archive/', {'reason': 'old'}, expect=202))
        assert NotificationTemplate.objects.get(pk=t.id).status == ACTIVE
        approve(request_id)
        assert NotificationTemplate.objects.get(pk=t.id).status == ARCHIVED

    def test_the_policies_exist_for_india_and_the_us_and_are_always_a_checker(self, world):
        from servers.approvals.models import ApprovalPolicy
        for country in ('IN', 'US'):
            for action in ('notification.template_change', 'notification.campaign_send'):
                policy = ApprovalPolicy.objects.get(country=country, action_key=action, is_active=True)
                assert policy.always_checker

    # --- scope ----------------------------------------------------------------

    def test_templates_are_scoped_to_the_callers_country(self, world):
        india = make_template(world.geo, key='a.in', country='IN')
        us = make_template(world.geo, key='a.us', country='US')
        client, _ = world.mkt
        assert {r['id'] for r in (get(client, f'{ADM}/templates/'))['results']} == {india.id}
        raw(client, f'{ADM}/templates/{us.id}/', expect=404)
        patch(client, f'{ADM}/templates/{us.id}/', {'body': 'x'}, expect=404)
        post(client, f'{ADM}/templates/{us.id}/submit/', {'reason': 'x'}, expect=404)
        sup, _ = world.super
        assert len((get(sup, f'{ADM}/templates/'))['results']) == 2
        us_client, _ = world.mkt_us
        assert {r['id'] for r in (get(us_client, f'{ADM}/templates/'))['results']} == {us.id}

    def test_a_missing_template_and_an_out_of_scope_one_answer_identically(self, world):
        us = make_template(world.geo, key='a.us', country='US')
        client, _ = world.mkt
        out_of_scope = raw(client, f'{ADM}/templates/{us.id}/', expect=404)
        missing = raw(client, f'{ADM}/templates/999999/', expect=404)
        assert out_of_scope.json() == missing.json()

    def test_the_scope_switcher_narrows_and_never_widens(self, world):
        make_template(world.geo, key='a.in', country='IN')
        make_template(world.geo, key='a.us', country='US')
        sup, _ = world.super
        india = (get(sup, f'{ADM}/templates/?scope=IN'))['results']
        assert [r['country'] for r in india] == ['IN']
        india_client, _ = world.mkt
        assert raw(india_client, f'{ADM}/templates/?scope=US', expect=403).status_code == 403
        assert raw(india_client, f'{ADM}/messages/?scope=US', expect=403).status_code == 403

    def test_only_english_exists_for_now(self, world):
        client, _ = world.mkt
        assert error_code(post(client, f'{ADM}/templates/', {**EMAIL, 'locale': 'hi'},
                               expect=400)) == 'LOCALE_NOT_SUPPORTED'

    def test_a_read_only_holder_in_scope_gets_403_on_writes(self, world):
        t = make_template(world.geo, channel='email', subject='s', body='b', variables=(),
                          status=DRAFT)
        client, _ = world.support
        raw(client, f'{ADM}/templates/{t.id}/', expect=404)           # HYD support, IN template
        sup_in, _ = operator('support_agent', world.geo, 'IN')
        get(sup_in, f'{ADM}/templates/{t.id}/')
        assert patch(sup_in, f'{ADM}/templates/{t.id}/', {'body': 'x'}).status_code == 403
        assert post(sup_in, f'{ADM}/templates/{t.id}/submit/', {'reason': 'x'}).status_code == 403

    def test_a_city_scoped_marketing_admin_cannot_write_a_country_template(self, world):
        client, _ = world.mkt_hyd
        post(client, f'{ADM}/templates/', EMAIL, expect=403)


# ===========================================================================
# CAMPAIGNS
# ===========================================================================

@pytest.fixture
def promo(world):
    """A promotional push template, three Hyderabad drivers (two opted in, one with no token),
    one Warangal driver."""
    geo = world.geo
    template = make_template(geo, key='promo.weekend', channel='push', category='promotional',
                             title='Weekend', body='Earn more {bonus}', variables=('bonus',))
    users = {}
    for name, node, opted, token in (('a', 'IN-TG-HYD', True, 'tok-a'),
                                     ('b', 'IN-TG-HYD', False, 'tok-b'),
                                     ('c', 'IN-TG-HYD', True, ''),
                                     ('w', 'IN-TG-WGL', True, 'tok-w')):
        user = make_driver_user(geo, node, phone=f'+91970001{ord(name):04d}')
        user.fcm_token = token
        user.save(update_fields=['fcm_token'])
        if opted:
            preferences.set_preference(user, 'push', 'promotional', True, 'app')
        users[name] = user
    return template, users


def campaign_body(template, **over):
    body = {'name': 'Weekend boost', 'template_id': template.id,
            'audience': {'role': 'drivers', 'cities': ['IN-TG-HYD']},
            'template_values': {'bonus': '10%'}, 'reason': 'Weekend supply push'}
    body.update(over)
    return body


class TestCampaigns:
    def create(self, world, promo, who='mkt', **over):
        template, _ = promo
        client, _ = getattr(world, who)
        return body_of(post(client, f'{ADM}/campaigns/', campaign_body(template, **over), expect=201))

    def test_the_audience_preview_is_the_whole_funnel(self, world, promo):
        template, _ = promo
        client, _ = world.mkt
        data = body_of(post(client, f'{ADM}/campaigns/audience-preview/', {
            'template_id': template.id, 'audience': {'role': 'drivers', 'cities': ['IN-TG-HYD']}},
            expect=200))
        funnel = data['funnel']
        assert funnel == {**funnel, 'total': 3, 'unreachable_on_channel': 1, 'not_opted_in': 1, 'reachable': 1,
                          'held_by_quiet_hours': funnel['held_by_quiet_hours'],
                          'send_now': 1 - funnel['held_by_quiet_hours'], 'channel': 'push'}
        assert funnel['quiet_hours'] == [21, 10]

    def test_the_preview_counts_what_quiet_hours_would_hold(self, world, promo):
        from datetime import datetime, timezone as dt_utc
        template, _ = promo
        audience = {'role': 'drivers', 'cities': ['IN-TG-HYD']}
        night = datetime(2026, 11, 3, 17, 0, tzinfo=dt_utc.utc)       # 22:30 in Hyderabad
        day = datetime(2026, 11, 3, 6, 0, tzinfo=dt_utc.utc)          # 11:30 in Hyderabad
        held = campaign_workflow.preview(audience, template, now=night)
        free = campaign_workflow.preview(audience, template, now=day)
        assert (held['held_by_quiet_hours'], held['send_now']) == (1, 0)
        assert held['earliest_release_at'].startswith('2026-11-04T04:30')       # 10:00 IST
        assert (free['held_by_quiet_hours'], free['send_now'], free['earliest_release_at']) == (0, 1, None)

    def test_a_draft_needs_an_active_promotional_template_in_the_same_country(self, world, promo):
        template, _ = promo
        client, _ = world.mkt
        transactional = make_template(world.geo, key='t.x', channel='push', title='t', body='b',
                                      variables=())
        assert error_code(post(client, f'{ADM}/campaigns/', campaign_body(
            template, template_id=transactional.id), expect=400)) == 'TEMPLATE_NOT_USABLE'
        draft = make_template(world.geo, key='promo.d', channel='push', category='promotional',
                              title='t', body='b', variables=(), status=DRAFT)
        assert error_code(post(client, f'{ADM}/campaigns/', campaign_body(
            template, template_id=draft.id), expect=400)) == 'TEMPLATE_NOT_USABLE'
        post(client, f'{ADM}/campaigns/', campaign_body(template, name=''), expect=400)
        post(client, f'{ADM}/campaigns/', campaign_body(
            template, audience={'role': 'robots', 'cities': ['IN-TG-HYD']}), expect=400)
        post(client, f'{ADM}/campaigns/', campaign_body(
            template, template_values={'nope': 'x'}), expect=400)

    def test_a_city_outside_the_callers_scope_is_a_404(self, world, promo):
        template, _ = promo
        client, _ = world.mkt_hyd
        response = post(client, f'{ADM}/campaigns/', campaign_body(
            template, audience={'role': 'drivers', 'cities': ['IN-TG-WGL']}), expect=404)
        assert error_code(response) == 'NOT_FOUND'
        post(client, f'{ADM}/campaigns/', campaign_body(
            template, audience={'role': 'drivers', 'cities': ['NOPE']}), expect=404)

    def test_cities_in_two_countries_are_refused(self, world, promo):
        template, _ = promo
        client, _ = world.super
        post(client, f'{ADM}/campaigns/', campaign_body(
            template, audience={'role': 'drivers', 'cities': ['IN-TG-HYD', 'US-TX-AUS']}), expect=400)

    def test_the_cities_endpoint_lists_only_the_callers_cities(self, world, promo):
        client, _ = world.mkt_hyd
        codes = [c['code'] for c in (get(client, f'{ADM}/cities/'))['cities']]
        assert codes == ['IN-TG-HYD']

    def test_the_whole_flow_submit_approve_send(self, world, promo):
        template, users = promo
        campaign = self.create(world, promo)
        assert campaign['status'] == CAMPAIGN_DRAFT and campaign['scope'] == 'IN-TG-HYD'
        client, maker = world.mkt
        response = post(client, f'{ADM}/campaigns/{campaign["id"]}/submit/', {}, expect=202)
        request = ApprovalRequest.objects.get(pk=rid(response))
        assert request.action_key == 'notification.campaign_send'
        assert request.payload['funnel']['reachable'] == 1
        assert request.payload['template_key'] == 'promo.weekend'
        assert Campaign.objects.get(pk=campaign['id']).status == CAMPAIGN_PENDING
        assert not NotificationMessage.objects.exists()               # nothing before a checker

        assert approve_request(client, request.pk).status_code == 403  # not by the maker
        approve(request.pk)
        row = Campaign.objects.get(pk=campaign['id'])
        assert row.status == CAMPAIGN_SCHEDULED

        assert campaign_workflow.start(row.pk) == 'sent'
        row.refresh_from_db()
        assert row.status == CAMPAIGN_SENT and row.queued_count == 1 and row.suppressed_count == 0
        messages = NotificationMessage.objects.filter(campaign=row)
        assert [m.recipient_user_id for m in messages] == [users['a'].pk]
        assert messages[0].source == f'campaign:{row.pk}' and messages[0].category == 'promotional'
        # idempotent: a resumed run adds nothing
        campaign_workflow.Campaign.objects.filter(pk=row.pk).update(status=CAMPAIGN_SCHEDULED)
        campaign_workflow.start(row.pk)
        assert NotificationMessage.objects.filter(campaign=row).count() == 1
        actions = set(AdminAuditLog.objects.filter(target_type='notification_campaign')
                      .values_list('action', flat=True))
        assert {'notification_campaign_created', 'notification_campaign_submitted',
                'notification_campaign_approved', 'notification_campaign_sent'} <= actions

    def test_a_promotional_send_in_quiet_hours_is_held(self, world, promo):
        from datetime import datetime, timezone as dt_utc
        template, users = promo
        campaign = self.create(world, promo)
        client, _ = world.mkt
        approve(rid(post(client, f'{ADM}/campaigns/{campaign["id"]}/submit/', {}, expect=202)))
        night = datetime(2026, 11, 3, 17, 0, tzinfo=dt_utc.utc)
        Campaign.objects.filter(pk=campaign['id']).update(scheduled_at=night)
        campaign_workflow.start(campaign['id'], now=night)
        message = NotificationMessage.objects.get(campaign_id=campaign['id'])
        assert message.status == 'queued' and message.not_before.hour == 4   # 10:00 IST in UTC

    def test_a_scheduled_campaign_waits_for_its_time(self, world, promo):
        campaign = self.create(world, promo, scheduled_at=(timezone.now() + timedelta(days=2)).isoformat())
        client, _ = world.mkt
        approve(rid(post(client, f'{ADM}/campaigns/{campaign["id"]}/submit/', {}, expect=202)))
        assert campaign_workflow.start(campaign['id']) == 'skipped'
        assert campaign_workflow.due_ids() == []
        later = timezone.now() + timedelta(days=3)
        assert campaign_workflow.due_ids(later) == [campaign['id']]

    def test_a_campaign_with_nobody_reachable_cannot_be_submitted(self, world, promo):
        template, users = promo
        for user in users.values():
            preferences.set_preference(user, 'push', 'promotional', False, 'app')
        campaign = self.create(world, promo)
        client, _ = world.mkt
        assert error_code(post(client, f'{ADM}/campaigns/{campaign["id"]}/submit/', {},
                               expect=400)) == 'EMPTY_AUDIENCE'

    def test_a_rejected_request_returns_the_campaign_to_draft(self, world, promo):
        campaign = self.create(world, promo)
        client, _ = world.mkt
        request_id = rid(post(client, f'{ADM}/campaigns/{campaign["id"]}/submit/', {}, expect=202))
        chk, _ = checker()
        post(chk, f'{APPROVALS}/{request_id}/reject/', {'reason': 'too broad'}, expect=200)
        assert Campaign.objects.get(pk=campaign['id']).status == CAMPAIGN_DRAFT

    def test_cancel_is_allowed_until_sending_starts(self, world, promo):
        client, _ = world.mkt
        # draft
        a = self.create(world, promo, name='a')
        post(client, f'{ADM}/campaigns/{a["id"]}/cancel/', {}, expect=200)
        # pending: the approval request is cancelled with it
        b = self.create(world, promo, name='b')
        request_id = rid(post(client, f'{ADM}/campaigns/{b["id"]}/submit/', {}, expect=202))
        post(client, f'{ADM}/campaigns/{b["id"]}/cancel/', {}, expect=200)
        assert ApprovalRequest.objects.get(pk=request_id).status == 'cancelled'
        # scheduled
        c = self.create(world, promo, name='c')
        approve(rid(post(client, f'{ADM}/campaigns/{c["id"]}/submit/', {}, expect=202)))
        post(client, f'{ADM}/campaigns/{c["id"]}/cancel/', {}, expect=200)
        assert campaign_workflow.start(c['id']) == 'skipped'
        assert {Campaign.objects.get(pk=x['id']).status for x in (a, b, c)} == {CAMPAIGN_CANCELLED}
        # sending / sent: too late
        d = self.create(world, promo, name='d')
        approve(rid(post(client, f'{ADM}/campaigns/{d["id"]}/submit/', {}, expect=202)))
        campaign_workflow.start(d['id'])
        assert error_code(post(client, f'{ADM}/campaigns/{d["id"]}/cancel/', {},
                               expect=409)) == 'CAMPAIGN_NOT_CANCELLABLE'

    def test_a_template_retired_while_pending_makes_the_approval_fail_safely(self, world, promo):
        template, _ = promo
        campaign = self.create(world, promo)
        client, _ = world.mkt
        request_id = rid(post(client, f'{ADM}/campaigns/{campaign["id"]}/submit/', {}, expect=202))
        NotificationTemplate.objects.filter(pk=template.pk).update(version=2)   # changed since
        chk, _ = checker()
        approve_request(chk, request_id)
        assert ApprovalRequest.objects.get(pk=request_id).error_code == 'TEMPLATE_CHANGED'
        assert Campaign.objects.get(pk=campaign['id']).status == CAMPAIGN_PENDING

    def test_only_a_draft_can_be_edited_and_edits_are_audited(self, world, promo):
        campaign = self.create(world, promo)
        client, _ = world.mkt
        edited = body_of(patch(client, f'{ADM}/campaigns/{campaign["id"]}/', {'name': 'Renamed'},
                               expect=200))
        assert edited['name'] == 'Renamed'
        assert AdminAuditLog.objects.filter(action='notification_campaign_updated').exists()
        post(client, f'{ADM}/campaigns/{campaign["id"]}/submit/', {}, expect=202)
        patch(client, f'{ADM}/campaigns/{campaign["id"]}/', {'name': 'Late'}, expect=409)

    def test_scope_a_campaign_is_invisible_outside_its_anchor(self, world, promo):
        campaign = self.create(world, promo, who='mkt')
        cid = campaign['id']
        us_client, _ = world.mkt_us
        raw(us_client, f'{ADM}/campaigns/{cid}/', expect=404)
        post(us_client, f'{ADM}/campaigns/{cid}/submit/', {}, expect=404)
        post(us_client, f'{ADM}/campaigns/{cid}/cancel/', {}, expect=404)
        assert (get(us_client, f'{ADM}/campaigns/'))['results'] == []
        support, _ = world.support                    # HYD support: the campaign is anchored at HYD
        assert (get(support, f'{ADM}/campaigns/{cid}/'))['id'] == cid
        assert post(support, f'{ADM}/campaigns/{cid}/cancel/', {}).status_code == 403

    def test_the_detail_carries_the_funnel_and_message_counts(self, world, promo):
        campaign = self.create(world, promo)
        client, _ = world.mkt
        data = (get(client, f'{ADM}/campaigns/{campaign["id"]}/'))
        assert data['funnel']['reachable'] == 1 and data['message_counts'] == {}


# ===========================================================================
# THE DELIVERY LOG
# ===========================================================================

class TestDeliveryLog:
    @pytest.fixture
    def logged(self, world):
        geo = world.geo
        hyd = make_driver_user(geo, 'IN-TG-HYD', phone='+919700002001', email='h1@example.test')
        aus = make_driver_user(geo, 'US-TX-AUS', phone='+15125552001', email='a1@example.test')
        t_in = make_template(geo, channel='sms')
        t_us = make_template(geo, key='test.us', channel='email', country='US', subject='s', body='b',
                             variables=())
        rows = {}
        for name, user, template, provider, status in (
                ('hyd_sms', hyd, t_in, 'msg91', 'delivered'),
                ('hyd_failed', hyd, t_in, 'msg91', 'failed'),
                ('aus_email', aus, t_us, 'ses', 'sent')):
            from servers.notifications import recipients
            rows[name] = NotificationMessage.objects.create(
                template=template, template_key=template.key, template_version=1,
                channel=template.channel, country=template.country,
                city=recipients.city_of(user), recipient_user_id=user.pk,
                recipient_masked=recipients.masked_for(user, template.channel), provider=provider,
                provider_message_id=f'pm-{name}', status=status, idempotency_key=f'log:{name}',
                source='test.event', body='Hello world')
            rows[name].events.create(status='queued')
            rows[name].events.create(status=status, source='webhook:msg91')
        return rows, hyd, aus

    def test_the_log_is_scoped_by_the_recipients_city(self, world, logged):
        rows, _, _ = logged
        support, _ = world.support
        ids = {m['id'] for m in (get(support, f'{ADM}/messages/'))['results']}
        assert ids == {rows['hyd_sms'].id, rows['hyd_failed'].id}
        raw(support, f'{ADM}/messages/{rows["aus_email"].id}/', expect=404)
        sup, _ = world.super
        assert (get(sup, f'{ADM}/messages/'))['count'] == 3
        us, _ = world.mkt_us
        assert [m['id'] for m in (get(us, f'{ADM}/messages/'))['results']] == [rows['aus_email'].id]

    def test_the_filters(self, world, logged):
        rows, hyd, aus = logged
        sup, _ = world.super

        def ids(query):
            return {m['id'] for m in (get(sup, f'{ADM}/messages/?{query}'))['results']}

        assert ids('status=failed') == {rows['hyd_failed'].id}
        assert ids('channel=email') == {rows['aus_email'].id}
        assert ids('provider=msg91') == {rows['hyd_sms'].id, rows['hyd_failed'].id}
        assert ids('template=test.us') == {rows['aus_email'].id}
        assert ids(f'user_id={hyd.pk}') == {rows['hyd_sms'].id, rows['hyd_failed'].id}
        assert ids(f'user_id={aus.pk}&status=sent') == {rows['aus_email'].id}
        assert ids('status=queued') == set()
        raw(sup, f'{ADM}/messages/?user_id=abc', expect=400)

    def test_recipients_are_masked_and_nothing_raw_is_returned(self, world, logged):
        rows, hyd, _ = logged
        sup, _ = world.super
        text = raw(sup, f'{ADM}/messages/{rows["hyd_sms"].id}/').content.decode()
        assert '+91••••••2001' in text
        for secret in ('9700002001', 'h1@example.test', '15125552001', 'a1@example.test'):
            assert secret not in text

    def test_the_detail_has_the_status_timeline(self, world, logged):
        rows, _, _ = logged
        sup, _ = world.super
        data = (get(sup, f'{ADM}/messages/{rows["hyd_sms"].id}/'))
        assert [e['status'] for e in data['timeline']] == ['queued', 'delivered']
        assert data['timeline'][1]['source'] == 'webhook:msg91'

    def test_the_list_is_bounded(self, world, logged):
        rows, hyd, _ = logged
        for i in range(120):
            NotificationMessage.objects.create(
                template_key='bulk', channel='sms', country='IN', city=rows['hyd_sms'].city,
                recipient_user_id=hyd.pk, idempotency_key=f'bulk:{i}', status='sent')
        sup, _ = world.super
        data = (get(sup, f'{ADM}/messages/?limit=1000'))
        assert data['limit'] == 100 and len(data['results']) == 100 and data['count'] == 123


# ===========================================================================
# PROVIDERS
# ===========================================================================

class TestProviders:
    def test_the_page_shows_state_and_never_a_secret(self, world, settings, fake_credentials):
        settings.NOTIFY_LIVE_SEND = False
        settings.NOTIFY_TEST_RECIPIENTS = ['a@example.test', '+919700000000']
        client, _ = world.mkt
        response = raw(client, f'{ADM}/providers/')
        text = response.content.decode()
        for secret in fake_credentials.values():
            assert secret not in text
        data = response.json()['data']
        assert data['live_send'] is False and data['allowlist_size'] == 2
        rows = {(r['channel'], r['country']): r for r in data['providers']}
        assert rows[('sms', 'IN')]['provider'] == 'msg91' and rows[('sms', 'IN')]['mode'] == 'stub'
        assert rows[('sms', 'IN')]['credentials_present'] is True
        assert rows[('sms', 'US')]['provider'] == 'twilio'
        assert rows[('in_app', 'IN')]['internal'] is True
        assert 'MSG91_AUTH_KEY' in rows[('sms', 'IN')]['credential_names']
        assert 'a@example.test' not in text and '9700000000' not in text      # size only

    def test_credentials_present_is_false_without_them_and_live_follows_the_flag(self, world, settings):
        settings.NOTIFY_LIVE_SEND = True
        settings.IS_PRODUCTION = False
        client, _ = world.mkt
        data = (get(client, f'{ADM}/providers/'))
        rows = {(r['channel'], r['country']): r for r in data['providers']}
        assert rows[('sms', 'IN')]['mode'] == 'live' and rows[('sms', 'IN')]['credentials_present'] is False
        assert data['allowlist_applies'] is True


# ===========================================================================
# PREFERENCES: the app's own, and an admin's opt-out
# ===========================================================================

class TestPreferencesApi:
    def test_the_app_reads_and_writes_its_own_preferences(self, geo):
        user = make_rider_user(geo)
        client = client_for(user)
        data = client.get(f'{API}/preferences/').json()['data']
        assert data['effective']['sms']['transactional'] is True
        assert data['effective']['whatsapp']['transactional'] is False
        assert data['effective']['push']['promotional'] is False
        assert data['locked'] == [{'channel': 'in_app', 'category': 'transactional'}]
        out = client.put(f'{API}/preferences/', {'preferences': [
            {'channel': 'whatsapp', 'category': 'transactional', 'opted_in': True},
            {'channel': 'push', 'category': 'promotional', 'opted_in': True}]}, format='json')
        assert out.status_code == 200
        assert out.json()['data']['effective']['whatsapp']['transactional'] is True
        row = user.channel_preferences.get(channel='whatsapp', category='transactional')
        assert row.source == 'app' and row.opted_in

    def test_in_app_transactional_cannot_be_switched_off_and_junk_is_400(self, geo):
        client = client_for(make_rider_user(geo))
        bad = client.put(f'{API}/preferences/', {'channel': 'in_app', 'category': 'transactional',
                                                  'opted_in': False}, format='json')
        assert bad.status_code == 400 and bad.json()['error']['code'] == 'NOT_ALLOWED'
        for body in ({}, {'preferences': []}, {'channel': 'sms', 'category': 'x', 'opted_in': True},
                     {'channel': 'sms', 'category': 'promotional', 'opted_in': 'yes'}):
            assert client.put(f'{API}/preferences/', body, format='json').status_code == 400

    def test_operators_and_anonymous_have_no_app_preferences(self, world):
        client, _ = world.super
        assert client.get(f'{API}/preferences/').status_code == 403
        from rest_framework.test import APIClient
        assert APIClient().get(f'{API}/preferences/').status_code in (401, 403)

    def test_the_driver_app_uses_the_same_endpoint(self, geo):
        client = client_for(make_driver_user(geo))
        assert client.get(f'{API}/preferences/').status_code == 200


class TestAdminPreferences:
    @pytest.fixture
    def person(self, world):
        return make_rider_user(world.geo, 'IN-TG-HYD', phone='+919700003001')

    def test_support_can_read_but_not_change(self, world, person):
        client, _ = world.support
        data = (get(client, f'{ADM}/users/{person.pk}/preferences/'))
        assert data['effective']['sms']['transactional'] is True
        assert put(client, f'{ADM}/users/{person.pk}/preferences/', {}).status_code == 403

    def test_marketing_can_only_opt_a_user_out_with_a_reason_and_it_is_audited(self, world, person):
        client, _ = world.mkt
        path = f'{ADM}/users/{person.pk}/preferences/'
        body = {'channel': 'email', 'category': 'promotional', 'opted_in': False}
        assert error_code(put(client, path, {**body, 'opted_in': True}, expect=400)) == 'OPT_IN_NOT_ALLOWED'
        assert error_code(put(client, path, body, expect=400)) == 'REASON_REQUIRED'
        data = body_of(put(client, path, {**body, 'reason': 'Rider asked on a call'}, expect=200))
        assert data['effective']['email']['promotional'] is False
        row = AdminAuditLog.objects.get(action='notification_preference_changed_by_admin')
        assert row.target_id == str(person.pk) and row.reason == 'Rider asked on a call'
        assert row.actor_email and row.actor_label
        assert preferences.is_opted_in(person.pk, 'email', 'promotional')[0] is False
        assert person.channel_preferences.get(channel='email', category='promotional').source == 'admin'

    def test_in_app_transactional_cannot_be_opted_out_by_an_admin_either(self, world, person):
        client, _ = world.mkt
        response = put(client, f'{ADM}/users/{person.pk}/preferences/', {
            'channel': 'in_app', 'category': 'transactional', 'opted_in': False, 'reason': 'x'})
        assert response.status_code == 400

    def test_a_user_outside_the_callers_scope_is_a_404(self, world, person):
        client, _ = world.mkt_us
        raw(client, f'{ADM}/users/{person.pk}/preferences/', expect=404)
        put(client, f'{ADM}/users/{person.pk}/preferences/', {}, expect=404)
        raw(client, f'{ADM}/users/999999/preferences/', expect=404)


# ===========================================================================
# THE COMMAND CENTRE ALERT
# ===========================================================================

class TestProviderFailingAlert:
    def messages(self, geo, failed, sent, provider='msg91', channel='sms'):
        from servers.notifications import recipients  # noqa: F401
        user = make_driver_user(geo, 'IN-TG-HYD')
        for i in range(failed + sent):
            NotificationMessage.objects.create(
                template_key='t', channel=channel, country='IN', recipient_user_id=user.pk,
                provider=provider, status='failed' if i < failed else 'sent',
                idempotency_key=f'h:{provider}:{channel}:{failed}:{sent}:{i}')

    def alerts(self, client):
        response = client.get('/api/v1/command-centre/admin/alerts/?kind=notify.provider_failing')
        if response.status_code == 403:         # no report.read: no alerts page at all
            return []
        assert response.status_code == 200
        return response.json()['data']['results']

    def test_a_high_failure_rate_raises_one_warning_visible_to_super_admin_only(self, world):
        from servers.command_centre.models import OperatorAlert
        from servers.notifications import health
        self.messages(world.geo, failed=6, sent=6)
        result = health.check()
        assert len(result['raised']) == 1
        alert = OperatorAlert.objects.get(kind='notify.provider_failing')
        assert alert.severity == 'warning' and alert.payload['provider'] == 'msg91'
        assert alert.payload['failed'] == 6 and alert.payload['total'] == 12
        assert 'Hello' not in str(alert.payload)
        sup, _ = world.super
        assert len(self.alerts(sup)) == 1
        for who in ('ops', 'mkt', 'support', 'finance'):
            client, _ = getattr(world, who)
            assert self.alerts(client) == [], who
        # a second run raises no second alert
        health.check()
        assert OperatorAlert.objects.filter(kind='notify.provider_failing').count() == 1

    def test_it_clears_itself_when_the_rate_recovers(self, world):
        from servers.command_centre.models import OperatorAlert
        from servers.notifications import health
        self.messages(world.geo, failed=6, sent=6)
        health.check()
        NotificationMessage.objects.all().update(
            created_at=timezone.now() - timedelta(minutes=30))
        assert len(health.check()['cleared']) == 1
        assert OperatorAlert.objects.get(kind='notify.provider_failing').acknowledged_at is not None

    def test_a_small_sample_or_a_low_rate_raises_nothing(self, world):
        from servers.notifications import health
        self.messages(world.geo, failed=2, sent=0)                       # 100% of 2
        self.messages(world.geo, failed=1, sent=20, provider='twilio')   # 5% of 21
        assert health.check() == {'raised': [], 'cleared': []}

    def test_the_stub_and_in_app_are_never_measured(self, world):
        from servers.notifications import health
        self.messages(world.geo, failed=15, sent=0, provider='stub')
        assert health.measure() == []

    def test_the_threshold_is_config(self, world, settings):
        from servers.notifications import health
        self.messages(world.geo, failed=4, sent=8)                       # 33%
        settings.NOTIFY_FAILURE_RATE_THRESHOLD = 0.5
        assert health.check()['raised'] == []
        settings.NOTIFY_FAILURE_RATE_THRESHOLD = 0.3
        assert len(health.check()['raised']) == 1

    def test_the_dashboard_tile_does_not_count_it(self, world):
        from servers.command_centre.services import open_alert_count
        from servers.notifications import health
        self.messages(world.geo, failed=6, sent=6)
        health.check()
        assert open_alert_count() == 0

    def test_it_can_be_acknowledged_by_a_super_admin_and_by_nobody_else(self, world):
        from servers.notifications import health
        self.messages(world.geo, failed=6, sent=6)
        health.check()
        sup, _ = world.super
        alert_id = self.alerts(sup)[0]['id']
        ops, _ = world.ops
        assert post(ops, f'/api/v1/command-centre/admin/alerts/{alert_id}/acknowledge/', {}).status_code == 404
        post(sup, f'/api/v1/command-centre/admin/alerts/{alert_id}/acknowledge/', {}, expect=200)


def test_geo_fixture_has_both_countries(geo):
    assert {'IN', 'US', 'IN-TG-HYD', 'US-TX-AUS'} <= set(GeoNode.objects.values_list('code', flat=True))
