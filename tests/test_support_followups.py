"""Support follow-ups (AUDIT 27.36), one class per numbered item.

  1. WAITING_USER pauses the RESOLUTION clock only; safety never pauses or
     auto-closes; the hourly sweep reminds once and auto-closes per country
     config, and does nothing for a country with no config row.
  2. A refund approval linked to a ticket that is rejected / cancelled /
     expired goes back to the agent: internal note with the reason, inbox
     item, no rider-facing message.
  3. The rider-facing refund line is neutral and never names an internal step.
  4. support_lead template, versioned macros with restore, the SLA dashboard,
     and SLA / country-config changes that need a checker.
  5. Every new admin GET is scoped (the structural guards and the PII sweep
     inventory carry them).
"""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from conftest import seed_actor
from servers.admin_access.roles import ROLE_TEMPLATES, validate_templates
from servers.admin_audit.models import AdminAuditLog
from servers.approvals.models import REQUESTED, ApprovalRequest
from servers.support import waiting
from servers.support.models import (
    OperatorNotification, SupportCountryConfig, SupportMacro, SupportMacroVersion,
    SupportMessage, SupportSlaPolicy, SupportTicket,
)
from servers.support.sla import apply_due_times, refund_review_sentence, sla_state
from test_sos_support_zones_matrix import (  # noqa: F401 - fixtures and helpers
    _client_for, geo, operator,
)
from test_support_module import (  # noqa: F401 - fixtures and helpers
    BASE, LIST, MY_TICKETS, audit, mk, policies, post, url, world,
)

pytestmark = pytest.mark.django_db

HOURS = timedelta(hours=1)
DAYS = timedelta(days=1)


def config(country='IN', reminder=2, autoclose=7, hours=72):
    return SupportCountryConfig.objects.create(
        country=country, waiting_reminder_days=reminder, waiting_autoclose_days=autoclose,
        refund_review_hours=hours)


def put_waiting(ticket, since):
    """Put a ticket in WAITING_USER since `since` (what the status write does)."""
    SupportTicket.objects.filter(pk=ticket.pk).update(
        status='WAITING_USER', waiting_since=since)
    ticket.refresh_from_db()
    return ticket


def rider_view(user, ticket):
    response = _client_for(user).get(f'{MY_TICKETS}{ticket.id}/')
    assert response.status_code == 200, response.content
    return response


# ===========================================================================
# 1. Waiting on the user pauses the resolution clock only
# ===========================================================================

class TestPause:
    def test_leaving_waiting_pushes_resolution_out_by_the_paused_time(self, geo, world):
        client, _ = operator('support_agent', geo)
        ticket = world['t_hyd']
        due0, first0 = ticket.resolution_due_at, ticket.first_response_due_at
        assert post(client, ticket, 'status/', {'status': 'WAITING_USER'}).status_code == 200
        ticket.refresh_from_db()
        assert ticket.waiting_since is not None and ticket.resolution_due_at == due0
        # Pretend it has been waiting for three hours.
        SupportTicket.objects.filter(pk=ticket.pk).update(
            waiting_since=timezone.now() - 3 * HOURS)
        assert post(client, ticket, 'status/', {'status': 'IN_PROGRESS'}).status_code == 200
        ticket.refresh_from_db()
        shift = ticket.resolution_due_at - due0
        assert abs(shift - 3 * HOURS) < timedelta(seconds=30)
        assert abs(ticket.resolution_paused_seconds - 3 * 3600) < 30
        assert ticket.waiting_since is None
        # First response never pauses.
        assert ticket.first_response_due_at == first0
        row = audit(ticket, 'support_ticket_status_changed').order_by('-id').first()
        assert row.before['waiting_since'] and row.after['waiting_since'] is None

    def test_a_paused_ticket_shows_paused_not_breaching(self, geo, world):
        client, _ = operator('support_agent', geo)
        ticket = world['t_hyd']
        # Answered in time, so only the resolution clock is open; and late.
        now = timezone.now()
        SupportTicket.objects.filter(pk=ticket.pk).update(
            first_responded_at=now, resolution_due_at=now - HOURS)
        ticket.refresh_from_db()
        assert sla_state(ticket, now)['state'] == 'breached'
        assert post(client, ticket, 'status/', {'status': 'WAITING_USER'}).status_code == 200
        data = client.get(url(ticket)).json()['data']
        assert data['sla']['state'] == 'paused' and data['sla']['paused'] is True
        assert data['sla']['breached'] is False
        assert data['sla']['resolution']['paused'] is True
        ids = {r['id'] for r in client.get(LIST + '?breach=breached').json()['data']['results']}
        assert ticket.id not in ids
        assert client.get(LIST).json()['data']['queues']['breaching'] == 0

    def test_first_response_keeps_running_while_waiting(self, geo, world):
        client, _ = operator('support_agent', geo)
        ticket = world['t_hyd']
        SupportTicket.objects.filter(pk=ticket.pk).update(
            first_response_due_at=timezone.now() - HOURS)
        post(client, ticket, 'status/', {'status': 'WAITING_USER'})
        sla = client.get(url(ticket)).json()['data']['sla']
        assert sla['state'] == 'breached' and sla['paused'] is True

    def test_the_user_replying_through_the_app_ends_the_pause(self, geo, world):
        ticket = world['t_hyd']
        due0 = ticket.resolution_due_at
        put_waiting(ticket, timezone.now() - 2 * HOURS)
        r = _client_for(world['hyd_rider']).post(
            f'{MY_TICKETS}{ticket.id}/messages/', {'body': 'Here is the screenshot'},
            format='json')
        assert r.status_code == 201, r.content
        ticket.refresh_from_db()
        assert ticket.status == 'IN_PROGRESS' and ticket.waiting_since is None
        assert abs((ticket.resolution_due_at - due0) - 2 * HOURS) < timedelta(seconds=30)

    def test_a_priority_change_keeps_the_time_already_paused(self, geo, world):
        client, _ = operator('support_agent', geo)
        ticket = world['t_hyd']
        put_waiting(ticket, timezone.now() - HOURS)
        post(client, ticket, 'status/', {'status': 'IN_PROGRESS'})
        ticket.refresh_from_db()
        paused = ticket.resolution_paused_seconds
        assert paused > 0
        post(client, ticket, 'priority/', {'priority': 'high'})
        ticket.refresh_from_db()
        expected = ticket.created_at + timedelta(minutes=1440, seconds=paused)
        assert ticket.resolution_due_at == expected

    def test_safety_never_pauses(self, geo, world):
        client, _ = operator('support_agent', geo)
        ticket = mk(world['hyd_rider'], world['hyd_trip'], 'safety', 'I felt unsafe')
        assert ticket.priority == 'urgent' and ticket.is_safety
        assert post(client, ticket, 'status/', {'status': 'WAITING_USER'}).status_code == 200
        ticket.refresh_from_db()
        assert ticket.status == 'WAITING_USER' and ticket.waiting_since is None
        sla = client.get(url(ticket)).json()['data']['sla']
        assert sla['paused'] is False and sla['state'] != 'paused'
        # Re-categorised later: still a safety ticket.
        post(client, ticket, 'category/', {'issue_type': 'other'})
        ticket.refresh_from_db()
        assert ticket.is_safety
        post(client, ticket, 'status/', {'status': 'IN_PROGRESS'})
        post(client, ticket, 'status/', {'status': 'WAITING_USER'})
        ticket.refresh_from_db()
        assert ticket.waiting_since is None


class TestWaitingSweep:
    def test_one_reminder_after_n_days_and_never_a_second(self, geo, world):
        config()
        ticket = put_waiting(world['t_hyd'], timezone.now() - 3 * DAYS)
        assert waiting.sweep_waiting() == {'reminded': 1, 'closed': 0}
        assert waiting.sweep_waiting() == {'reminded': 0, 'closed': 0}
        ticket.refresh_from_db()
        assert ticket.reminder_sent_at is not None and ticket.status == 'WAITING_USER'
        reminders = SupportMessage.everything.filter(
            ticket=ticket, author_role='system', body=waiting.REMINDER_TEXT)
        assert reminders.count() == 1
        # The user sees it, and it is neutral.
        body = rider_view(world['hyd_rider'], ticket).json()['data']['messages']
        assert waiting.REMINDER_TEXT in [m['body'] for m in body]
        row = audit(ticket, 'support_ticket_reminded').get()
        assert row.actor_id is None and row.actor_label == waiting.SYSTEM_LABEL

    def test_not_before_n_days(self, geo, world):
        config()
        ticket = put_waiting(world['t_hyd'], timezone.now() - 1 * DAYS)
        assert waiting.sweep_waiting() == {'reminded': 0, 'closed': 0}
        ticket.refresh_from_db()
        assert ticket.reminder_sent_at is None

    def test_auto_close_after_m_days(self, geo, world):
        config()
        ticket = put_waiting(world['t_hyd'], timezone.now() - 8 * DAYS)
        assert waiting.sweep_waiting() == {'reminded': 0, 'closed': 1}
        ticket.refresh_from_db()
        assert ticket.status == 'CLOSED' and ticket.resolved_at is not None
        assert SupportMessage.everything.filter(
            ticket=ticket, author_role='system', body=waiting.AUTOCLOSE_TEXT).count() == 1
        row = audit(ticket, 'support_ticket_autoclosed').get()
        assert row.actor_id is None and row.actor_label == waiting.SYSTEM_LABEL
        assert row.before['status'] == 'WAITING_USER' and row.after['status'] == 'CLOSED'
        assert waiting.sweep_waiting() == {'reminded': 0, 'closed': 0}
        assert audit(ticket, 'support_ticket_autoclosed').count() == 1

    def test_no_config_means_no_reminder_and_no_auto_close(self, geo, world):
        ticket = put_waiting(world['t_hyd'], timezone.now() - 30 * DAYS)
        assert SupportCountryConfig.objects.count() == 0
        assert waiting.sweep_waiting() == {'reminded': 0, 'closed': 0}
        ticket.refresh_from_db()
        assert ticket.status == 'WAITING_USER' and ticket.reminder_sent_at is None
        # Another country's config does not apply to this one.
        config('US')
        assert waiting.sweep_waiting() == {'reminded': 0, 'closed': 0}

    def test_safety_is_never_reminded_or_closed(self, geo, world):
        config()
        ticket = mk(world['hyd_rider'], world['hyd_trip'], 'safety', 'unsafe')
        SupportTicket.objects.filter(pk=ticket.pk).update(
            status='WAITING_USER', waiting_since=timezone.now() - 30 * DAYS)
        assert waiting.sweep_waiting() == {'reminded': 0, 'closed': 0}
        ticket.refresh_from_db()
        assert ticket.status == 'WAITING_USER'

    def test_the_task_is_registered_and_scheduled_hourly(self):
        from base.settings import CELERY_BEAT_SCHEDULE
        from servers.support import tasks

        assert tasks.sweep_waiting_tickets.name == 'support.sweep_waiting_tickets'
        entry = [v for v in CELERY_BEAT_SCHEDULE.values()
                 if v['task'] == 'support.sweep_waiting_tickets']
        assert len(entry) == 1

    def test_seed_writes_country_config_only_where_absent(self, geo):
        actor = seed_actor()
        call_command('seed_support_sla', actor_email=actor.email, stdout=StringIO())
        rows = {c.country: c for c in SupportCountryConfig.objects.all()}
        assert set(rows) == {'IN', 'US'}
        assert (rows['IN'].waiting_reminder_days, rows['IN'].waiting_autoclose_days,
                rows['IN'].refund_review_hours) == (2, 7, 72)
        SupportCountryConfig.objects.filter(country='US').update(refund_review_hours=24)
        call_command('seed_support_sla', actor_email=actor.email, stdout=StringIO())
        assert SupportCountryConfig.objects.get(country='US').refund_review_hours == 24
        from django.core.management.base import CommandError

        with pytest.raises(CommandError):
            call_command('seed_support_sla', stdout=StringIO())


# ===========================================================================
# 2. + 3. Refund approvals that are turned down; the rider-facing wording
# ===========================================================================

FORBIDDEN_WORDS = ('approval', 'approve', 'checker', 'maker', 'pending approval')


def over_limit_refund(client, world, key='r1', amount=60_000):
    from servers.ride.models import Trip

    ticket = world['t_hyd']
    Trip.objects.filter(pk=ticket.trip_id_id).update(
        estimated_fare_minor=90_000, final_fare_minor=90_000)
    r = client.post(
        f'/api/v1/ride/admin/trips/{ticket.trip_id_id}/refund/',
        {'amount_minor': amount, 'reason': 'fare dispute', 'idempotency_key': key,
         'ticket_id': ticket.id}, format='json')
    assert r.status_code == 202, r.content
    return ApprovalRequest.objects.get(pk=r.json()['data']['approval_request']['id'])


def decide(checker, request, verb, reason='Not supported by the trip record'):
    return checker.post(f'/api/v1/approvals/admin/requests/{request.id}/{verb}/',
                        {'reason': reason}, format='json')


class TestRejectedRefund:
    def test_a_rejected_refund_returns_the_ticket_to_the_agent(self, geo, world, policies):
        config()
        agent_client, agent = operator('support_agent', geo)
        ticket = world['t_hyd']
        request = over_limit_refund(agent_client, world)
        before_msgs = list(SupportMessage.objects.filter(ticket=ticket).values_list(
            'id', flat=True))
        checker, checker_user = operator('super_admin', geo)
        reason = 'Amount is not supported by the trip record'
        assert decide(checker, request, 'reject', reason).status_code == 200
        ticket.refresh_from_db()
        assert ticket.assigned_to_id == agent.id and ticket.status == 'IN_PROGRESS'
        assert ticket.returned_reason == reason and ticket.returned_at is not None
        # An INTERNAL note carries the reason...
        note = SupportMessage.everything.filter(ticket=ticket, author_role='internal').get()
        assert reason in note.body and note.author_id == checker_user.id
        # ...and the rider thread gained nothing at all.
        after = list(SupportMessage.objects.filter(ticket=ticket).values_list('id', flat=True))
        assert after == before_msgs
        # The agent has an inbox item and the queue shows it as returned.
        inbox = agent_client.get(BASE + 'inbox/').json()['data']
        assert inbox['unread'] == 1 and inbox['results'][0]['ticket_id'] == ticket.id
        assert reason in inbox['results'][0]['text']
        queue = agent_client.get(LIST + '?returned=me')
        assert [r['id'] for r in queue.json()['data']['results']] == [ticket.id]
        assert queue.json()['data']['queues']['returned'] == 1
        detail = agent_client.get(url(ticket)).json()['data']
        assert detail['returned_reason'] == reason and detail['returned_at']
        # Audited, with the approver as actor.
        row = audit(ticket, 'support_ticket_refund_returned').get()
        assert row.actor_id == checker_user.id and row.after['outcome'] == 'rejected'
        # Replying picks the ticket up again.
        post(agent_client, ticket, 'reply/', {'body': 'Sorry, we cannot refund that amount'})
        ticket.refresh_from_db()
        assert ticket.returned_at is None
        assert agent_client.get(BASE + 'inbox/').json()['data']['unread'] == 1  # until read
        marked = agent_client.post(BASE + 'inbox/read/', {'all': True}, format='json')
        assert marked.json()['data'] == {'marked': 1, 'unread': 0}
        again = agent_client.post(BASE + 'inbox/read/', {'all': True}, format='json')
        assert again.json()['data']['marked'] == 0
        assert AdminAuditLog.objects.filter(action='support_inbox_read').count() == 1

    def test_a_cancelled_refund_returns_too(self, geo, world, policies):
        agent_client, agent = operator('support_agent', geo)
        request = over_limit_refund(agent_client, world)
        from servers.approvals import service as approvals

        # (The cancel endpoint needs report.read, which an agent lacks; the
        # service is what the hook sits in, so call it as the maker.)
        approvals.cancel(request, agent, reason='Wrong amount')
        ticket = world['t_hyd']
        ticket.refresh_from_db()
        assert ticket.returned_at is not None and ticket.assigned_to_id == agent.id
        assert audit(ticket, 'support_ticket_refund_returned').get().after['outcome'] == 'cancelled'

    def test_an_expired_refund_returns_too(self, geo, world, policies):
        from servers.approvals import service as approvals

        agent_client, agent = operator('support_agent', geo)
        request = over_limit_refund(agent_client, world)
        ApprovalRequest.objects.filter(pk=request.pk).update(
            expires_at=timezone.now() - HOURS)
        assert approvals.expire_stale() == 1
        ticket = world['t_hyd']
        ticket.refresh_from_db()
        assert ticket.returned_at is not None and ticket.assigned_to_id == agent.id
        assert 'expired' in ticket.returned_reason
        assert OperatorNotification.objects.filter(recipient=agent, ticket=ticket).count() == 1

    def test_a_refund_with_no_ticket_does_nothing_extra(self, geo, world, policies):
        from servers.ride.models import Trip

        agent_client, _ = operator('support_agent', geo)
        trip = world['hyd_trip']
        Trip.objects.filter(pk=trip.pk).update(estimated_fare_minor=90_000,
                                               final_fare_minor=90_000)
        r = agent_client.post(
            f'/api/v1/ride/admin/trips/{trip.id}/refund/',
            {'amount_minor': 60_000, 'reason': 'x', 'idempotency_key': 'nt'}, format='json')
        request = ApprovalRequest.objects.get(pk=r.json()['data']['approval_request']['id'])
        checker, _ = operator('super_admin', geo)
        assert decide(checker, request, 'reject').status_code == 200
        assert OperatorNotification.objects.count() == 0

    def test_the_rider_endpoint_never_leaks_an_internal_word(self, geo, world, policies):
        config()
        agent_client, _ = operator('support_agent', geo)
        ticket = world['t_hyd']
        request = over_limit_refund(agent_client, world)
        secret = 'ZZ-APPROVER-REASON-9981'
        checker, _ = operator('super_admin', geo)
        # Waiting: the rider sees the neutral line only.
        text = rider_view(world['hyd_rider'], ticket).content.decode().lower()
        assert "we're reviewing your refund request" in text.replace('\\u0027', "'")
        for word in FORBIDDEN_WORDS:
            assert word not in text, word
        decide(checker, request, 'reject', secret)
        # Rejected: nothing new, and not the reason.
        text = rider_view(world['hyd_rider'], ticket).content.decode().lower()
        assert secret.lower() not in text
        for word in FORBIDDEN_WORDS:
            assert word not in text, word
        # A second request that is approved: "issued" wording is neutral too.
        second = over_limit_refund(agent_client, world, key='r2', amount=55_000)
        assert decide(checker, second, 'approve').status_code == 200
        response = rider_view(world['hyd_rider'], ticket)
        text = response.content.decode().lower()
        for word in FORBIDDEN_WORDS + (secret.lower(),):
            assert word not in text, word
        bodies = [m['body'] for m in response.json()['data']['messages']]
        assert any('has been issued' in b or 'on its way' in b for b in bodies)
        # The list endpoint too.
        listing = _client_for(world['hyd_rider']).get(MY_TICKETS).content.decode().lower()
        for word in FORBIDDEN_WORDS + (secret.lower(),):
            assert word not in listing, word


class TestRefundWording:
    def test_timeframe_comes_from_the_country_config(self, geo, world):
        assert refund_review_sentence('IN') == "We're reviewing your refund request."
        config('IN', hours=72)
        assert refund_review_sentence('IN') == (
            "We're reviewing your refund request. You'll hear from us within 3 days.")
        SupportCountryConfig.objects.filter(country='IN').update(refund_review_hours=24)
        assert refund_review_sentence('IN').endswith('within 24 hours.')
        SupportCountryConfig.objects.filter(country='IN').update(refund_review_hours=48)
        assert refund_review_sentence('IN').endswith('within 2 days.')
        SupportCountryConfig.objects.filter(country='IN').update(refund_review_hours=1)
        assert refund_review_sentence('IN').endswith('within 1 hour.')
        # No fallback to any other country.
        assert refund_review_sentence('US') == "We're reviewing your refund request."
        assert refund_review_sentence('') == "We're reviewing your refund request."

    def test_same_line_whether_it_ran_at_once_or_waits(self, geo, world, policies):
        config('IN', hours=72)
        client, _ = operator('support_agent', geo)
        ticket = world['t_hyd']
        request = over_limit_refund(client, world)           # waits for a checker
        waits = SupportMessage.everything.get(ticket=ticket, author_role='system').body
        assert request.status == REQUESTED
        other = mk(world['hyd_rider'], world['hyd_trip'], 'fare_dispute', 'again')
        r = client.post(
            f'/api/v1/ride/admin/trips/{other.trip_id_id}/refund/',
            {'amount_minor': 20_000, 'reason': 'fare dispute', 'idempotency_key': 'now',
             'ticket_id': other.id}, format='json')
        assert r.status_code == 200, r.content               # ran at once
        first = list(SupportMessage.everything.filter(
            ticket=other, author_role='system').order_by('id').values_list('body', flat=True))
        assert first[0] == waits == (
            "We're reviewing your refund request. You'll hear from us within 3 days.")
        assert 'has been issued' in first[1]


# ===========================================================================
# 4a. The support_lead template
# ===========================================================================

class TestSupportLeadRole:
    EXPECTED = {
        'trip.read', 'rider.read', 'driver.read', 'support.read', 'support.reply',
        'support.refund', 'support.macro', 'support.dashboard', 'rider.note', 'rider.flag',
        'rider.signout',
    }

    def test_the_template_holds_exactly_the_agreed_capabilities(self):
        assert validate_templates() == []
        lead = next(t for t in ROLE_TEMPLATES if t['code'] == 'support_lead')
        assert set(lead['permissions']) == self.EXPECTED
        assert not lead.get('is_global_only')

    def test_dashboard_and_sla_grants(self):
        by = {t['code']: set(t['permissions']) for t in ROLE_TEMPLATES}
        assert 'support.dashboard' in by['city_ops_manager'] and 'support.sla' in by['city_ops_manager']
        assert 'support.dashboard' in by['super_admin']
        assert {c for c, p in by.items() if 'support.dashboard' in p} == {
            'super_admin', 'city_ops_manager', 'support_lead'}

    def test_a_lead_works_tickets_in_scope_only(self, geo, world):
        client, _ = operator('support_lead', geo)
        assert client.get(url(world['t_hyd'])).status_code == 200
        assert client.get(url(world['t_aus'])).status_code == 404
        assert post(client, world['t_hyd'], 'reply/', {'body': 'Hi'}).status_code == 201


# ===========================================================================
# 4b. Macro versions
# ===========================================================================

class TestMacroVersions:
    def test_every_edit_is_a_version_and_restore_is_a_new_one(self, geo, world):
        admin, _ = operator('super_admin', geo)
        made = admin.post(BASE + 'macros/', {'title': 'Lost item', 'body': 'Hi {first_name}, v1',
                                             'locale': 'en'}, format='json')
        assert made.status_code == 201, made.content
        macro_id = made.json()['data']['id']
        assert made.json()['data']['version'] == 1
        edit = admin.patch(f'{BASE}macros/{macro_id}/', {'body': 'Hi {first_name}, v2'},
                           format='json')
        assert edit.status_code == 200 and edit.json()['data']['version'] == 2
        # An edit that changes nothing writes nothing.
        same = admin.patch(f'{BASE}macros/{macro_id}/', {'body': 'Hi {first_name}, v2'},
                           format='json')
        assert same.json()['data']['changed'] is False
        assert SupportMacroVersion.objects.filter(macro_id=macro_id).count() == 2

        history = admin.get(f'{BASE}macros/{macro_id}/versions/').json()['data']
        assert [v['version'] for v in history['results']] == [2, 1]
        assert history['results'][1]['body'] == 'Hi {first_name}, v1'
        assert history['results'][0]['edited_by']['id']

        restored = admin.post(f'{BASE}macros/{macro_id}/restore/', {'version': 1},
                              format='json')
        assert restored.status_code == 200, restored.content
        data = restored.json()['data']
        assert data['changed'] is True and data['version'] == 3
        assert data['body'] == 'Hi {first_name}, v1'
        versions = list(SupportMacroVersion.objects.filter(macro_id=macro_id).order_by('version'))
        assert [v.version for v in versions] == [1, 2, 3]
        assert versions[2].restored_from == 1 and versions[2].body == versions[0].body
        assert SupportMacro.objects.get(pk=macro_id).body == 'Hi {first_name}, v1'
        # Restoring what it already says is a no-op.
        again = admin.post(f'{BASE}macros/{macro_id}/restore/', {'version': 3}, format='json')
        assert again.json()['data']['changed'] is False
        assert SupportMacroVersion.objects.filter(macro_id=macro_id).count() == 3
        assert admin.post(f'{BASE}macros/{macro_id}/restore/', {'version': 99},
                          format='json').status_code == 404
        # Audit rows carry before / after.
        actions = list(AdminAuditLog.objects.filter(
            target_type='support_macro', target_id=str(macro_id)).order_by('id'
                                                                           ).values_list('action', flat=True))
        assert actions == ['support_macro_created', 'support_macro_updated',
                           'support_macro_restored']
        row = AdminAuditLog.objects.get(action='support_macro_restored')
        assert row.before['body'].endswith('v2') and row.after['body'].endswith('v1')
        assert row.before['version'] == 2 and row.after['version'] == 3

    def test_history_and_restore_need_support_macro(self, geo, world):
        macro = SupportMacro.objects.create(title='T', body='Hello')
        SupportMacroVersion.objects.create(macro=macro, version=1, title='T', body='Hello')
        agent, _ = operator('support_agent', geo)
        assert agent.get(f'{BASE}macros/{macro.id}/versions/').status_code == 403
        assert agent.post(f'{BASE}macros/{macro.id}/restore/', {'version': 1},
                          format='json').status_code == 403

    def test_a_lead_with_country_scope_manages_that_countrys_macros(self, geo, world):
        lead, _ = operator('support_lead', geo, 'IN')
        made = lead.post(BASE + 'macros/', {'title': 'IN only', 'body': 'Hello', 'country': 'IN'},
                         format='json')
        assert made.status_code == 201, made.content
        other = lead.post(BASE + 'macros/', {'title': 'US only', 'body': 'Hello', 'country': 'US'},
                          format='json')
        assert other.status_code == 403


# ===========================================================================
# 4c. The SLA dashboard
# ===========================================================================

def shaped(ticket, *, created_ago, priority=None, responded_after=None, resolved_after=None,
           status=None, assigned=None, waiting_since=None):
    """Make a ticket look like it was raised `created_ago` ago and handled so."""
    now = timezone.now()
    created = now - created_ago
    changes = {'created_at': created}
    if priority:
        changes['priority'] = priority
    if status:
        changes['status'] = status
    if assigned is not None:
        changes['assigned_to'] = assigned
    if responded_after is not None:
        changes['first_responded_at'] = created + responded_after
    if resolved_after is not None:
        changes['resolved_at'] = created + resolved_after
        changes['status'] = 'CLOSED'
    if waiting_since is not None:
        changes['status'] = 'WAITING_USER'
        changes['waiting_since'] = waiting_since
    SupportTicket.objects.filter(pk=ticket.pk).update(**changes)
    ticket.refresh_from_db()
    apply_due_times(ticket)
    ticket.refresh_from_db()
    return ticket


class TestDashboard:
    @pytest.fixture
    def fixture(self, geo, world):
        lead, lead_user = operator('support_lead', geo)
        r, trip = world['hyd_rider'], world['hyd_trip']
        mins = lambda n: timedelta(minutes=n)  # noqa: E731
        a = shaped(mk(r, trip, 'payment', 'A'), created_ago=5 * DAYS, responded_after=mins(10),
                   resolved_after=mins(600), assigned=lead_user)
        b = shaped(mk(r, trip, 'payment', 'B'), created_ago=5 * DAYS, responded_after=mins(300),
                   resolved_after=mins(3000), assigned=lead_user)
        c = shaped(mk(r, trip, 'payment', 'C'), created_ago=5 * DAYS, priority='high')
        d = shaped(mk(r, trip, 'payment', 'D'), created_ago=1 * DAYS, responded_after=mins(5),
                   assigned=lead_user, waiting_since=timezone.now() - 2 * HOURS)
        return lead, lead_user, {'a': a, 'b': b, 'c': c, 'd': d}

    def rows(self, client, query=''):
        response = client.get(BASE + 'sla/dashboard/' + query)
        assert response.status_code == 200, response.content
        return response.json()['data']

    def test_numbers_on_a_known_fixture(self, geo, world, fixture):
        lead, lead_user, _ = fixture
        data = self.rows(lead)
        hyd = {r['priority']: r for r in data['rows'] if r['city']['code'] == 'IN-TG-HYD'}
        # normal: t_hyd, t_driver, A, B, D opened. First response: A met, B late,
        # D met (t_hyd / t_driver are not due yet). Resolution: A met, B late.
        normal = hyd['normal']
        assert normal['opened'] == 5
        assert normal['first_response_met_pct'] == 66.7
        assert normal['resolution_met_pct'] == 50.0
        assert normal['median_first_response_minutes'] == 10.0
        assert normal['currently_breaching'] == 0 and normal['paused'] == 1
        # high: C never answered and is long past both deadlines.
        high = hyd['high']
        assert high['opened'] == 1 and high['first_response_met_pct'] == 0.0
        assert high['resolution_met_pct'] == 0.0
        assert high['median_first_response_minutes'] is None
        assert high['currently_breaching'] == 1 and high['paused'] == 0
        # Per agent: the lead owns A, B, D.
        agents = {(a['agent'] or {}).get('id'): a for a in data['agents']}
        mine = agents[lead_user.id]
        assert mine['opened'] == 3 and mine['first_response_met_pct'] == 66.7
        assert mine['paused'] == 1 and mine['currently_breaching'] == 0
        assert agents[None]['unassigned'] is True and agents[None]['currently_breaching'] == 1
        assert data['totals']['paused'] == 1
        assert data['date_to'] and data['date_from']

    def test_the_date_range_is_applied(self, geo, world, fixture):
        lead, _, _ = fixture
        today = timezone.now().date().isoformat()
        data = self.rows(lead, f'?date_from={today}&date_to={today}')
        hyd = {r['priority']: r for r in data['rows'] if r['city']['code'] == 'IN-TG-HYD'}
        # Only t_hyd and t_driver were opened today; A, B, C, D are older. The
        # "right now" columns still see every open ticket.
        assert hyd['normal']['opened'] == 2
        assert hyd['high']['opened'] == 0 and hyd['high']['currently_breaching'] == 1
        assert lead.get(BASE + 'sla/dashboard/?date_from=2026-01-02&date_to=2026-01-01'
                        ).status_code == 400
        assert lead.get(BASE + 'sla/dashboard/?date_from=2025-01-01&date_to=2026-01-01'
                        ).status_code == 400
        assert lead.get(BASE + 'sla/dashboard/?date_from=nope').status_code == 400

    def test_scope_and_capability(self, geo, world, fixture):
        lead, _, _ = fixture
        # No capability: a support agent does not hold support.dashboard.
        agent, _ = operator('support_agent', geo)
        assert agent.get(BASE + 'sla/dashboard/').status_code == 403
        # Another city's lead sees none of Hyderabad's tickets.
        austin, _ = operator('support_lead', geo, 'US-TX-AUS')
        data = self.rows(austin)
        assert all(r['city']['code'] != 'IN-TG-HYD' for r in data['rows'])
        assert data['totals']['opened'] == self.rows(austin)['totals']['opened']
        hyd_total = self.rows(lead)['totals']['opened']
        assert hyd_total > self.rows(austin)['totals']['opened']
        # A scope the caller does not hold is refused.
        assert lead.get(BASE + 'sla/dashboard/?scope=US-TX-AUS').status_code in (403, 404)
        # City Ops and Super Admin hold it too.
        assert operator('city_ops_manager', geo)[0].get(BASE + 'sla/dashboard/').status_code == 200
        assert operator('super_admin', geo)[0].get(BASE + 'sla/dashboard/').status_code == 200
        # Nothing in the payload names a rider or a phone.
        text = lead.get(BASE + 'sla/dashboard/').content.decode()
        assert '+9198' not in text and 'Lakshmi' not in text


# ===========================================================================
# 4d. SLA and country-config changes need a checker
# ===========================================================================

class TestSlaChangeNeedsAChecker:
    BODY = {'first_response_minutes': 120, 'resolution_minutes': 600}

    def test_the_change_waits_for_a_checker_and_then_applies(self, geo, world, policies):
        maker, maker_user = operator('super_admin', geo)
        r = maker.put(BASE + 'sla/IN/normal/', self.BODY, format='json')
        assert r.status_code == 202, r.content
        assert r.json()['data']['state'] == 'pending_approval'
        assert not SupportSlaPolicy.objects.filter(country='IN', priority='normal').exists()
        request = ApprovalRequest.objects.get(pk=r.json()['data']['approval_request']['id'])
        assert request.action_key == 'support.sla_change' and request.status == REQUESTED
        # Same request again: the same pending row, no second one.
        again = maker.put(BASE + 'sla/IN/normal/', self.BODY, format='json')
        assert again.json()['data']['duplicate'] is True
        assert ApprovalRequest.objects.filter(action_key='support.sla_change').count() == 1
        # The page shows it as pending.
        page = maker.get(BASE + 'sla/').json()['data']
        assert [p['id'] for p in page['pending']] == [request.id]
        assert page['pending'][0]['first_response_minutes'] == 120
        # The maker cannot check their own request.
        assert maker.post(f'/api/v1/approvals/admin/requests/{request.id}/approve/', {},
                          format='json').status_code in (403, 409)
        assert not SupportSlaPolicy.objects.filter(country='IN', priority='normal').exists()
        checker, _ = operator('super_admin', geo)
        done = checker.post(f'/api/v1/approvals/admin/requests/{request.id}/approve/', {},
                            format='json')
        assert done.status_code == 200, done.content
        row = SupportSlaPolicy.objects.get(country='IN', priority='normal')
        assert (row.first_response_minutes, row.resolution_minutes) == (120, 600)
        assert row.updated_by_id == maker_user.id
        audit_row = AdminAuditLog.objects.get(action='support_sla_changed')
        assert audit_row.before['first_response_minutes'] == 240
        assert audit_row.after['first_response_minutes'] == 120
        assert audit_row.after['approval_request_id'] == request.id
        assert maker.get(BASE + 'sla/').json()['data']['pending'] == []
        # A new ticket is timed by the new policy.
        fresh = mk(world['hyd_rider'], world['hyd_trip'], 'payment', 'new')
        assert fresh.first_response_due_at - fresh.created_at == timedelta(minutes=120)
        # Unchanged values answer "no change" without a request.
        same = maker.put(BASE + 'sla/IN/normal/', self.BODY, format='json')
        assert same.status_code == 200 and same.json()['data']['changed'] is False

    def test_a_rejected_change_applies_nothing(self, geo, world, policies):
        maker, _ = operator('super_admin', geo)
        request = ApprovalRequest.objects.get(pk=maker.put(
            BASE + 'sla/IN/low/', self.BODY, format='json').json()['data']['approval_request']['id'])
        checker, _ = operator('super_admin', geo)
        assert decide(checker, request, 'reject', 'No').status_code == 200
        assert not SupportSlaPolicy.objects.filter(country='IN', priority='low').exists()

    def test_city_ops_asks_for_its_own_country_only(self, geo, world, policies):
        cityops, _ = operator('city_ops_manager', geo, 'IN')
        # Another country: refused for lack of scope, before anything is raised.
        denied = cityops.put(BASE + 'sla/US/normal/', self.BODY, format='json')
        assert denied.status_code == 403
        assert denied.json()['error']['code'] == 'PERMISSION_DENIED'
        denied_cfg = cityops.put(BASE + 'sla/config/US/', {
            'waiting_reminder_days': 2, 'waiting_autoclose_days': 7,
            'refund_review_hours': 72}, format='json')
        assert denied_cfg.status_code == 403
        assert ApprovalRequest.objects.filter(action_key='support.sla_change').count() == 0
        # Its own country: a request, never an immediate change.
        ok = cityops.put(BASE + 'sla/IN/normal/', self.BODY, format='json')
        assert ok.status_code == 202, ok.content
        assert not SupportSlaPolicy.objects.exists()
        # A city-scoped City Ops holds no country-level support.sla.
        city_only, _ = operator('city_ops_manager', geo)
        assert city_only.put(BASE + 'sla/IN/normal/', self.BODY, format='json').status_code == 403
        # A role without support.sla is refused.
        agent, _ = operator('support_agent', geo)
        assert agent.put(BASE + 'sla/IN/normal/', self.BODY, format='json').status_code == 403

    def test_an_unconfigured_country_is_blocked_not_defaulted(self, geo, world, policies):
        # Approval policies are seeded for India only (by ruling): a US change
        # is refused, never given India's rules.
        admin, _ = operator('super_admin', geo)
        r = admin.put(BASE + 'sla/US/normal/', self.BODY, format='json')
        assert r.status_code == 409
        assert r.json()['error']['code'] == 'THRESHOLD_NOT_CONFIGURED'

    def test_country_config_changes_need_a_checker_and_bump_the_version(self, geo, world, policies):
        maker, _ = operator('super_admin', geo)
        body = {'waiting_reminder_days': 3, 'waiting_autoclose_days': 10, 'refund_review_hours': 96}
        page = maker.get(BASE + 'sla/').json()['data']
        assert {c['country']: c['configured'] for c in page['config']} == {
            'IN': False, 'US': False}
        r = maker.put(BASE + 'sla/config/IN/', body, format='json')
        assert r.status_code == 202, r.content
        assert not SupportCountryConfig.objects.exists()
        request = ApprovalRequest.objects.get(pk=r.json()['data']['approval_request']['id'])
        checker, _ = operator('super_admin', geo)
        assert decide(checker, request, 'approve').status_code == 200
        cfg = SupportCountryConfig.objects.get(country='IN')
        assert (cfg.waiting_reminder_days, cfg.waiting_autoclose_days,
                cfg.refund_review_hours, cfg.version) == (3, 10, 96, 1)
        assert AdminAuditLog.objects.filter(action='support_country_config_changed').count() == 1
        assert maker.put(BASE + 'sla/config/IN/', body, format='json'
                         ).json()['data']['changed'] is False
        bad = maker.put(BASE + 'sla/config/IN/', {**body, 'waiting_autoclose_days': 3},
                        format='json')
        assert bad.status_code == 400
        page = maker.get(BASE + 'sla/').json()['data']
        assert next(c for c in page['config'] if c['country'] == 'IN')['configured'] is True

    def test_the_policy_row_exists_and_is_always_a_checker(self, geo, policies):
        from servers.approvals.service import active_policy

        row = active_policy('IN', 'support.sla_change')
        assert row is not None and row.limit_kind == 'always' and row.maker_limit is None
        assert active_policy('US', 'support.sla_change') is None
