"""Maker-checker: request -> approve/reject -> execute, by two people, once.

WHAT THESE PROVE
----------------
The owner's rulings (AUDIT.md §10, §22, per-country section), each one
asserted against the service AND, where there is a route, over HTTP:

  * per-country thresholds in local-currency minor units: within the maker
    limit the action runs now with an audit row that says so; above it a
    request waits and NOTHING runs;
  * the checker is a DIFFERENT admin holding `approval.check` (and the
    action's own capability) in the request's scope -- self-approval is
    refused in code and by a database constraint; a missing capability is
    403; out of scope is 404, identical to a missing id;
  * approve executes exactly once: a repeat by the same checker returns the
    same outcome, a repeat by another is 409, a re-entrant approve from
    inside the executor does not run it twice, and two threads racing on
    PostgreSQL produce one execution;
  * reject, cancel and expiry never execute, and each is audited;
  * the USA has no thresholds, so a US action is BLOCKED with
    THRESHOLD_NOT_CONFIGURED -- and becomes possible only after a policy is
    written through `approvals.policy_change`, which itself takes a checker;
  * the one shipped executor: payout approval. The endpoint raises a
    request, a second finance admin approves it, the withdrawal flips and
    the provider dispatch is called once.

Only the executor's call count is trusted as evidence that something ran.
"""

import threading
from decimal import Decimal
from io import StringIO
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from conftest import seed_actor, seed_admin_access
from servers.admin_access.models import AdminAssignment, AdminRole, GeoNode
from servers.admin_access.roles import ROLE_TEMPLATES
from servers.admin_audit.models import AdminAuditLog
from servers.approvals import registry, service
from servers.approvals.actions import (
    ACTIONS, BULK_ACTION, DRIVER_DEACTIVATE, FINANCE_PAYOUT, POLICY_CHANGE, SUPPORT_REFUND,
)
from servers.approvals.models import (
    APPROVED, CANCELLED, EXECUTED, EXPIRED, FAILED, REJECTED, REQUESTED,
    ApprovalPolicy, ApprovalRequest,
)
from servers.driver.models import Driver, WithdrawalRequest
from servers.rider.models import Wallet, get_wallet

pytestmark = pytest.mark.django_db

User = get_user_model()

API = '/api/v1/approvals/admin'
PAYOUT_PATCH = 'servers.driver.services.trigger_payout_creation'

# ---------------------------------------------------------------------------
# Counting executors. The framework is tested against stand-ins, swapped in
# for each test: the Trips module now registers the real refund and bulk
# executors (servers.ride.admin_trips), which have their own tests.
# ---------------------------------------------------------------------------

CALLS = []


def _counting_refund_executor(request):
    CALLS.append(request.id)
    return {'refunded': request.amount, 'currency': request.currency}


DEACTIVATE_CALLS = []


def _counting_deactivate_executor(request):
    DEACTIVATE_CALLS.append(request.id)
    return {'deactivated': request.target_id}


def _bulk_executor(request):
    return {'records': request.amount}


STAND_INS = {
    SUPPORT_REFUND: _counting_refund_executor,
    DRIVER_DEACTIVATE: _counting_deactivate_executor,
    BULK_ACTION: _bulk_executor,
}


@pytest.fixture(autouse=True)
def _stand_in_executors():
    saved = {key: registry._EXECUTORS.get(key) for key in STAND_INS}
    registry._EXECUTORS.update(STAND_INS)
    yield
    for key, fn in saved.items():
        if fn is None:
            registry._EXECUTORS.pop(key, None)
        else:
            registry._EXECUTORS[key] = fn


@pytest.fixture(autouse=True)
def _reset_calls():
    CALLS.clear()
    DEACTIVATE_CALLS.clear()
    yield
    CALLS.clear()
    DEACTIVATE_CALLS.clear()


# ---------------------------------------------------------------------------
# People and places
# ---------------------------------------------------------------------------

@pytest.fixture
def geo(db):
    seed_admin_access()
    return {node.code: node for node in GeoNode.objects.all()}


@pytest.fixture
def policies(geo):
    """The India policies only.

    The seed now also writes the United States (owner, AUDIT 27.44; see
    tests/test_finance_actions.py). This file proves the FRAMEWORK's behaviour
    for a country with no policy, so the US rows are removed here."""
    out = StringIO()
    call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=out)
    ApprovalPolicy.objects.filter(country='US').delete()
    return out.getvalue()


@pytest.fixture
def raw_seed(geo):
    out = StringIO()
    call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=out)
    return out.getvalue()


def _template(code):
    return next(t for t in ROLE_TEMPLATES if t['code'] == code)


@pytest.fixture
def checker_roles(geo):
    """Roles that may check. Roles are data; `approval.check` is granted
    here to a Finance-shaped role rather than by editing the template."""
    finance = set(_template('finance')['permissions'])
    AdminRole.objects.create(
        code='finance_checker', name='Finance (checker)',
        permissions=sorted(finance | {'approval.check'}),
    )
    AdminRole.objects.create(
        code='checker_only', name='Checker with no action capability',
        permissions=['approval.check', 'report.read'],
    )
    AdminRole.objects.create(
        code='support_checker', name='Support (checker)',
        permissions=sorted(set(_template('support_agent')['permissions'])
                           | {'approval.check', 'report.read'}),
    )


_seq = iter(range(1000))


def make_admin(email, **extra):
    i = next(_seq)
    phone = f'+9197700{i:05d}'
    extra.setdefault('role', 'admin')
    extra.setdefault('is_staff', True)
    return User.objects.create_user(phone_number=phone, username=phone, email=email,
                                    full_name=email.split('@')[0], **extra)


def assign(user, role_code, scope):
    return AdminAssignment.objects.create(
        admin=user, role=AdminRole.objects.get(code=role_code), scope=scope,
    )


def client(user):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    return c


@pytest.fixture
def hyd_support(geo):
    u = make_admin('support.hyd@saaradhigo.test')
    assign(u, 'support_agent', geo['IN-TG-HYD'])
    return u


@pytest.fixture
def hyd_checker(geo, checker_roles):
    u = make_admin('checker.hyd@saaradhigo.test')
    assign(u, 'finance_checker', geo['IN-TG-HYD'])
    return u


@pytest.fixture
def second_hyd_checker(geo, checker_roles):
    u = make_admin('checker2.hyd@saaradhigo.test')
    assign(u, 'finance_checker', geo['IN-TG-HYD'])
    return u


@pytest.fixture
def austin_checker(geo, checker_roles):
    u = make_admin('checker.aus@saaradhigo.test')
    assign(u, 'finance_checker', geo['US-TX-AUS'])
    return u


@pytest.fixture
def hyd_finance(geo):
    u = make_admin('finance.hyd@saaradhigo.test')
    assign(u, 'finance', geo['IN-TG-HYD'])
    return u


def refund(actor, amount, *, country='IN', currency='INR', node=None, key=None, reason=''):
    return service.require_approval_or_execute(
        SUPPORT_REFUND, actor=actor, country=country, amount=amount, currency=currency,
        scope_node=node, target_type='trip', target_id='T1', payload={'trip': 'T1'},
        idempotency_key=key or f'refund:{amount}:{currency}:{next(_seq)}', reason=reason,
    )


def rows_for(request):
    return list(AdminAuditLog.objects.filter(
        target_type='approval_request', target_id=str(request.id)).order_by('id'))


def actions_for(request):
    return [r.action for r in rows_for(request)]


# ===========================================================================
# Policies: the seed
# ===========================================================================

class TestSeed:
    def test_india_defaults_are_unchanged_and_the_us_has_the_owners_set(self, raw_seed):
        active = {(p.country, p.action_key): p for p in ApprovalPolicy.objects.filter(is_active=True)}
        assert {k for k in active if k[0] == 'IN'} == {
            ('IN', 'support.refund'), ('IN', 'finance.refund'), ('IN', 'finance.adjust'),
            ('IN', 'finance.payout'), ('IN', 'pricing.change'), ('IN', 'driver.deactivate'),
            ('IN', 'bulk.action'), ('IN', 'support.sla_change'),
            ('IN', 'finance.payout_config_change'), ('IN', 'finance.wallet_credit'),
            ('IN', 'zone.change'),
            ('IN', 'notification.template_change'), ('IN', 'notification.campaign_send'),
            ('IN', 'promotion.launch'), ('IN', 'promotion.budget_increase'),
            ('IN', 'fraud.rule_change'),
        }
        assert {k for k in active if k[0] == 'US'} == {
            ('US', 'support.refund'), ('US', 'finance.refund'), ('US', 'finance.adjust'),
            ('US', 'finance.wallet_credit'), ('US', 'finance.payout'), ('US', 'bulk.action'),
            ('US', 'pricing.change'), ('US', 'driver.deactivate'), ('US', 'support.sla_change'),
            ('US', 'finance.payout_config_change'), ('US', 'zone.change'),
            ('US', 'notification.template_change'), ('US', 'notification.campaign_send'),
            ('US', 'promotion.launch'), ('US', 'promotion.budget_increase'),
            ('US', 'fraud.rule_change'),
        }
        assert (active['IN', 'support.refund'].maker_limit,
                active['IN', 'support.refund'].currency) == (50_000, 'INR')
        assert active['IN', 'finance.refund'].maker_limit == 500_000
        assert active['IN', 'finance.adjust'].maker_limit == 200_000
        for key in ('finance.payout', 'pricing.change', 'driver.deactivate'):
            assert active['IN', key].always_checker
            assert active['IN', key].currency == ''
        assert (active['IN', 'bulk.action'].limit_kind,
                active['IN', 'bulk.action'].maker_limit) == ('count', 50)
        assert (active['US', 'support.refund'].maker_limit, active['US', 'support.refund'].currency) == (1_500, 'USD')
        assert active['US', 'finance.refund'].maker_limit == 15_000
        assert active['US', 'finance.payout'].maker_limit == 100_000

    def test_the_seed_is_idempotent_and_never_overwrites(self, raw_seed):
        # A two-person decision changed a limit; a redeploy must not undo it.
        service.write_policy_version(
            country='IN', action_key='support.refund', limit_kind='money',
            currency='INR', maker_limit=10_000, created_by=seed_actor(), reason='test',
        )
        before = tuple(ApprovalPolicy.objects.values_list('id', 'is_active', 'maker_limit'))
        audit = AdminAuditLog.objects.count()

        call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=StringIO())

        assert tuple(ApprovalPolicy.objects.values_list('id', 'is_active', 'maker_limit')) == before
        assert AdminAuditLog.objects.count() == audit
        assert service.active_policy('IN', 'support.refund').maker_limit == 10_000

    def test_the_seed_is_refused_without_an_actor(self, geo):
        with pytest.raises(CommandError, match='--actor-email'):
            call_command('seed_approval_policies', stdout=StringIO())
        assert not ApprovalPolicy.objects.exists()

    def test_the_seed_writes_one_audit_row_naming_the_actor(self, geo):
        actor = seed_actor()
        call_command('seed_approval_policies', actor_email=actor.email, stdout=StringIO())
        row = AdminAuditLog.objects.get(action='approval_policies_seeded')
        assert row.actor_id == actor.pk
        assert row.actor_label == f'{actor.email} via seed_approval_policies'
        assert len(row.after['created']) == 32
        assert row.after['us_policies_written'] == 16

    def test_every_catalogue_action_is_a_known_capability(self):
        from servers.admin_access.roles import ALL_PERMISSIONS
        for action in ACTIONS.values():
            assert action.capability in ALL_PERMISSIONS, action.key


# ===========================================================================
# The decision
# ===========================================================================

class TestWithinAndAboveLimit:
    def test_within_limit_executes_now_and_audits_the_limit(self, policies, hyd_support, geo):
        outcome = refund(hyd_support, 30_000, node=geo['IN-TG-HYD'], reason='duplicate charge')

        assert outcome.executed and not outcome.duplicate
        assert CALLS == [outcome.request.id]
        row = outcome.request
        assert row.status == EXECUTED
        assert row.checker_id is None
        assert row.decision_reason == 'within maker limit'
        assert row.result == {'refunded': 30_000, 'currency': 'INR'}
        assert row.policy.maker_limit == 50_000
        assert actions_for(row) == ['approval_within_maker_limit', 'approval_executed']
        within = rows_for(row)[0]
        assert within.after['within_maker_limit'] is True
        assert within.after['maker_limit'] == 50_000
        assert within.actor_id == hyd_support.pk
        assert within.actor_email == hyd_support.email
        assert within.reason == 'duplicate charge'

    def test_exactly_the_limit_is_within_it(self, policies, hyd_support):
        assert refund(hyd_support, 50_000).executed
        assert len(CALLS) == 1

    def test_above_limit_creates_a_request_and_runs_nothing(self, policies, hyd_support, geo):
        outcome = refund(hyd_support, 50_001, node=geo['IN-TG-HYD'])

        assert not outcome.executed
        assert CALLS == []
        row = outcome.request
        assert row.status == REQUESTED
        assert row.maker_id == hyd_support.pk
        assert row.maker_email == hyd_support.email
        assert row.scope_node == geo['IN-TG-HYD']
        assert row.country == 'IN'
        assert row.expires_at > row.requested_at
        assert actions_for(row) == ['approval_requested']
        assert rows_for(row)[0].actor_id == hyd_support.pk

    def test_a_request_with_no_node_is_anchored_at_the_country(self, policies, hyd_support, geo):
        row = refund(hyd_support, 90_000).request
        assert row.scope_node == geo['IN']

    def test_currency_mismatch_is_refused_not_converted(self, policies, hyd_support):
        with pytest.raises(service.InvalidRequest) as exc:
            refund(hyd_support, 100, currency='USD')
        assert exc.value.code == 'CURRENCY_MISMATCH'
        assert CALLS == []

    def test_an_amount_is_required_where_the_policy_is_by_amount(self, policies, hyd_support):
        with pytest.raises(service.InvalidRequest) as exc:
            refund(hyd_support, None)
        assert exc.value.code == 'AMOUNT_REQUIRED'

    def test_a_float_or_bool_amount_is_a_defect(self, policies, hyd_support):
        for bad in (300.0, True):
            with pytest.raises(service.InvalidRequest) as exc:
                refund(hyd_support, bad)
            assert exc.value.code == 'AMOUNT_NOT_INTEGER'

    def test_an_unknown_action_is_refused(self, policies, hyd_support):
        with pytest.raises(service.InvalidRequest) as exc:
            service.require_approval_or_execute(
                'finance.payouts', actor=hyd_support, country='IN', amount=1,
                currency='INR', idempotency_key='typo')
        assert exc.value.code == 'UNKNOWN_ACTION'

    def test_a_count_threshold(self, policies, hyd_support, geo):
        kw = dict(actor=hyd_support, country='IN', scope_node=geo['IN-TG-HYD'])
        fifty = service.require_approval_or_execute(
            BULK_ACTION, amount=50, idempotency_key='bulk:50', **kw)
        fifty_one = service.require_approval_or_execute(
            BULK_ACTION, amount=51, idempotency_key='bulk:51', **kw)
        assert fifty.executed
        assert fifty_one.request.status == REQUESTED

    def test_always_checker_ignores_the_amount(self, policies, hyd_support, geo):
        outcome = service.require_approval_or_execute(
            DRIVER_DEACTIVATE, actor=hyd_support, country='IN', scope_node=geo['IN-TG-HYD'],
            target_type='driver', target_id='7', idempotency_key='deactivate:7')
        assert outcome.request.status == REQUESTED
        assert DEACTIVATE_CALLS == []

    def test_the_same_idempotency_key_returns_the_open_request(self, policies, hyd_support):
        first = refund(hyd_support, 90_000, key='refund:T1')
        again = refund(hyd_support, 90_000, key='refund:T1')
        assert again.duplicate and again.request.id == first.request.id
        assert ApprovalRequest.objects.count() == 1

    def test_a_non_operator_cannot_raise_a_request(self, policies):
        rider = User.objects.create_user(phone_number='+919000000001', role='rider')
        with pytest.raises(service.NotPermitted):
            refund(rider, 100)


class TestTheUsIsBlockedUntilConfigured:
    def test_a_us_action_is_refused_with_the_explicit_code(self, policies, austin_checker, geo):
        with pytest.raises(service.ThresholdNotConfigured) as exc:
            refund(austin_checker, 100, country='US', currency='USD', node=geo['US-TX-AUS'])
        assert exc.value.code == 'THRESHOLD_NOT_CONFIGURED'
        assert exc.value.status == 409
        assert CALLS == []
        assert not ApprovalRequest.objects.exists()

    def test_no_amount_is_small_enough(self, policies, austin_checker, geo):
        with pytest.raises(service.ThresholdNotConfigured):
            refund(austin_checker, 0, country='US', currency='USD', node=geo['US-TX-AUS'])

    def test_unblocked_only_by_a_two_person_policy_change(self, policies, geo, austin_checker):
        a = make_admin('sa.one@saaradhigo.test')
        b = make_admin('sa.two@saaradhigo.test')
        assign(a, 'super_admin', geo['GLOBAL'])
        assign(b, 'super_admin', geo['GLOBAL'])

        proposal = service.propose_policy_change(
            a, country='US', action_key=SUPPORT_REFUND, limit_kind='money',
            currency='USD', maker_limit=2_000, reason='US pilot: $20 support refund limit')
        assert proposal.request.status == REQUESTED
        with pytest.raises(service.ThresholdNotConfigured):
            refund(austin_checker, 100, country='US', currency='USD', node=geo['US-TX-AUS'])

        service.approve(proposal.request, b, reason='agreed')

        policy = service.active_policy('US', SUPPORT_REFUND)
        assert (policy.maker_limit, policy.currency, policy.version) == (2_000, 'USD', 1)
        assert policy.changed_via_id == proposal.request.id
        assert policy.created_by_id == a.pk
        assert refund(austin_checker, 1_500, country='US', currency='USD',
                      node=geo['US-TX-AUS']).executed
        assert not refund(austin_checker, 2_001, country='US', currency='USD',
                          node=geo['US-TX-AUS']).executed


# ===========================================================================
# Who may check
# ===========================================================================

@pytest.fixture
def pending(policies, hyd_support, geo):
    return refund(hyd_support, 75_000, node=geo['IN-TG-HYD'], key='refund:pending').request


class TestTheChecker:
    def test_self_approval_is_refused(self, policies, geo, checker_roles):
        maker = make_admin('maker.checker@saaradhigo.test')
        assign(maker, 'support_checker', geo['IN-TG-HYD'])
        row = refund(maker, 75_000, node=geo['IN-TG-HYD']).request

        with pytest.raises(service.NotPermitted) as exc:
            service.approve(row, maker, reason='me')
        assert exc.value.code == 'SELF_APPROVAL'
        with pytest.raises(service.NotPermitted):
            service.reject(row, maker, reason='me')

        resp = client(maker).post(f'{API}/requests/{row.id}/approve/', {}, format='json')
        assert resp.status_code == 403
        assert resp.json()['error']['code'] == 'SELF_APPROVAL'
        row.refresh_from_db()
        assert row.status == REQUESTED and CALLS == []

    def test_the_database_refuses_maker_equals_checker(self, pending, hyd_support):
        pending.set_checker(hyd_support)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                pending.save()

    def test_a_checker_without_approval_check_is_403(self, pending, hyd_finance):
        resp = client(hyd_finance).post(f'{API}/requests/{pending.id}/approve/', {}, format='json')
        assert resp.status_code == 403
        with pytest.raises(service.NotPermitted) as exc:
            service.approve(pending, hyd_finance)
        assert exc.value.code == 'CHECKER_LACKS_CAPABILITY'
        assert CALLS == []

    def test_a_checker_without_the_actions_capability_is_403(self, pending, geo, checker_roles):
        bystander = make_admin('bystander@saaradhigo.test')
        assign(bystander, 'checker_only', geo['IN-TG-HYD'])
        resp = client(bystander).post(f'{API}/requests/{pending.id}/approve/', {}, format='json')
        assert resp.status_code == 403
        assert resp.json()['error']['code'] == 'CHECKER_LACKS_ACTION_CAPABILITY'
        assert CALLS == []

    def test_a_checker_out_of_scope_gets_404_like_a_missing_id(self, pending, austin_checker):
        c = client(austin_checker)
        real = c.post(f'{API}/requests/{pending.id}/approve/', {}, format='json')
        missing = c.post(f'{API}/requests/999999/approve/', {}, format='json')
        assert real.status_code == missing.status_code == 404
        assert real.json()['error']['code'] == missing.json()['error']['code'] == 'NOT_FOUND'
        assert real.json()['error']['message'] == missing.json()['error']['message']
        with pytest.raises(service.NotInScope):
            service.approve(pending, austin_checker)
        assert CALLS == []

    def test_a_checker_in_scope_may_see_and_approve(self, pending, hyd_checker):
        c = client(hyd_checker)
        assert c.get(f'{API}/requests/{pending.id}/').status_code == 200
        resp = c.post(f'{API}/requests/{pending.id}/approve/', {'reason': 'ok'}, format='json')
        assert resp.status_code == 200, resp.content
        body = resp.json()['data']
        assert body['status'] == EXECUTED
        assert body['checker']['email'] == hyd_checker.email
        assert CALLS == [pending.id]

    def test_a_country_checker_covers_the_city(self, pending, geo, checker_roles):
        india = make_admin('checker.in@saaradhigo.test')
        assign(india, 'finance_checker', geo['IN'])
        service.approve(pending, india)
        assert CALLS == [pending.id]

    def test_a_rider_cannot_reach_the_api(self, pending):
        rider = User.objects.create_user(phone_number='+919000000002', role='rider')
        assert client(rider).get(f'{API}/requests/').status_code in (401, 403)


# ===========================================================================
# Exactly once
# ===========================================================================

class TestExactlyOnce:
    def test_a_second_approve_by_the_same_checker_returns_the_same_result(self, pending, hyd_checker):
        c = client(hyd_checker)
        first = c.post(f'{API}/requests/{pending.id}/approve/', {}, format='json')
        second = c.post(f'{API}/requests/{pending.id}/approve/', {}, format='json')
        assert first.status_code == second.status_code == 200
        assert first.json()['data']['result'] == second.json()['data']['result']
        assert first.json()['data']['executed_at'] == second.json()['data']['executed_at']
        assert CALLS == [pending.id]
        assert actions_for(pending).count('approval_executed') == 1

    def test_a_second_approve_by_another_checker_is_409(self, pending, hyd_checker, second_hyd_checker):
        service.approve(pending, hyd_checker)
        resp = client(second_hyd_checker).post(f'{API}/requests/{pending.id}/approve/', {}, format='json')
        assert resp.status_code == 409
        assert resp.json()['error']['code'] == 'ALREADY_DECIDED'
        assert CALLS == [pending.id]

    def test_a_re_entrant_approve_from_inside_the_executor_runs_once(
            self, policies, hyd_support, hyd_checker, geo):
        seen = []

        def reentrant(request):
            seen.append(request.id)
            # The executor itself (or something it calls) approves again.
            service.approve(request, hyd_checker)
            return {'ok': True}

        original = registry.executor_for(SUPPORT_REFUND)
        registry._EXECUTORS[SUPPORT_REFUND] = reentrant
        try:
            row = refund(hyd_support, 80_000, node=geo['IN-TG-HYD']).request
            service.approve(row, hyd_checker)
        finally:
            registry._EXECUTORS[SUPPORT_REFUND] = original

        row.refresh_from_db()
        assert seen == [row.id]
        assert row.status == EXECUTED

    def test_an_executor_failure_is_recorded_not_hidden(self, policies, hyd_support, hyd_checker, geo):
        def boom(request):
            raise RuntimeError('provider down')

        original = registry.executor_for(SUPPORT_REFUND)
        registry._EXECUTORS[SUPPORT_REFUND] = boom
        try:
            row = refund(hyd_support, 80_000, node=geo['IN-TG-HYD']).request
            resp = client(hyd_checker).post(f'{API}/requests/{row.id}/approve/', {}, format='json')
        finally:
            registry._EXECUTORS[SUPPORT_REFUND] = original

        assert resp.status_code == 200
        row.refresh_from_db()
        assert row.status == FAILED
        assert row.error_code == 'EXECUTOR_ERROR'
        assert 'provider down' in row.error
        assert actions_for(row)[-1] == 'approval_failed'
        # A failed request releases its key: the maker may try again.
        assert refund(hyd_support, 80_000, node=geo['IN-TG-HYD'],
                      key=row.idempotency_key).request.id != row.id

    def test_an_executor_refusal_is_a_named_failure(self, policies, hyd_support, hyd_checker, geo):
        def refuse(request):
            raise registry.ExecutionRefused('TARGET_GONE', 'the trip was deleted')

        original = registry.executor_for(SUPPORT_REFUND)
        registry._EXECUTORS[SUPPORT_REFUND] = refuse
        try:
            row = refund(hyd_support, 80_000, node=geo['IN-TG-HYD']).request
            service.approve(row, hyd_checker)
        finally:
            registry._EXECUTORS[SUPPORT_REFUND] = original
        row.refresh_from_db()
        assert (row.status, row.error_code) == (FAILED, 'TARGET_GONE')


@pytest.mark.postgres
@pytest.mark.django_db(transaction=True)
def test_two_threads_approving_at_once_execute_once(policies, hyd_support, hyd_checker, geo):
    """SELECT FOR UPDATE is a no-op on SQLite, so the race is only real here.

    Both threads are the same checker, so both are entitled to an answer:
    one makes the transition and runs the executor, the other waits on the
    lock, finds its own decision already made and returns it as it stands
    (`approved` if the executor is still running, `executed` if it is
    done). One execution, and the row ends executed.
    """
    row = refund(hyd_support, 80_000, node=geo['IN-TG-HYD'], key='refund:race').request
    gate = threading.Barrier(2)
    results, errors = [], []

    def go():
        try:
            gate.wait(timeout=10)
            results.append(service.approve(row, hyd_checker).status)
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(repr(exc))
        finally:
            connection.close()

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    assert len(results) == 2 and set(results) <= {APPROVED, EXECUTED}
    assert EXECUTED in results
    assert CALLS == [row.id]
    row.refresh_from_db()
    assert row.status == EXECUTED


# ===========================================================================
# Reject, cancel, expire
# ===========================================================================

class TestRejectCancelExpire:
    def test_reject_never_executes_and_needs_a_reason(self, pending, hyd_checker, second_hyd_checker):
        c = client(hyd_checker)
        bare = c.post(f'{API}/requests/{pending.id}/reject/', {}, format='json')
        assert bare.status_code == 400
        assert bare.json()['error']['code'] == 'REASON_REQUIRED'

        resp = c.post(f'{API}/requests/{pending.id}/reject/', {'reason': 'not owed'}, format='json')
        assert resp.status_code == 200
        pending.refresh_from_db()
        assert pending.status == REJECTED
        assert pending.checker_id == hyd_checker.pk
        assert pending.decision_reason == 'not owed'
        assert CALLS == []
        assert actions_for(pending) == ['approval_requested', 'approval_rejected']

        # Nobody can approve it afterwards, and the same checker's repeat is idempotent.
        after = client(second_hyd_checker).post(f'{API}/requests/{pending.id}/approve/', {}, format='json')
        assert after.status_code == 409
        again = c.post(f'{API}/requests/{pending.id}/reject/', {'reason': 'not owed'}, format='json')
        assert again.status_code == 200
        assert CALLS == []

    def test_a_rejected_key_may_be_raised_again(self, pending, hyd_checker, hyd_support, geo):
        service.reject(pending, hyd_checker, reason='wrong trip')
        fresh = refund(hyd_support, 75_000, node=geo['IN-TG-HYD'], key='refund:pending')
        assert not fresh.duplicate and fresh.request.id != pending.id

    def test_only_the_maker_may_cancel_and_only_while_open(self, pending, hyd_support, hyd_checker):
        other = client(hyd_checker).post(f'{API}/requests/{pending.id}/cancel/', {}, format='json')
        assert other.status_code == 403
        assert other.json()['error']['code'] == 'NOT_MAKER'

        # The maker is a support agent, who holds no report.read: the route
        # refuses them, and the service path is what a console acting for
        # them would call. Both are asserted.
        route = client(hyd_support).post(f'{API}/requests/{pending.id}/cancel/', {}, format='json')
        assert route.status_code == 403
        service.cancel(pending, hyd_support, reason='raised on the wrong trip')
        pending.refresh_from_db()
        assert pending.status == CANCELLED
        assert actions_for(pending)[-1] == 'approval_cancelled'

        with pytest.raises(service.WrongState) as exc:
            service.approve(pending, hyd_checker)
        assert exc.value.code == 'NOT_OPEN'
        assert CALLS == []
        assert service.cancel(pending, hyd_support).status == CANCELLED  # idempotent

    def test_an_expired_request_cannot_be_approved(self, pending, hyd_checker):
        ApprovalRequest.objects.filter(pk=pending.pk).update(
            expires_at=timezone.now() - timezone.timedelta(minutes=1))
        resp = client(hyd_checker).post(f'{API}/requests/{pending.id}/approve/', {}, format='json')
        assert resp.status_code == 409
        assert resp.json()['error']['code'] == 'EXPIRED'
        pending.refresh_from_db()
        assert pending.status == EXPIRED
        assert CALLS == []
        expired_row = rows_for(pending)[-1]
        assert expired_row.action == 'approval_expired'
        assert expired_row.actor_id is None
        assert expired_row.actor_label == 'system:approval_expiry'

    def test_the_sweep_expires_only_what_is_due(self, policies, hyd_support, geo):
        due = refund(hyd_support, 70_000, node=geo['IN-TG-HYD'], key='k1').request
        fresh = refund(hyd_support, 70_000, node=geo['IN-TG-HYD'], key='k2').request
        ApprovalRequest.objects.filter(pk=due.pk).update(
            expires_at=timezone.now() - timezone.timedelta(hours=1))

        assert service.expire_stale() == 1
        assert service.expire_stale() == 0
        due.refresh_from_db()
        fresh.refresh_from_db()
        assert (due.status, fresh.status) == (EXPIRED, REQUESTED)

    def test_the_ttl_is_a_setting(self, policies, hyd_support, settings):
        settings.APPROVAL_REQUEST_TTL_HOURS = 1
        row = refund(hyd_support, 70_000).request
        assert row.expires_at - row.requested_at == timezone.timedelta(hours=1)


# ===========================================================================
# Audit: every transition, with the actor as a snapshot
# ===========================================================================

class TestAudit:
    def test_the_full_approve_flow_is_on_the_record(self, pending, hyd_support, hyd_checker):
        service.approve(pending, hyd_checker, reason='looks right')
        rows = rows_for(pending)
        assert [r.action for r in rows] == [
            'approval_requested', 'approval_approved', 'approval_executed']
        requested, approved, executed = rows
        assert (requested.actor_id, requested.actor_email) == (hyd_support.pk, hyd_support.email)
        for row in (approved, executed):
            assert (row.actor_id, row.actor_email) == (hyd_checker.pk, hyd_checker.email)
            assert row.actor_label
            assert row.reason == 'looks right'
        assert approved.before['status'] == REQUESTED
        assert approved.after['status'] == APPROVED
        assert approved.after['checker_id'] == hyd_checker.pk
        assert executed.after['status'] == EXECUTED
        assert executed.after['result'] == {'refunded': 75_000, 'currency': 'INR'}

    def test_rows_survive_the_actor_being_renamed(self, pending, hyd_checker):
        service.approve(pending, hyd_checker)
        hyd_checker.email = 'someone.else@saaradhigo.test'
        hyd_checker.save(update_fields=['email'])
        assert rows_for(pending)[1].actor_email == 'checker.hyd@saaradhigo.test'

    def test_no_row_was_written_directly(self):
        """Every row goes through record_admin_action / audit_context."""
        import pathlib
        app = pathlib.Path(__file__).resolve().parents[1] / 'servers' / 'approvals'
        for path in app.rglob('*.py'):
            assert 'AdminAuditLog(' not in path.read_text(encoding='utf-8'), path
            assert 'AdminAuditLog.objects.create' not in path.read_text(encoding='utf-8'), path


# ===========================================================================
# Policy change is itself maker-checker
# ===========================================================================

class TestPolicyChange:
    @pytest.fixture
    def admins(self, geo, policies):
        a = make_admin('settings.a@saaradhigo.test')
        b = make_admin('settings.b@saaradhigo.test')
        assign(a, 'super_admin', geo['GLOBAL'])
        assign(b, 'super_admin', geo['GLOBAL'])
        return a, b

    def test_a_change_waits_for_a_second_admin(self, admins):
        a, b = admins
        before = service.active_policy('IN', 'support.refund')
        resp = client(a).post(f'{API}/policies/change/', {
            'country': 'IN', 'action_key': 'support.refund', 'limit_kind': 'money',
            'currency': 'INR', 'maker_limit': 100_000, 'reason': 'Diwali surge in refunds',
        }, format='json')
        assert resp.status_code == 202, resp.content
        req_id = resp.json()['data']['approval_request']['id']
        assert resp.json()['data']['approval_request']['action_key'] == POLICY_CHANGE
        assert service.active_policy('IN', 'support.refund').id == before.id

        # Self-approval of one's own threshold change: refused.
        own = client(a).post(f'{API}/requests/{req_id}/approve/', {}, format='json')
        assert own.status_code == 403

        ok = client(b).post(f'{API}/requests/{req_id}/approve/', {'reason': 'agreed'}, format='json')
        assert ok.status_code == 200, ok.content
        assert ok.json()['data']['status'] == EXECUTED

        now = service.active_policy('IN', 'support.refund')
        before.refresh_from_db()
        assert (now.maker_limit, now.version) == (100_000, before.version + 1)
        assert not before.is_active and before.valid_to is not None
        assert now.changed_via_id == req_id
        assert now.created_by_id == a.pk
        changed = AdminAuditLog.objects.get(action='approval_policy_changed')
        assert changed.actor_id == b.pk
        assert changed.before['maker_limit'] == 50_000
        assert changed.after['maker_limit'] == 100_000

    def test_a_rejected_change_writes_nothing(self, admins):
        a, b = admins
        proposal = service.propose_policy_change(
            a, country='IN', action_key='finance.payout', limit_kind='money',
            currency='INR', maker_limit=100_000, reason='let finance release small payouts')
        service.reject(proposal.request, b, reason='payout runs always take two')
        assert service.active_policy('IN', 'finance.payout').always_checker

    def test_the_threshold_must_be_in_local_currency(self, admins):
        a, _ = admins
        resp = client(a).post(f'{API}/policies/change/', {
            'country': 'IN', 'action_key': 'support.refund', 'limit_kind': 'money',
            'currency': 'USD', 'maker_limit': 1, 'reason': 'x',
        }, format='json')
        assert resp.status_code == 400
        assert resp.json()['error']['code'] == 'CURRENCY_NOT_LOCAL'

    def test_a_reason_is_required(self, admins):
        a, _ = admins
        resp = client(a).post(f'{API}/policies/change/', {
            'country': 'IN', 'action_key': 'support.refund', 'limit_kind': 'always',
        }, format='json')
        assert resp.status_code == 400
        assert resp.json()['error']['code'] == 'REASON_REQUIRED'

    def test_platform_settings_is_required(self, policies, hyd_finance):
        resp = client(hyd_finance).post(f'{API}/policies/change/', {
            'country': 'IN', 'action_key': 'support.refund', 'limit_kind': 'always', 'reason': 'x',
        }, format='json')
        assert resp.status_code == 403
        assert not ApprovalRequest.objects.exists()

    def test_the_built_in_rule_has_no_policy_row(self, admins):
        a, _ = admins
        with pytest.raises(service.InvalidRequest) as exc:
            service.propose_policy_change(
                a, country='IN', action_key=POLICY_CHANGE, limit_kind='always', reason='x')
        assert exc.value.code == 'ACTION_HAS_NO_POLICY'

    def test_policies_are_readable_with_report_read(self, policies, geo, hyd_support):
        analyst = make_admin('analyst@saaradhigo.test')
        assign(analyst, 'analyst', geo['IN-TG-HYD'])
        resp = client(analyst).get(f'{API}/policies/?country=IN')
        assert resp.status_code == 200
        assert len(resp.json()['data']['policies']) == 16
        assert client(hyd_support).get(f'{API}/policies/').status_code == 403


# ===========================================================================
# Listing and scope
# ===========================================================================

class TestListing:
    def test_lists_are_scoped_paged_and_filterable(self, policies, geo, checker_roles, hyd_support):
        for i in range(25):
            refund(hyd_support, 60_000 + i, node=geo['IN-TG-HYD'], key=f'list:{i}')
        hyd = make_admin('lister.hyd@saaradhigo.test')
        assign(hyd, 'finance_checker', geo['IN-TG-HYD'])
        aus = make_admin('lister.aus@saaradhigo.test')
        assign(aus, 'finance_checker', geo['US-TX-AUS'])

        page = client(hyd).get(f'{API}/requests/?pending_for_me=1').json()['data']
        assert page['count'] == 25 and len(page['results']) == 20
        assert client(aus).get(f'{API}/requests/').json()['data']['count'] == 0
        assert client(hyd).get(f'{API}/requests/?status=executed').json()['data']['count'] == 0
        assert client(hyd).get(f'{API}/requests/?status=nonsense').status_code == 400
        mine = client(hyd).get(f'{API}/requests/?mine=1').json()['data']
        assert mine['count'] == 0

    def test_the_approvals_routes_are_in_the_guarded_namespace(self):
        from test_admin_namespace_is_gated import admin_routes, ungated_admin_routes

        routes = {r for r, _ in admin_routes()}
        for expected in (
            'api/v1/approvals/admin/requests/',
            'api/v1/approvals/admin/requests/<int:request_id>/approve/',
            'api/v1/approvals/admin/requests/<int:request_id>/reject/',
            'api/v1/approvals/admin/requests/<int:request_id>/cancel/',
            'api/v1/approvals/admin/policies/',
            'api/v1/approvals/admin/policies/change/',
        ):
            assert expected in routes
        assert ungated_admin_routes() == {}


# ===========================================================================
# The shipped executor: payouts
# ===========================================================================

PAYOUT = '/api/v1/driver/admin/withdrawals'


def _driver(phone, city, balance='5000.00'):
    u = User.objects.create_user(phone_number=phone, username=phone, role='driver')
    d = Driver.objects.create(user_id=u, approved=True, upi_id='qa@upi', home_city=city)
    w = get_wallet(u, Wallet.SCOPE_DRIVER)
    w.balance = Decimal(balance)
    w.save(update_fields=['balance_minor'])
    return d


def _withdrawal(driver, amount='1000.00'):
    return WithdrawalRequest.objects.create(
        driver=driver, amount=Decimal(amount), payout_method='upi', status='pending')


class TestPayoutEndToEnd:
    def test_request_then_second_finance_admin_approves_then_one_dispatch(
            self, policies, geo, hyd_finance, hyd_checker):
        w = _withdrawal(_driver('+919800000901', geo['IN-TG-HYD']))

        with mock.patch(PAYOUT_PATCH, return_value=True) as dispatch:
            # The maker asks.
            resp = client(hyd_finance).post(f'{PAYOUT}/{w.id}/approve/', {'reason': 'weekly run'},
                                            format='json')
            assert resp.status_code == 202, resp.content
            body = resp.json()['data']
            req = body['approval_request']
            assert req['action_key'] == FINANCE_PAYOUT
            assert (req['amount'], req['currency'], req['country']) == (100_000, 'INR', 'IN')
            assert req['scope'] == 'IN-TG-HYD'
            assert req['target_id'] == str(w.id)
            assert body['withdrawal']['status'] == 'pending'
            w.refresh_from_db()
            assert w.status == 'pending'
            assert dispatch.call_count == 0

            # Asking again is the same request, not a second one.
            again = client(hyd_finance).post(f'{PAYOUT}/{w.id}/approve/', {}, format='json')
            assert again.status_code == 202
            assert again.json()['data']['approval_request']['id'] == req['id']
            assert again.json()['data']['duplicate'] is True

            # The maker cannot be the checker.
            own = client(hyd_finance).post(f"{API}/requests/{req['id']}/approve/", {}, format='json')
            assert own.status_code == 403

            # A different finance admin with approval.check approves.
            ok = client(hyd_checker).post(f"{API}/requests/{req['id']}/approve/",
                                          {'reason': 'checked against the ledger'}, format='json')
            assert ok.status_code == 200, ok.content
            assert ok.json()['data']['status'] == EXECUTED
            assert ok.json()['data']['result']['approved_ids'] == [w.id]
            assert ok.json()['data']['result']['payout_triggered_ids'] == [w.id]
            w.refresh_from_db()
            assert w.status == 'approved' and w.processed_at is not None
            assert dispatch.call_count == 1
            assert dispatch.call_args[0][0].id == w.id

            # Repeats change nothing.
            client(hyd_checker).post(f"{API}/requests/{req['id']}/approve/", {}, format='json')
            assert dispatch.call_count == 1

        flipped = AdminAuditLog.objects.get(action='withdrawal_approved', target_id=str(w.id))
        assert flipped.actor_id == hyd_checker.pk
        assert flipped.reason == 'checked against the ledger'

    def test_a_us_payout_is_blocked_until_the_us_is_configured(self, policies, geo, checker_roles):
        maker = make_admin('finance.aus@saaradhigo.test')
        assign(maker, 'finance', geo['US-TX-AUS'])
        w = _withdrawal(_driver('+12125550001', geo['US-TX-AUS']))

        with mock.patch(PAYOUT_PATCH) as dispatch:
            resp = client(maker).post(f'{PAYOUT}/{w.id}/approve/', {}, format='json')
        assert resp.status_code == 409
        assert resp.json()['error']['code'] == 'THRESHOLD_NOT_CONFIGURED'
        w.refresh_from_db()
        assert w.status == 'pending'
        assert dispatch.call_count == 0
        assert not ApprovalRequest.objects.exists()

    def test_out_of_scope_and_missing_withdrawals_still_answer_404(self, policies, geo, hyd_finance):
        w = _withdrawal(_driver('+12125550002', geo['US-TX-AUS']))
        c = client(hyd_finance)
        assert c.post(f'{PAYOUT}/{w.id}/approve/', {}, format='json').status_code == 404
        assert c.post(f'{PAYOUT}/999999/approve/', {}, format='json').status_code == 404
        assert not ApprovalRequest.objects.exists()

    def test_a_withdrawal_rejected_meanwhile_is_not_released(self, policies, geo, hyd_finance, hyd_checker):
        w = _withdrawal(_driver('+919800000902', geo['IN-TG-HYD']))
        req_id = client(hyd_finance).post(f'{PAYOUT}/{w.id}/approve/', {}, format='json') \
            .json()['data']['approval_request']['id']
        WithdrawalRequest.objects.filter(pk=w.pk).update(status='rejected')

        with mock.patch(PAYOUT_PATCH) as dispatch:
            resp = client(hyd_checker).post(f'{API}/requests/{req_id}/approve/', {}, format='json')
        assert resp.json()['data']['status'] == FAILED
        assert resp.json()['data']['error_code'] == 'WITHDRAWAL_NOT_PENDING'
        assert dispatch.call_count == 0

    def test_a_bulk_approve_is_one_request_for_the_run(self, policies, geo, hyd_finance, hyd_checker):
        d = _driver('+919800000903', geo['IN-TG-HYD'], balance='9000.00')
        w1, w2 = _withdrawal(d, '1000.00'), _withdrawal(d, '2000.00')
        done = _withdrawal(d, '10.00')
        WithdrawalRequest.objects.filter(pk=done.pk).update(status='rejected')

        with mock.patch(PAYOUT_PATCH, return_value=True) as dispatch:
            resp = client(hyd_finance).post(f'{PAYOUT}/bulk-action/', {
                'action': 'approve', 'withdrawal_ids': [w1.id, w2.id, done.id, 424242],
            }, format='json')
            assert resp.status_code == 202, resp.content
            body = resp.json()['data']
            assert body['requested_ids'] == sorted([w1.id, w2.id])
            assert sorted(body['skipped_ids']) == sorted([done.id, 424242])
            req = body['approval_request']
            assert (req['amount'], req['currency'], req['target_type']) == (300_000, 'INR', 'withdrawal_batch')
            assert WithdrawalRequest.objects.filter(status='pending').count() == 2
            assert dispatch.call_count == 0

            ok = client(hyd_checker).post(f"{API}/requests/{req['id']}/approve/", {}, format='json')
            assert ok.status_code == 200
            assert ok.json()['data']['result']['approved_ids'] == sorted([w1.id, w2.id])
            assert dispatch.call_count == 2
        assert WithdrawalRequest.objects.filter(status='approved').count() == 2

    def test_a_bulk_reject_still_works_without_a_checker(self, policies, geo, hyd_finance):
        d = _driver('+919800000904', geo['IN-TG-HYD'])
        w = _withdrawal(d)
        resp = client(hyd_finance).post(f'{PAYOUT}/bulk-action/', {
            'action': 'reject', 'withdrawal_ids': [w.id], 'admin_notes': 'kyc hold',
        }, format='json')
        assert resp.status_code == 200
        w.refresh_from_db()
        assert w.status == 'rejected'
        assert not ApprovalRequest.objects.exists()
