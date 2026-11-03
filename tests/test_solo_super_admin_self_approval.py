"""The sole Super Admin may check their own requests, until a second checker exists.

Owner decision (2026-11-03): every payout and maker-checker action was blocked,
because the owner is the only person who can approve and a maker can never
check their own request. With `APPROVALS_SOLO_SUPER_ADMIN` on, a Super Admin
may decide a request they raised -- with a reason, recorded as self-approved --
but only while nobody else could check it. These tests hold each edge of that.
"""

from decimal import Decimal
from io import StringIO
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import IntegrityError, transaction
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from conftest import seed_actor, seed_admin_access
from servers.admin_access.models import AdminAssignment, AdminRole, GeoNode
from servers.admin_audit.models import AdminAuditLog
from servers.approvals import registry, service
from servers.approvals.actions import FINANCE_PAYOUT, SUPPORT_REFUND
from servers.approvals.models import (
    APPROVED, EXECUTED, FAILED, REJECTED, REQUESTED, ApprovalPolicy, ApprovalRequest,
)
from servers.driver.models import Driver, WithdrawalRequest
from servers.rider.models import Wallet, get_wallet

pytestmark = pytest.mark.django_db

User = get_user_model()

API = '/api/v1/approvals/admin'
PAYOUT = '/api/v1/driver/admin/withdrawals'
PAYOUT_PATCH = 'servers.driver.services.trigger_payout_creation'
REASON = 'Sole approver until launch; checked the ledger.'

CALLS = []


def _counting_refund_executor(request):
    CALLS.append(request.id)
    return {'refunded': request.amount}


@pytest.fixture(autouse=True)
def _stand_in_refund():
    saved = registry._EXECUTORS.get(SUPPORT_REFUND)
    registry._EXECUTORS[SUPPORT_REFUND] = _counting_refund_executor
    CALLS.clear()
    yield
    if saved is None:
        registry._EXECUTORS.pop(SUPPORT_REFUND, None)
    else:
        registry._EXECUTORS[SUPPORT_REFUND] = saved


@pytest.fixture
def solo_on(settings):
    settings.APPROVALS_SOLO_SUPER_ADMIN = True


@pytest.fixture
def geo(db):
    seed_admin_access()
    out = StringIO()
    call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=out)
    return {node.code: node for node in GeoNode.objects.all()}


_seq = iter(range(1000))


def make_admin(email, **extra):
    i = next(_seq)
    phone = f'+9197711{i:05d}'
    extra.setdefault('role', 'admin')
    extra.setdefault('is_staff', True)
    return User.objects.create_user(phone_number=phone, username=phone, email=email,
                                    full_name=email.split('@')[0], **extra)


def assign(user, role_code, scope):
    return AdminAssignment.objects.create(
        admin=user, role=AdminRole.objects.get(code=role_code), scope=scope)


def client(user):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    return c


@pytest.fixture
def owner(geo):
    u = make_admin('owner@saaradhigo.test')
    assign(u, 'super_admin', geo['GLOBAL'])
    return u


def refund(actor, amount=10_000_000):
    """A refund far above any maker limit, so it always waits for a checker."""
    return service.require_approval_or_execute(
        SUPPORT_REFUND, actor=actor, country='IN', amount=amount, currency='INR',
        target_type='trip', target_id='T1', payload={'trip': 'T1'},
        idempotency_key=f'refund:{next(_seq)}',
    ).request


def _driver(phone, city, upi='qa@upi'):
    u = User.objects.create_user(phone_number=phone, username=phone, role='driver')
    d = Driver.objects.create(user_id=u, approved=True, upi_id=upi, home_city=city)
    w = get_wallet(u, Wallet.SCOPE_DRIVER)
    w.balance = Decimal('5000.00')
    w.save(update_fields=['balance_minor'])
    return d


def _withdrawal(driver):
    return WithdrawalRequest.objects.create(
        driver=driver, amount=Decimal('1000.00'), payout_method='upi', status='pending')


# ---------------------------------------------------------------------------
# Flag off: exactly as before
# ---------------------------------------------------------------------------

def test_flag_off_refuses_self_approval_as_today(owner):
    row = refund(owner)
    assert service.self_approval_allowed(row, owner) is False
    with pytest.raises(service.NotPermitted) as exc:
        service.approve(row, owner, reason=REASON)
    assert exc.value.code == 'SELF_APPROVAL'
    resp = client(owner).get(f'{API}/requests/{row.id}/').json()['data']
    assert resp['can_decide'] is False and resp['self_approval'] is False
    assert 'different person' in resp['cannot_decide_reason']


# ---------------------------------------------------------------------------
# Flag on, sole Super Admin
# ---------------------------------------------------------------------------

def test_sole_super_admin_may_approve_with_a_reason(solo_on, owner):
    row = refund(owner)
    assert service.self_approval_allowed(row, owner) is True

    detail = client(owner).get(f'{API}/requests/{row.id}/').json()['data']
    assert detail['can_decide'] is True and detail['self_approval'] is True

    resp = client(owner).post(f'{API}/requests/{row.id}/approve/', {'reason': REASON},
                              format='json')
    assert resp.status_code == 200, resp.content
    body = resp.json()['data']
    assert body['status'] == EXECUTED and body['self_approved'] is True
    assert CALLS == [row.id]

    row.refresh_from_db()
    assert row.checker_id == row.maker_id == owner.pk and row.self_approved
    audit = AdminAuditLog.objects.get(action='approval_self_approved', target_id=str(row.id))
    assert audit.actor_id == owner.pk
    assert audit.after['self_approved'] is True
    assert audit.after['APPROVALS_SOLO_SUPER_ADMIN'] is True
    assert audit.reason == REASON
    assert not AdminAuditLog.objects.filter(action='approval_approved',
                                            target_id=str(row.id)).exists()


@pytest.mark.parametrize('reason', ['', '   ', 'too short'])
def test_self_approval_without_a_real_reason_is_refused(solo_on, owner, reason):
    row = refund(owner)
    resp = client(owner).post(f'{API}/requests/{row.id}/approve/', {'reason': reason},
                              format='json')
    assert resp.status_code == 400
    assert resp.json()['error']['code'] == 'REASON_REQUIRED'
    row.refresh_from_db()
    assert row.status == REQUESTED and row.checker_id is None and CALLS == []


def test_sole_super_admin_may_reject_their_own_request_with_a_reason(solo_on, owner):
    row = refund(owner)
    with pytest.raises(service.InvalidRequest):
        service.reject(row, owner, reason='no')
    row = service.reject(row, owner, reason='Raised by mistake, withdrawn.')
    assert row.status == REJECTED and row.self_approved is True and CALLS == []


def test_own_requests_appear_in_waiting_for_me(solo_on, owner):
    row = refund(owner)
    data = client(owner).get(f'{API}/requests/?pending_for_me=1').json()['data']
    assert [r['id'] for r in data['results']] == [row.id]


# ---------------------------------------------------------------------------
# The exception switches itself off
# ---------------------------------------------------------------------------

def test_a_second_eligible_checker_ends_the_exception(solo_on, owner, geo):
    row = refund(owner)
    second = make_admin('second@saaradhigo.test')
    assign(second, 'super_admin', geo['GLOBAL'])

    assert service.self_approval_allowed(row, owner) is False
    with pytest.raises(service.NotPermitted) as exc:
        service.approve(row, owner, reason=REASON)
    assert exc.value.code == 'SELF_APPROVAL'
    # The second person can, as always.
    assert service.approve(row, second, reason=REASON).status == EXECUTED


def test_a_scoped_finance_checker_ends_it_only_where_they_can_check(solo_on, owner, geo):
    finance = set(AdminRole.objects.get(code='finance').permissions)
    AdminRole.objects.create(code='finance_checker', name='Finance (checker)',
                             permissions=sorted(finance | {'approval.check'}))
    hyd = make_admin('finance.hyd@saaradhigo.test')
    assign(hyd, 'finance_checker', geo['IN-TG-HYD'])

    # A refund is support.refund: the finance checker cannot check it.
    assert service.self_approval_allowed(refund(owner), owner) is True

    # A Hyderabad payout: they can, so the owner may not check their own.
    w = _withdrawal(_driver('+919800001901', geo['IN-TG-HYD']))
    with mock.patch(PAYOUT_PATCH, return_value=True):
        made = client(owner).post(f'{PAYOUT}/{w.id}/approve/', {'reason': 'release'},
                                  format='json')
    assert made.status_code == 202, made.content
    row = ApprovalRequest.objects.get(pk=made.json()['data']['approval_request']['id'])
    assert service.self_approval_allowed(row, owner) is False


def test_inactive_and_break_glass_accounts_do_not_count_as_checkers(solo_on, owner, geo):
    gone = make_admin('gone@saaradhigo.test', is_active=False)
    assign(gone, 'super_admin', geo['GLOBAL'])
    glass = make_admin('glass@saaradhigo.test', is_superuser=True, is_break_glass=True)
    assert glass.is_superuser
    assert service.self_approval_allowed(refund(owner), owner) is True


def test_another_superuser_counts_as_a_checker(solo_on, owner):
    make_admin('root@saaradhigo.test', is_superuser=True)
    assert service.self_approval_allowed(refund(owner), owner) is False


# ---------------------------------------------------------------------------
# Who may never use it
# ---------------------------------------------------------------------------

def test_a_break_glass_super_admin_is_refused(solo_on, geo):
    glass = make_admin('glass.maker@saaradhigo.test', is_break_glass=True)
    assign(glass, 'super_admin', geo['GLOBAL'])
    row = refund(glass)
    assert service.self_approval_allowed(row, glass) is False
    with pytest.raises(service.NotPermitted) as exc:
        service.approve(row, glass, reason=REASON)
    assert exc.value.code == 'SELF_APPROVAL'


def test_a_maker_who_is_not_super_admin_is_refused(solo_on, geo):
    role = AdminRole.objects.create(
        code='country_checker', name='Country checker',
        permissions=sorted(set(AdminRole.objects.get(code='support_agent').permissions)
                           | {'approval.check', 'report.read'}))
    maker = make_admin('country@saaradhigo.test')
    AdminAssignment.objects.create(admin=maker, role=role, scope=geo['IN'])
    row = refund(maker)
    assert service.self_approval_allowed(row, maker) is False
    with pytest.raises(service.NotPermitted) as exc:
        service.approve(row, maker, reason=REASON)
    assert exc.value.code == 'SELF_APPROVAL'


def test_someone_else_never_self_approves(solo_on, owner, geo):
    other = make_admin('other@saaradhigo.test')
    assign(other, 'support_agent', geo['IN-TG-HYD'])
    row = refund(other)
    assert service.self_approval_allowed(row, owner) is False  # owner is not the maker
    assert service.approve(row, owner, reason='').self_approved is False


# ---------------------------------------------------------------------------
# The database and the blockers
# ---------------------------------------------------------------------------

def test_the_database_refuses_maker_as_checker_unless_self_approved(owner):
    row = refund(owner)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            ApprovalRequest.objects.filter(pk=row.pk).update(
                status=APPROVED, checker_id=owner.pk, self_approved=False)
    ApprovalRequest.objects.filter(pk=row.pk).update(
        status=APPROVED, checker_id=owner.pk, self_approved=True)


def test_a_payout_with_a_blocker_stays_blocked(solo_on, owner, geo):
    w = _withdrawal(_driver('+919800001902', geo['IN-TG-HYD'], upi=''))
    with mock.patch(PAYOUT_PATCH, return_value=True) as dispatch:
        made = client(owner).post(f'{PAYOUT}/{w.id}/approve/', {'reason': 'release'},
                                  format='json')
        assert made.status_code == 202, made.content
        rid = made.json()['data']['approval_request']['id']
        detail = client(owner).get(f'{API}/requests/{rid}/').json()['data']
        assert detail['blockers'] and detail['self_approval'] is True
        resp = client(owner).post(f'{API}/requests/{rid}/approve/', {'reason': REASON},
                                  format='json')
    assert resp.status_code == 200, resp.content
    assert resp.json()['data']['status'] == FAILED
    assert dispatch.call_count == 0
    w.refresh_from_db()
    assert w.status == 'pending'


def test_sole_super_admin_releases_a_payout_end_to_end(solo_on, owner, geo):
    w = _withdrawal(_driver('+919800001903', geo['IN-TG-HYD']))
    with mock.patch(PAYOUT_PATCH, return_value=True) as dispatch:
        made = client(owner).post(f'{PAYOUT}/{w.id}/approve/', {'reason': 'release'},
                                  format='json')
        rid = made.json()['data']['approval_request']['id']
        resp = client(owner).post(f'{API}/requests/{rid}/approve/', {'reason': REASON},
                                  format='json')
    assert resp.status_code == 200, resp.content
    assert resp.json()['data']['status'] == EXECUTED
    assert resp.json()['data']['action_key'] == FINANCE_PAYOUT
    assert dispatch.call_count == 1
    w.refresh_from_db()
    assert w.status == 'approved'
    flipped = AdminAuditLog.objects.get(action='withdrawal_approved', target_id=str(w.id))
    assert flipped.actor_id == owner.pk
    assert ApprovalPolicy.objects.filter(country='IN', action_key=FINANCE_PAYOUT).exists()


def test_the_list_says_whether_the_exception_is_on_and_counts_self_decisions(owner, settings):
    row = refund(owner)
    off = client(owner).get(f'{API}/requests/?self_approved=1').json()['data']
    assert off['self_approval_on'] is False and off['count'] == 0

    settings.APPROVALS_SOLO_SUPER_ADMIN = True
    service.approve(row, owner, reason=REASON)
    refund(owner)  # still open: not counted
    on = client(owner).get(f'{API}/requests/?self_approved=1').json()['data']
    assert on['self_approval_on'] is True
    assert [r['id'] for r in on['results']] == [row.id]
