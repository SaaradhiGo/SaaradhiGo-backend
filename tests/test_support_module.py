"""Support module (console module 5): queue, ticket page, writes, macros, SLA.

WHAT THESE PROVE
----------------
  * ONE scope rule (`servers/support/scope.py`): a ticket with a trip sits in
    the trip's city; else a driver's ticket in the driver's home city; else a
    rider's ticket wherever that rider is visible (a trip-less +91 rider is
    found by every support holder scoped anywhere in India). A ticket with a
    trip is not also visible through the rider's other cities. Out of scope
    is 404, no capability at the ticket is 403, and every ticket the list
    shows opens with 200 for the same caller;
  * per-role capability (support_agent writes; finance / safety read; marketing,
    analyst, KYC are refused);
  * every write leaves one audit row with before / after and is idempotent;
  * assignment is refused to anyone who cannot reply at the ticket;
  * SLA due times, breach, recompute on a priority change, the seeded policy;
  * an internal note never reaches the rider / driver endpoints;
  * macro placeholders come from a fixed allow-list and render server-side;
  * fare review goes through the existing trip refund (maker-checker), links
    the ticket and posts system messages, and moves no money itself;
  * contact details inside message text are masked, the logged reveal
    returns the thread.
"""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from conftest import seed_actor
from servers.admin_access.models import AdminAssignment, AdminRole
from servers.admin_access.roles import ROLE_TEMPLATES, SUPER_ADMIN, validate_templates
from servers.admin_audit.models import AdminAuditLog
from servers.approvals.models import REQUESTED, ApprovalRequest
from servers.rider.models import Rider
from servers.support.models import SupportMacro, SupportMessage, SupportSlaPolicy, SupportTicket
from servers.support.scope import tickets_visible_to
from test_sos_support_zones_matrix import (  # noqa: F401 - fixtures and helpers
    _client_for, _driver, _rider, _zone, geo, operator,
)
from test_trips_module import AUSTIN_POLYGON, _trip, ids  # noqa: F401

pytestmark = pytest.mark.django_db

BASE = '/api/v1/support/admin/'
LIST = BASE + 'tickets/'
MY_TICKETS = '/api/v1/support/tickets/'
READ_ROLES = {'support_agent', 'finance', 'safety_officer', 'city_ops_manager', SUPER_ADMIN}
WRITE_ROLES = {'support_agent', 'support_lead', SUPER_ADMIN}
NO_ACCESS = ['marketing', 'analyst', 'kyc_verifier']
ROLES = [t['code'] for t in ROLE_TEMPLATES]


def url(ticket, tail=''):
    return f'{LIST}{ticket.id}/{tail}'


def mk(user, trip=None, issue='other', description='help', **extra):
    return SupportTicket.objects.create(
        user_id=user, trip_id=trip, issue_type=issue, description=description, **extra)


def post(client, ticket, tail, body=None):
    return client.post(url(ticket, tail), body or {}, format='json')


def audit(ticket, action=None):
    rows = AdminAuditLog.objects.filter(target_type='support_ticket', target_id=str(ticket.id))
    return rows.filter(action=action) if action else rows


@pytest.fixture
def world(geo, settings):
    from decimal import Decimal

    settings.RIDER_CREDIT_BALANCE_CAP = Decimal('100000.00')
    hyd = _zone('IN-TG-HYD', geo['IN-TG-HYD'])
    aus = _zone('US-TX-AUS', geo['US-TX-AUS'], country='US', state_code='TX',
                timezone_name='America/Chicago', polygon_geojson=AUSTIN_POLYGON, currency='USD')
    now = timezone.now()
    w = {'hyd_zone': hyd, 'aus_zone': aus}
    w['hyd_rider'] = _rider('+919876520001', 'Lakshmi Devi')
    w['aus_rider'] = _rider('+15125520002', 'Austin Rider')
    w['both_rider'] = _rider('+919876520003', 'Both Cities')
    w['newcomer'] = _rider('+919876520004', 'New Comer')      # +91, never booked
    w['us_newcomer'] = _rider('+15125520005', 'Us Newcomer')   # +1, never booked
    w['nowhere'] = _rider('+447700900006', 'Nowhere Rider')    # unknown prefix
    w['hyd_driver'] = _driver('+919876520007', 'Kiran Rao', geo['IN-TG-HYD'])
    w['aus_driver'] = _driver('+15125520008', 'Austin Driver', geo['US-TX-AUS'])
    w['hyd_trip'] = _trip(w['hyd_rider'], hyd, driver=w['hyd_driver'], fare=40_000, paid=True,
                          method='cash', requested_at=now - timedelta(hours=2))
    w['aus_trip'] = _trip(w['aus_rider'], aus, fare=2_000, paid=True, method='cash',
                          requested_at=now - timedelta(hours=2))
    _trip(w['both_rider'], hyd, requested_at=now - timedelta(days=100))
    _trip(w['both_rider'], aus, requested_at=now - timedelta(days=2))
    w['t_hyd'] = mk(w['hyd_rider'], w['hyd_trip'], 'fare_dispute', 'Charged too much')
    w['t_aus'] = mk(w['aus_rider'], w['aus_trip'], 'fare_dispute', 'Charged too much')
    # A rider who rides in BOTH cities raises a ticket about the Austin trip:
    # it is an Austin ticket, not also a Hyderabad one.
    w['both_trip'] = _trip(w['both_rider'], aus, requested_at=now - timedelta(hours=5))
    w['t_both_aus_trip'] = mk(w['both_rider'], w['both_trip'], 'payment', 'Austin trip issue')
    w['t_newcomer'] = mk(w['newcomer'], None, 'account', 'I cannot book a ride')
    w['t_us_newcomer'] = mk(w['us_newcomer'], None, 'account', 'I cannot book a ride')
    w['t_nowhere'] = mk(w['nowhere'], None, 'account', 'unplaceable')
    w['t_driver'] = mk(w['hyd_driver'].user_id, None, 'payment', 'Payout late')
    w['t_aus_driver'] = mk(w['aus_driver'].user_id, None, 'payment', 'Payout late')
    return w


TICKET_KEYS = ('t_hyd', 't_aus', 't_both_aus_trip', 't_newcomer', 't_us_newcomer',
               't_nowhere', 't_driver', 't_aus_driver')


def listed(client, query=''):
    response = client.get(LIST + query)
    assert response.status_code == 200, response.content
    return set(ids(response))


# ===========================================================================
# Scope
# ===========================================================================

class TestTicketScope:
    def test_hyderabad_sees_its_trip_its_driver_and_indian_newcomers(self, geo, world):
        got = listed(operator('support_agent', geo)[0])
        assert got == {world[k].id for k in ('t_hyd', 't_newcomer', 't_driver')}

    def test_austin_sees_austin_and_us_newcomers(self, geo, world):
        got = listed(operator('support_agent', geo, 'US-TX-AUS')[0])
        assert got == {world[k].id for k in (
            't_aus', 't_both_aus_trip', 't_us_newcomer', 't_aus_driver')}

    def test_a_ticket_with_a_trip_is_not_also_the_riders_other_city(self, geo, world):
        # The rider rode in Hyderabad 100 days ago, but THIS ticket is about
        # an Austin trip.
        client, _ = operator('support_agent', geo)
        assert url(world['t_both_aus_trip']) and client.get(
            url(world['t_both_aus_trip'])).status_code == 404

    def test_country_scope_and_global(self, geo, world):
        got = listed(operator('support_agent', geo, 'IN')[0])
        assert got == {world[k].id for k in ('t_hyd', 't_newcomer', 't_driver')}
        everyone = listed(operator(SUPER_ADMIN, geo)[0])
        assert everyone == {world[k].id for k in TICKET_KEYS}  # incl. the unplaceable one

    def test_an_unplaceable_requester_is_for_unrestricted_callers_only(self, geo, world):
        for scope in ('IN-TG-HYD', 'IN', 'US-TX-AUS', 'US'):
            client, _ = operator('support_agent', geo, scope)
            assert client.get(url(world['t_nowhere'])).status_code == 404
        assert operator(SUPER_ADMIN, geo)[0].get(url(world['t_nowhere'])).status_code == 200

    def test_scope_narrowing_applies_on_top(self, geo, world):
        client, _ = operator(SUPER_ADMIN, geo)
        got = listed(client, '?scope=US-TX-AUS')
        assert got == {world[k].id for k in (
            't_aus', 't_both_aus_trip', 't_us_newcomer', 't_aus_driver')}

    def test_out_of_scope_is_404_and_no_capability_is_403(self, geo, world):
        support, _ = operator('support_agent', geo)
        assert support.get(url(world['t_aus'])).status_code == 404
        assert post(support, world['t_aus'], 'reply/', {'body': 'x'}).status_code == 404
        finance, _ = operator('finance', geo)
        assert finance.get(url(world['t_hyd'])).status_code == 200
        assert post(finance, world['t_hyd'], 'reply/', {'body': 'x'}).status_code == 403
        assert post(finance, world['t_newcomer'], 'note/', {'body': 'x'}).status_code == 403

    @pytest.mark.parametrize('scope', ['IN-TG-HYD', 'IN', 'US-TX-AUS', 'US'])
    @pytest.mark.parametrize('role', [r for r in ROLES if r != SUPER_ADMIN])
    def test_every_listed_ticket_opens(self, geo, world, role, scope):
        """One rule everywhere: a row the list shows never 404s on the page."""
        client, _ = operator(role, geo, scope)
        if role in NO_ACCESS:
            assert client.get(LIST).status_code == 403
            return
        shown = listed(client)
        assert shown, (role, scope)
        for ticket_id in shown:
            assert client.get(f'{LIST}{ticket_id}/').status_code == 200, (role, scope, ticket_id)
        # ...and nothing the page opens is missing from the list.
        for key in TICKET_KEYS:
            opens = client.get(url(world[key])).status_code == 200
            assert opens == (world[key].id in shown), (role, scope, key)

    def test_the_assignee_list_and_macros_use_the_same_rule(self, geo, world):
        support, _ = operator('support_agent', geo)
        assert support.get(url(world['t_aus'], 'assignees/')).status_code == 404
        assert support.get(url(world['t_newcomer'], 'assignees/')).status_code == 200
        macro = SupportMacro.objects.create(title='Hi', body='Hi {first_name}')
        assert support.get(f'{BASE}macros/?ticket={world["t_aus"].id}').status_code == 404
        assert support.get(
            f'{BASE}macros/{macro.id}/preview/?ticket={world["t_aus"].id}').status_code == 404

    def test_the_rule_is_one_queryset(self, geo, world):
        _, user = operator('support_agent', geo)
        qs = tickets_visible_to(user)
        assert set(qs.values_list('id', flat=True)) == {
            world[k].id for k in ('t_hyd', 't_newcomer', 't_driver')}

    def test_a_driver_without_a_home_city_is_global_only(self, geo, world):
        from servers.driver.models import Driver

        Driver.objects.filter(pk=world['hyd_driver'].pk).update(home_city=None)
        assert world['t_driver'].id not in listed(operator('support_agent', geo)[0])
        assert operator(SUPER_ADMIN, geo)[0].get(url(world['t_driver'])).status_code == 200


# ===========================================================================
# Capability per role
# ===========================================================================

class TestCapabilities:
    @pytest.mark.parametrize('role', ROLES)
    def test_read_and_write_by_role(self, geo, world, role):
        client, _ = operator(role, geo)
        ticket = world['t_hyd']
        if role in NO_ACCESS:
            assert client.get(LIST).status_code == 403
            assert client.get(url(ticket)).status_code == 403
            return
        assert client.get(url(ticket)).status_code == 200
        wrote = {
            'reply': post(client, ticket, 'reply/', {'body': 'On it'}).status_code,
            'note': post(client, ticket, 'note/', {'body': 'internal'}).status_code,
            'status': post(client, ticket, 'status/', {'status': 'WAITING_USER'}).status_code,
            'priority': post(client, ticket, 'priority/', {'priority': 'high'}).status_code,
            'category': post(client, ticket, 'category/', {'issue_type': 'payment'}).status_code,
        }
        if role in WRITE_ROLES:
            assert set(wrote.values()) <= {200, 201}, (role, wrote)
        else:
            assert set(wrote.values()) == {403}, (role, wrote)

    def test_allowed_actions_follow_the_capability(self, geo, world):
        data = operator('support_agent', geo)[0].get(url(world['t_hyd'])).json()['data']
        a = data['allowed_actions']
        assert a['reply'] and a['note'] and a['assign'] and a['refund'] and not a['reveal']
        data = operator('finance', geo)[0].get(url(world['t_hyd'])).json()['data']
        a = data['allowed_actions']
        assert not (a['reply'] or a['note'] or a['assign'] or a['status'])
        data = operator('safety_officer', geo)[0].get(url(world['t_hyd'])).json()['data']
        assert data['allowed_actions']['reveal'] and not data['allowed_actions']['reply']

    def test_macro_and_sla_capabilities_are_held_by_named_roles_only(self):
        # AUDIT 27.36: the Support Lead keeps the saved replies, City Ops
        # asks for SLA changes (through a checker); nobody else holds either.
        assert validate_templates() == []
        holders = {t['code'] for t in ROLE_TEMPLATES if 'support.macro' in t['permissions']}
        assert holders == {SUPER_ADMIN, 'support_lead'}
        holders = {t['code'] for t in ROLE_TEMPLATES if 'support.sla' in t['permissions']}
        assert holders == {SUPER_ADMIN, 'city_ops_manager'}


# ===========================================================================
# Writes: audit, idempotency
# ===========================================================================

class TestWrites:
    def test_reply_stamps_first_response_and_audits(self, geo, world):
        client, user = operator('support_agent', geo)
        ticket = world['t_hyd']
        response = post(client, ticket, 'reply/', {'body': 'Looking into it', 'idempotency_key': 'a1'})
        assert response.status_code == 201, response.content
        ticket.refresh_from_db()
        assert ticket.first_responded_at is not None and ticket.status == 'IN_PROGRESS'
        row = audit(ticket, 'support_ticket_replied').get()
        assert row.actor_id == user.id
        assert row.before['status'] == 'OPEN' and row.after['status'] == 'IN_PROGRESS'
        assert row.before['first_responded_at'] is None and row.after['first_responded_at']

    def test_reply_is_idempotent_on_its_key(self, geo, world):
        client, _ = operator('support_agent', geo)
        ticket = world['t_hyd']
        first = post(client, ticket, 'reply/', {'body': 'Once', 'idempotency_key': 'k'})
        again = post(client, ticket, 'reply/', {'body': 'Once', 'idempotency_key': 'k'})
        assert (first.status_code, again.status_code) == (201, 200)
        assert again.json()['data']['created'] is False
        assert SupportMessage.everything.filter(ticket=ticket, author_role='support').count() == 1
        assert audit(ticket, 'support_ticket_replied').count() == 1

    def test_a_note_does_not_stop_the_first_response_clock(self, geo, world):
        client, _ = operator('support_agent', geo)
        ticket = world['t_hyd']
        assert post(client, ticket, 'note/', {'body': 'Call the driver', 'idempotency_key': 'n'}
                    ).status_code == 201
        again = post(client, ticket, 'note/', {'body': 'Call the driver', 'idempotency_key': 'n'})
        assert again.status_code == 200
        ticket.refresh_from_db()
        assert ticket.first_responded_at is None and ticket.status == 'OPEN'
        assert audit(ticket, 'support_ticket_noted').count() == 1
        assert SupportMessage.everything.filter(ticket=ticket, author_role='internal').count() == 1

    def test_reply_to_a_closed_ticket_needs_a_status(self, geo, world):
        client, _ = operator('support_agent', geo)
        ticket = world['t_hyd']
        post(client, ticket, 'status/', {'status': 'CLOSED'})
        assert post(client, ticket, 'reply/', {'body': 'late'}).status_code == 409
        reopened = post(client, ticket, 'reply/', {'body': 'Sorry, reopening', 'status': 'IN_PROGRESS'})
        assert reopened.status_code == 201
        ticket.refresh_from_db()
        assert ticket.status == 'IN_PROGRESS' and ticket.resolved_at is None

    def test_an_unknown_status_is_refused(self, geo, world):
        client, _ = operator('support_agent', geo)
        response = post(client, world['t_hyd'], 'reply/', {'body': 'x', 'status': 'NOPE'})
        assert response.status_code == 400
        assert response.json()['error']['code'] == 'INVALID_STATUS'

    @pytest.mark.parametrize('tail,body,action,field,value', [
        ('status/', {'status': 'WAITING_USER'}, 'support_ticket_status_changed', 'status', 'WAITING_USER'),
        ('status/', {'status': 'CLOSED'}, 'support_ticket_status_changed', 'status', 'CLOSED'),
        ('priority/', {'priority': 'high'}, 'support_ticket_priority_changed', 'priority', 'high'),
        ('category/', {'issue_type': 'payment'}, 'support_ticket_category_changed', 'issue_type', 'payment'),
    ])
    def test_each_change_audits_once_and_is_idempotent(
            self, geo, world, tail, body, action, field, value):
        client, _ = operator('support_agent', geo)
        ticket = world['t_hyd']
        first = post(client, ticket, tail, body)
        assert first.status_code == 200 and first.json()['data']['changed'] is True
        row = audit(ticket, action).get()
        assert row.before[field] != value and row.after[field] == value
        again = post(client, ticket, tail, body)
        assert again.status_code == 200 and again.json()['data']['changed'] is False
        assert audit(ticket, action).count() == 1

    def test_closing_stamps_resolved_at_and_reopening_clears_it(self, geo, world):
        client, _ = operator('support_agent', geo)
        ticket = world['t_hyd']
        post(client, ticket, 'status/', {'status': 'CLOSED'})
        ticket.refresh_from_db()
        assert ticket.resolved_at is not None
        post(client, ticket, 'status/', {'status': 'OPEN'})
        ticket.refresh_from_db()
        assert ticket.resolved_at is None

    def test_the_old_urls_are_audited_too(self, geo, world):
        client, user = operator('support_agent', geo)
        ticket = world['t_hyd']
        assert post(client, ticket, 'assign/', {'assigned_to': user.id}).status_code == 200
        assert post(client, ticket, 'reply/', {'body': 'hello'}).status_code == 201
        assert audit(ticket, 'support_ticket_assigned').count() == 1
        assert audit(ticket, 'support_ticket_replied').count() == 1


# ===========================================================================
# Assignment
# ===========================================================================

class TestAssignment:
    def test_assign_me_none_and_id(self, geo, world):
        client, user = operator('support_agent', geo)
        ticket = world['t_hyd']
        r = post(client, ticket, 'assign/', {'assigned_to': 'me'})
        assert r.status_code == 200 and r.json()['data']['assigned_to'] == user.id
        assert post(client, ticket, 'assign/', {'assigned_to': user.id}).json()['data']['changed'] is False
        assert audit(ticket, 'support_ticket_assigned').count() == 1
        r = post(client, ticket, 'assign/', {'assigned_to': 'none'})
        assert r.json()['data']['assigned_to'] is None
        assert audit(ticket, 'support_ticket_assigned').count() == 2

    def test_only_an_operator_who_can_reply_here_is_eligible(self, geo, world):
        client, _ = operator('support_agent', geo)
        ticket = world['t_hyd']
        _, colleague = operator('support_agent', geo)
        _, austin_agent = operator('support_agent', geo, 'US-TX-AUS')
        _, finance = operator('finance', geo)
        _, rider_staff = operator('support_agent', geo)
        rider_staff.is_active = False
        rider_staff.save(update_fields=['is_active'])
        eligible = {row['id'] for row in client.get(url(ticket, 'assignees/')).json()['data']['results']}
        assert colleague.id in eligible
        assert not ({austin_agent.id, finance.id, rider_staff.id} & eligible)
        for refused in (austin_agent, finance, rider_staff, world['hyd_rider']):
            r = post(client, ticket, 'assign/', {'assigned_to': refused.id})
            assert r.status_code == 400, refused
            assert r.json()['error']['code'] == 'ASSIGNEE_NOT_ELIGIBLE'
        assert post(client, ticket, 'assign/', {'assigned_to': colleague.id}).status_code == 200
        assert post(client, ticket, 'assign/', {'assigned_to': 999999}).status_code == 400

    def test_the_queue_filters_and_counts(self, geo, world):
        client, user = operator('support_agent', geo)
        post(client, world['t_hyd'], 'assign/', {'assigned_to': 'me'})
        mine = listed(client, '?assigned=me&status=open')
        assert mine == {world['t_hyd'].id}
        assert listed(client, '?assigned=none&status=open') == {
            world['t_newcomer'].id, world['t_driver'].id}
        queues = client.get(LIST).json()['data']['queues']
        assert queues == {'mine': 1, 'unassigned': 2, 'breaching': 0, 'open': 3, 'closed': 0,
                          'returned': 0}
        post(client, world['t_driver'], 'status/', {'status': 'CLOSED'})
        assert listed(client, '?status=CLOSED') == {world['t_driver'].id}
        assert client.get(LIST).json()['data']['queues']['closed'] == 1


# ===========================================================================
# SLA
# ===========================================================================

class TestSla:
    def test_due_times_are_set_at_creation_from_the_default_policy(self, geo, world):
        ticket = world['t_hyd']
        assert ticket.priority == 'normal' and ticket.country == 'IN'
        assert ticket.first_response_due_at == ticket.created_at + timedelta(minutes=240)
        assert ticket.resolution_due_at == ticket.created_at + timedelta(minutes=2880)
        assert world['t_aus'].country == 'US'
        assert world['t_newcomer'].country == 'IN' and world['t_us_newcomer'].country == 'US'

    def test_a_safety_ticket_is_born_urgent(self, geo, world):
        ticket = mk(world['hyd_rider'], world['hyd_trip'], 'safety', 'I felt unsafe')
        assert ticket.priority == 'urgent'
        assert ticket.first_response_due_at == ticket.created_at + timedelta(minutes=15)
        assert ticket.resolution_due_at == ticket.created_at + timedelta(minutes=240)

    def test_a_policy_row_beats_the_default(self, geo, world):
        SupportSlaPolicy.objects.create(country='IN', priority='normal',
                                        first_response_minutes=30, resolution_minutes=60)
        ticket = mk(world['hyd_rider'], world['hyd_trip'])
        assert ticket.first_response_due_at == ticket.created_at + timedelta(minutes=30)

    def test_breach_flags_minutes_and_the_queue(self, geo, world):
        client, _ = operator('support_agent', geo)
        ticket = world['t_hyd']
        now = timezone.now()
        SupportTicket.objects.filter(pk=ticket.pk).update(
            first_response_due_at=now - timedelta(minutes=10),
            resolution_due_at=now + timedelta(hours=5))
        sla = client.get(url(ticket)).json()['data']['sla']
        assert sla['state'] == 'breached' and sla['breached'] is True
        assert sla['first_response']['breached'] and sla['first_response']['minutes_remaining'] <= -9
        assert listed(client, '?breach=breached') == {ticket.id}
        assert client.get(LIST).json()['data']['queues']['breaching'] == 1
        # A reply stops the first-response clock; the ticket is no longer breaching.
        SupportTicket.objects.filter(pk=ticket.pk).update(
            first_response_due_at=now + timedelta(hours=1))
        post(client, ticket, 'reply/', {'body': 'Hi'})
        assert listed(client, '?breach=breached') == set()

    def test_due_soon(self, geo, world):
        client, _ = operator('support_agent', geo)
        ticket = world['t_hyd']
        SupportTicket.objects.filter(pk=ticket.pk).update(
            first_response_due_at=timezone.now() + timedelta(minutes=30))
        assert listed(client, '?breach=due_soon') == {ticket.id}
        sla = client.get(url(ticket)).json()['data']['sla']
        assert sla['state'] == 'due_soon' and 0 <= sla['minutes_remaining'] <= 30

    def test_priority_change_recomputes_from_created_at(self, geo, world):
        client, _ = operator('support_agent', geo)
        ticket = world['t_hyd']
        post(client, ticket, 'priority/', {'priority': 'urgent'})
        ticket.refresh_from_db()
        assert ticket.first_response_due_at == ticket.created_at + timedelta(minutes=15)
        assert ticket.resolution_due_at == ticket.created_at + timedelta(minutes=240)
        row = audit(ticket, 'support_ticket_priority_changed').get()
        assert row.before['priority'] == 'normal' and row.after['priority'] == 'urgent'

    def test_closed_tickets_have_no_running_clock(self, geo, world):
        client, _ = operator('support_agent', geo)
        ticket = world['t_hyd']
        SupportTicket.objects.filter(pk=ticket.pk).update(
            first_response_due_at=timezone.now() - timedelta(hours=1))
        post(client, ticket, 'status/', {'status': 'CLOSED'})
        assert client.get(url(ticket)).json()['data']['sla']['state'] == 'closed'
        assert listed(client, '?breach=breached') == set()

    def test_sorting(self, geo, world):
        client, _ = operator('support_agent', geo)
        post(client, world['t_driver'], 'priority/', {'priority': 'urgent'})
        rows = client.get(LIST + '?ordering=priority').json()['data']['results']
        assert rows[0]['id'] == world['t_driver'].id
        due = client.get(LIST + '?ordering=resolution_due').json()['data']['results']
        assert [r['sla']['resolution']['due_at'] for r in due] == sorted(
            r['sla']['resolution']['due_at'] for r in due)
        assert client.get(LIST + '?ordering=nonsense').status_code == 400

    def test_the_policy_endpoints(self, geo, world):
        support, _ = operator('support_agent', geo)
        rows = support.get(BASE + 'sla/').json()['data']
        assert len(rows['results']) == 8 and rows['may_edit'] is False
        assert {r['source'] for r in rows['results']} == {'default'}
        assert support.put(BASE + 'sla/IN/normal/', {'first_response_minutes': 5,
                                                     'resolution_minutes': 10}, format='json'
                           ).status_code == 403
        admin, _ = operator(SUPER_ADMIN, geo)
        body = {'first_response_minutes': 120, 'resolution_minutes': 600}
        # A change is a REQUEST for a checker now (see test_support_followups).
        changed = admin.put(BASE + 'sla/IN/normal/', body, format='json')
        assert changed.status_code == 409, changed.content  # no policy seeded in this fixture
        assert admin.put(BASE + 'sla/IN/normal/', {'first_response_minutes': 50,
                                                   'resolution_minutes': 10}, format='json'
                         ).status_code == 400
        assert admin.put(BASE + 'sla/FR/normal/', body, format='json').status_code == 404

    def test_seed_command_is_audited_idempotent_and_never_overwrites(self, geo):
        actor = seed_actor()
        call_command('seed_support_sla', actor_email=actor.email, stdout=StringIO())
        assert SupportSlaPolicy.objects.count() == 8
        assert SupportSlaPolicy.objects.get(country='US', priority='urgent').first_response_minutes == 15
        SupportSlaPolicy.objects.filter(country='IN', priority='low').update(first_response_minutes=7)
        call_command('seed_support_sla', actor_email=actor.email, stdout=StringIO())
        assert SupportSlaPolicy.objects.get(country='IN', priority='low').first_response_minutes == 7
        assert AdminAuditLog.objects.filter(action='support_sla_seeded').count() == 1


# ===========================================================================
# Internal notes never reach the rider or driver
# ===========================================================================

class TestInternalNotesStayInternal:
    def test_no_user_facing_endpoint_returns_an_internal_note(self, geo, world):
        client, _ = operator('support_agent', geo)
        ticket = world['t_hyd']
        post(client, ticket, 'note/', {'body': 'SECRET-INTERNAL-NOTE'})
        post(client, ticket, 'reply/', {'body': 'Public reply'})
        rider = _client_for(world['hyd_rider'])
        for path in (f'{MY_TICKETS}{ticket.id}/', MY_TICKETS):
            response = rider.get(path)
            assert response.status_code == 200
            text = response.content.decode()
            assert 'SECRET-INTERNAL-NOTE' not in text, path
            assert 'Public reply' in text
        assert 'internal' not in {m['author_role'] for m in
                                  rider.get(f'{MY_TICKETS}{ticket.id}/').json()['data']['messages']}
        # The rider replying does not surface it either.
        reply = rider.post(f'{MY_TICKETS}{ticket.id}/messages/', {'body': 'thanks'}, format='json')
        assert reply.status_code == 201 and 'SECRET' not in reply.content.decode()
        # The default manager (every reverse relation and prefetch) leaves it out.
        assert not ticket.messages.filter(author_role='internal').exists()
        assert SupportMessage.everything.filter(ticket=ticket, author_role='internal').count() == 1

    def test_an_operator_reading_the_user_endpoint_does_not_get_notes_either(self, geo, world):
        client, _ = operator('support_agent', geo)
        ticket = world['t_hyd']
        post(client, ticket, 'note/', {'body': 'SECRET-INTERNAL-NOTE'})
        assert 'SECRET-INTERNAL-NOTE' not in client.get(f'{MY_TICKETS}{ticket.id}/').content.decode()

    def test_the_console_flags_notes(self, geo, world):
        client, _ = operator('support_agent', geo)
        ticket = world['t_hyd']
        post(client, ticket, 'note/', {'body': 'Call the driver'})
        thread = client.get(url(ticket)).json()['data']['messages']
        note = next(m for m in thread if m['author_role'] == 'internal')
        assert note['internal'] is True and note['body'] == 'Call the driver'
        assert not any(m['internal'] for m in thread if m['author_role'] != 'internal')


# ===========================================================================
# Macros
# ===========================================================================

class TestMacros:
    def test_only_the_macro_capability_writes(self, geo, world):
        support, _ = operator('support_agent', geo)
        body = {'title': 'Sorry', 'body': 'Hi {first_name}, ticket {ticket_id}.'}
        assert support.post(BASE + 'macros/', body, format='json').status_code == 403
        assert support.patch(BASE + 'macros/1/', {'title': 'x'}, format='json').status_code == 403
        admin, _ = operator(SUPER_ADMIN, geo)
        made = admin.post(BASE + 'macros/', body, format='json')
        assert made.status_code == 201, made.content
        assert AdminAuditLog.objects.filter(action='support_macro_created').count() == 1

    @pytest.mark.parametrize('bad', [
        'Hi {name}', 'Hi {first_name.__class__}', 'Hi {first_name!r}', 'Hi {first_name:>10}',
        'Hi {', 'Hi }', 'Hi {0}', 'Hi {}', 'Hi {ticket_id.real}',
    ])
    def test_unknown_placeholders_are_rejected_at_save(self, geo, bad):
        admin, _ = operator(SUPER_ADMIN, geo)
        r = admin.post(BASE + 'macros/', {'title': 'x', 'body': bad}, format='json')
        assert r.status_code == 400, bad
        assert r.json()['error']['code'] in ('UNKNOWN_PLACEHOLDER',), bad
        assert SupportMacro.objects.count() == 0

    def test_allowed_placeholders_render_server_side(self, geo, world):
        macro = SupportMacro.objects.create(
            title='t', body='Hi {first_name}, ticket #{ticket_id} about trip {trip_id}.')
        support, _ = operator('support_agent', geo)
        ticket = world['t_hyd']
        r = support.get(f'{BASE}macros/{macro.id}/preview/?ticket={ticket.id}')
        assert r.status_code == 200
        assert r.json()['data']['body'] == (
            f'Hi Lakshmi, ticket #{ticket.id} about trip {ticket.trip_id_id}.')
        # No trip: the placeholder is blank and reported.
        r = support.get(f'{BASE}macros/{macro.id}/preview/?ticket={world["t_newcomer"].id}')
        assert r.json()['data']['missing'] == ['trip_id']

    def test_reply_with_a_macro_renders_it_and_audits_the_id(self, geo, world):
        macro = SupportMacro.objects.create(title='t', body='Hi {first_name}, #{ticket_id}')
        support, _ = operator('support_agent', geo)
        ticket = world['t_hyd']
        r = post(support, ticket, 'reply/', {'macro_id': macro.id})
        assert r.status_code == 201
        assert r.json()['data']['body'] == f'Hi Lakshmi, #{ticket.id}'
        assert audit(ticket, 'support_ticket_replied').get().after['macro_id'] == macro.id

    def test_macros_are_filtered_by_ticket_country_locale_and_issue(self, geo, world):
        SupportMacro.objects.create(title='all', body='a')
        SupportMacro.objects.create(title='in-only', body='b', country='IN')
        SupportMacro.objects.create(title='us-only', body='c', country='US')
        SupportMacro.objects.create(title='fare', body='d', issue_types=['fare_dispute'])
        SupportMacro.objects.create(title='hindi', body='e', locale='hi')
        SupportMacro.objects.create(title='off', body='f', active=False)
        support, _ = operator('support_agent', geo)

        def titles(query):
            data = support.get(f'{BASE}macros/{query}').json()['data']['results']
            return {m['title'] for m in data}
        assert titles(f'?ticket={world["t_hyd"].id}') == {'all', 'in-only', 'fare', 'hindi'}
        assert titles(f'?ticket={world["t_newcomer"].id}') == {'all', 'in-only', 'hindi'}
        assert titles(f'?ticket={world["t_hyd"].id}&locale=hi') == {'hindi'}
        # A macro for another country cannot be used on this ticket.
        us = SupportMacro.objects.get(title='us-only')
        r = post(support, world['t_hyd'], 'reply/', {'macro_id': us.id})
        assert r.status_code == 400 and r.json()['error']['code'] == 'MACRO_NOT_FOR_TICKET'

    def test_edit_and_soft_delete_are_audited_and_idempotent(self, geo, world):
        admin, _ = operator(SUPER_ADMIN, geo)
        macro = SupportMacro.objects.create(title='old', body='Hi {first_name}')
        r = admin.patch(f'{BASE}macros/{macro.id}/', {'title': 'new', 'locale': 'te'}, format='json')
        assert r.status_code == 200 and r.json()['data']['locale'] == 'te'
        row = AdminAuditLog.objects.get(action='support_macro_updated')
        assert row.before['title'] == 'old' and row.after['title'] == 'new'
        assert admin.patch(f'{BASE}macros/{macro.id}/', {'title': 'new'}, format='json'
                           ).json()['data']['changed'] is False
        assert AdminAuditLog.objects.filter(action='support_macro_updated').count() == 1
        assert admin.delete(f'{BASE}macros/{macro.id}/').status_code == 200
        assert admin.delete(f'{BASE}macros/{macro.id}/').json()['data']['changed'] is False
        macro.refresh_from_db()
        assert macro.active is False  # soft
        assert AdminAuditLog.objects.filter(action='support_macro_deactivated').count() == 1

    def test_a_country_scoped_macro_manager_cannot_touch_other_countries(self, geo):
        AdminRole.objects.create(code='india_macros', name='India macros',
                                 permissions=['support.macro', 'support.read', 'support.reply'])
        client, user = operator('support_agent', geo, 'IN')
        AdminAssignment.objects.create(admin=user, role=AdminRole.objects.get(code='india_macros'),
                                       scope=geo['IN'])
        ok = client.post(BASE + 'macros/', {'title': 'in', 'body': 'x', 'country': 'IN'}, format='json')
        assert ok.status_code == 201, ok.content
        for country in ('US', None):
            r = client.post(BASE + 'macros/', {'title': 'x', 'body': 'x', 'country': country},
                            format='json')
            assert r.status_code == 403, country


# ===========================================================================
# Fare review: the existing trip refund, linked to the ticket
# ===========================================================================

@pytest.fixture
def policies(geo):
    call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=StringIO())


class TestFareReview:
    def refund(self, client, ticket, amount, key='r1'):
        return client.post(
            f'/api/v1/ride/admin/trips/{ticket.trip_id_id}/refund/',
            {'amount_minor': amount, 'reason': 'fare dispute', 'idempotency_key': key,
             'ticket_id': ticket.id}, format='json')

    def test_the_ticket_shows_the_fare_and_the_refund_state(self, geo, world, policies):
        client, _ = operator('support_agent', geo)
        data = client.get(url(world['t_hyd'])).json()['data']
        assert data['trip']['id'] == world['hyd_trip'].id
        assert data['trip']['fare']['currency'] == 'INR'
        assert data['refund']['refundable_minor'] == 40_000
        assert data['refund']['action_key'] == 'support.refund'
        assert data['refund']['policy']['maker_limit'] == 50_000
        assert data['requester']['page'] == {'type': 'rider', 'id': world['hyd_rider'].id}
        assert client.get(url(world['t_newcomer'])).json()['data']['trip'] is None

    def test_within_the_limit_it_runs_and_the_ticket_says_so(self, geo, world, policies):
        client, _ = operator('support_agent', geo)
        ticket = world['t_hyd']
        r = self.refund(client, ticket, 20_000)
        assert r.status_code == 200, r.content
        assert r.json()['data']['approval_request']['payload']['ticket_id'] == ticket.id
        assert f'#{ticket.id}' in r.json()['data']['approval_request']['request_reason']
        messages = list(SupportMessage.everything.filter(
            ticket=ticket, author_role='system').order_by('id'))
        # The same neutral line a waiting refund gets, then the "issued" line.
        assert len(messages) == 2
        assert messages[0].body.startswith("We're reviewing your refund request.")
        assert 'has been issued' in messages[1].body
        assert audit(ticket, 'support_ticket_refund_requested').count() == 1
        data = client.get(url(ticket)).json()['data']
        assert data['refund']['requests'][0]['money_action_status'] == 'completed'
        # A repeat with the same key is the same request: no second message.
        again = self.refund(client, ticket, 20_000)
        assert again.status_code == 200 and again.json()['data']['duplicate'] is True
        assert SupportMessage.everything.filter(ticket=ticket, author_role='system').count() == 2

    def test_over_the_limit_waits_for_a_checker_then_completes(self, geo, world, policies):
        client, _ = operator('support_agent', geo)
        ticket = world['t_hyd']
        from servers.ride.models import Trip

        Trip.objects.filter(pk=ticket.trip_id_id).update(
            estimated_fare_minor=90_000, final_fare_minor=90_000)
        r = self.refund(client, ticket, 60_000)
        assert r.status_code == 202, r.content
        assert r.json()['data']['state'] == 'pending_approval'
        request = ApprovalRequest.objects.get(pk=r.json()['data']['approval_request']['id'])
        assert request.status == REQUESTED
        pending = SupportMessage.everything.get(ticket=ticket, author_role='system')
        assert pending.body.startswith("We're reviewing your refund request.")
        data = client.get(url(ticket)).json()['data']
        assert data['refund']['requests'][0]['status'] == 'requested'
        # A checker (finance + approval.check) approves it; the ticket hears.
        checker, _ = operator(SUPER_ADMIN, geo)
        approved = checker.post(f'/api/v1/approvals/admin/requests/{request.id}/approve/',
                                {}, format='json')
        assert approved.status_code == 200, approved.content
        bodies = list(SupportMessage.everything.filter(
            ticket=ticket, author_role='system').order_by('id').values_list('body', flat=True))
        assert len(bodies) == 2 and 'has been issued' in bodies[1]

    def test_a_ticket_about_another_trip_cannot_be_linked(self, geo, world, policies):
        client, _ = operator('support_agent', geo)
        r = client.post(
            f'/api/v1/ride/admin/trips/{world["hyd_trip"].id}/refund/',
            {'amount_minor': 100, 'reason': 'x', 'idempotency_key': 'z',
             'ticket_id': world['t_newcomer'].id}, format='json')
        assert r.status_code == 400 and r.json()['error']['code'] == 'TICKET_TRIP_MISMATCH'

    def test_finance_may_not_review_a_fare_from_the_ticket_without_the_capability(
            self, geo, world, policies):
        client, _ = operator('safety_officer', geo)
        data = client.get(url(world['t_hyd'])).json()['data']
        assert data['allowed_actions']['refund'] is False

    def test_no_new_money_path(self):
        """Support imports no payment or wallet code and calls no refund executor."""
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent / 'servers' / 'support'
        for path in root.glob('*.py'):
            text = path.read_text(encoding='utf-8')
            assert not re.search(r'servers\.payments|rider\.credits|issue_refund_credit|Wallet',
                                 text), path.name


# ===========================================================================
# Masking
# ===========================================================================

class TestMasking:
    def test_contact_details_inside_text_are_masked(self, geo, world):
        ticket = mk(world['hyd_rider'], world['hyd_trip'], 'other',
                    'Call me on +91 98765 43210 or mail ravi.kumar@example.com')
        SupportMessage.objects.create(ticket=ticket, author=world['hyd_rider'], author_role='user',
                                      body='My other number is 9123456780, thanks')
        client, _ = operator('support_agent', geo)
        text = client.get(url(ticket)).content.decode()
        text += client.get(LIST).content.decode()
        for raw in ('9876543210', '98765 43210', 'ravi.kumar', '9123456780'):
            assert raw not in text, raw
        data = client.get(url(ticket)).json()['data']
        assert data['description'].startswith('Call me on ') and '3210' in data['description']
        assert 'thanks' in data['messages'][0]['body'] or 'thanks' in data['messages'][-1]['body']

    def test_operators_own_notes_are_masked_too(self, geo, world):
        client, _ = operator('support_agent', geo)
        ticket = world['t_hyd']
        post(client, ticket, 'note/', {'body': 'Driver says call 9988776655'})
        assert '9988776655' not in client.get(url(ticket)).content.decode()

    def test_the_requester_is_masked_and_the_thread_reveal_is_logged(self, geo, world):
        ticket = mk(world['hyd_rider'], world['hyd_trip'], 'other', 'ring 9876543210 please')
        support, _ = operator('support_agent', geo)
        data = support.get(url(ticket)).json()['data']
        assert '9876520001' not in str(data['requester'])
        reveal = lambda c: c.post('/api/v1/admin-access/reveal/', {  # noqa: E731
            'target_type': 'support_ticket', 'target_id': ticket.id,
            'fields': ['thread'], 'reason': 'call the rider'}, format='json')
        assert reveal(support).status_code == 403          # support holds no pii.view
        safety, _ = operator('safety_officer', geo)
        r = reveal(safety)
        assert r.status_code == 200, r.content
        assert '9876543210' in r.json()['data']['fields']['thread']['description']
        assert AdminAuditLog.objects.filter(action='pii_revealed',
                                            target_type='support_ticket').count() == 1

    def test_a_trip_less_rider_ticket_reveals_at_the_riders_anchors(self, geo, world):
        safety, _ = operator('safety_officer', geo)
        r = safety.post('/api/v1/admin-access/reveal/', {
            'target_type': 'support_ticket', 'target_id': world['t_newcomer'].id,
            'fields': ['requester_phone'], 'reason': 'cannot book'}, format='json')
        assert r.status_code == 200, r.content
        aus_safety, _ = operator('safety_officer', geo, 'US-TX-AUS')
        r = aus_safety.post('/api/v1/admin-access/reveal/', {
            'target_type': 'support_ticket', 'target_id': world['t_newcomer'].id,
            'fields': ['requester_phone'], 'reason': 'x'}, format='json')
        assert r.status_code == 404


# ===========================================================================
# List filters
# ===========================================================================

class TestFilters:
    def test_status_priority_issue_country_and_search(self, geo, world):
        client, _ = operator(SUPER_ADMIN, geo)
        post(client, world['t_hyd'], 'priority/', {'priority': 'high'})
        assert listed(client, '?priority=high') == {world['t_hyd'].id}
        assert listed(client, '?issue_type=account') == {
            world[k].id for k in ('t_newcomer', 't_us_newcomer', 't_nowhere')}
        assert listed(client, '?country=US') == {
            world[k].id for k in ('t_aus', 't_both_aus_trip', 't_us_newcomer', 't_aus_driver')}
        assert listed(client, f'?q={world["t_hyd"].id}') == {world['t_hyd'].id}
        assert listed(client, '?q=0004') == {world['t_newcomer'].id}      # the masked tail
        assert listed(client, '?q=%2B919876520004') == {world['t_newcomer'].id}
        assert listed(client, '?status=nonsense'.replace('nonsense', 'CLOSED')) == set()
        assert client.get(LIST + '?status=nonsense').status_code == 400
        assert client.get(LIST + '?breach=nonsense').status_code == 400

    def test_date_range_is_the_ticket_countrys_day(self, geo, world):
        client, _ = operator(SUPER_ADMIN, geo)
        today = timezone.now().date().isoformat()
        assert world['t_hyd'].id in listed(client, f'?date_from={today}')
        assert listed(client, '?date_to=2000-01-01') == set()
        assert client.get(LIST + '?date_from=2026-02-01&date_to=2026-01-01').status_code == 400

    def test_paging(self, geo, world):
        client, _ = operator(SUPER_ADMIN, geo)
        data = client.get(LIST + '?page_size=3').json()['data']
        assert data['count'] == len(TICKET_KEYS) and len(data['results']) == 3
        assert data['total_pages'] == 3


# ===========================================================================
# The model
# ===========================================================================

class TestModel:
    def test_the_rider_and_driver_app_payload_is_unchanged(self, geo, world):
        rider = _client_for(world['hyd_rider'])
        data = rider.get(f'{MY_TICKETS}{world["t_hyd"].id}/').json()['data']
        assert set(data) == {'id', 'user_id', 'trip_id', 'issue_type', 'status', 'description',
                             'assigned_to', 'created_at', 'updated_at', 'resolved_at', 'messages'}

    def test_creating_through_the_app_sets_the_clock(self, geo, world):
        rider = _client_for(world['hyd_rider'])
        r = rider.post(MY_TICKETS + 'create/', {'issue_type': 'safety',
                                                'description': 'unsafe driving'}, format='json')
        assert r.status_code == 201
        ticket = SupportTicket.objects.get(pk=r.json()['data']['id'])
        assert ticket.priority == 'urgent' and ticket.country == 'IN'
        assert ticket.first_response_due_at is not None
        assert Rider.objects.filter(user_id=world['hyd_rider']).exists()
