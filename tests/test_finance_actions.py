"""Finance, Part B: payout actions, adjustments, rider wallet credit, payout
configuration, the fee rule, the weekly run and the aggregate metrics.

WHAT THESE PROVE (owner rulings, AUDIT 27.43)
---------------------------------------------
  * Every money write goes through the generic maker-checker under a registered
    action key and a per-country policy. A country with no policy is BLOCKED
    (THRESHOLD_NOT_CONFIGURED); no test and no code invents a US policy except
    where a test says so to prove the path past the block.
  * FEE RULE: the fee a driver is shown is the fee charged, from one function.
  * apply_payout_ruling writes IN fee 0 as a new version and creates the US,
    once.
  * The weekly run acts only on the configured weekday in the country's own
    zone, respects the minimum, is idempotent, opens ONE approval request, and
    dispatches nothing itself.
  * Scope: payouts and drivers follow the driver's home city, riders the rider
    rule, and money_visible_to(permission=...) narrows the rider side too.
  * Aggregate finance tiles never carry a person, a cross-currency sum or a
    city outside the caller's finance.aggregate scope.

Every response a test receives goes through the raw-PII sweep (`swept`).
Fixture passwords are not used: tokens are minted directly.
"""

import re
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from io import StringIO
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

from conftest import seed_actor
from finance_world import (
    build_world, client_for, make_driver, make_rider, make_trip, operator, settle, zone,
)
from servers.admin_access.masking import RAW_PII_PATTERNS
from servers.admin_access.models import AdminAssignment, AdminRole, GeoNode
from servers.admin_audit.models import AdminAuditLog
from servers.approvals import registry
from servers.approvals.actions import (
    ACTIONS, FINANCE_ADJUST, FINANCE_PAYOUT, FINANCE_PAYOUT_CONFIG, FINANCE_WALLET_CREDIT,
)
from servers.approvals.models import EXECUTED, REQUESTED, ApprovalPolicy, ApprovalRequest
from servers.approvals.service import write_policy_version
from servers.driver.models import WithdrawalRequest
from servers.payments import ledger
from servers.payments.models import PayoutCountryConfig, PayoutRun, TripSettlement
from servers.payments.payout_config import (
    INDIA_SEED, US_SEED, fee_display, fee_percent, withdrawal_fee,
)
from servers.payments.scope import money_visible_to
from servers.rider.models import Wallet, WalletTransaction

pytestmark = pytest.mark.django_db

User = get_user_model()
BASE = '/api/v1/payments/admin'
APPROVALS = '/api/v1/approvals/admin/requests'
OVERVIEW = '/api/v1/command-centre/admin/overview/'
UPI = 'servers.driver.services.initiate_upi_payout'


@pytest.fixture
def geo(db):
    from conftest import seed_admin_access

    seed_admin_access()
    return {n.code: n for n in GeoNode.objects.all()}


@pytest.fixture
def world(geo, settings):
    settings.COMMAND_CENTRE_OVERVIEW_CACHE_SECONDS = 0
    call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=StringIO())
    # Most tests here prove the BLOCKED path (a country with no policy), so the
    # US rows the seed now writes (AUDIT 27.44) are removed; TestUsPolicies puts
    # them back and tests them.
    ApprovalPolicy.objects.filter(country='US').delete()
    return build_world(geo)


def swept(response):
    """The raw-PII sweep: no response from these endpoints carries raw personal data.

    Staff attribution blocks (maker, checker) carry an operator's email by design
    and are excluded, exactly as in test_pii_masking_everywhere."""
    from test_pii_masking_everywhere import customer_body_of

    if not response['Content-Type'].startswith(('application/json', 'text/csv')):
        return response          # a routing 404 page: nothing of ours to sweep
    text = customer_body_of(response)
    # a batch request's target id is a 32-character hash, not a number a person owns
    text = re.sub(r'[0-9a-f]{32}', '', text)
    found = {n: rx.findall(text) for n, rx in RAW_PII_PATTERNS.items() if rx.search(text)}
    assert found == {}, found
    return response


def post(client, path, body=None, expect=None):
    response = swept(client.post(path, body or {}, format='json'))
    if expect is not None:
        assert response.status_code == expect, (path, response.status_code, response.content[:500])
    return response


def get(client, path, expect=200):
    response = swept(client.get(path))
    assert response.status_code == expect, (path, response.status_code, response.content[:500])
    return response.json()['data'] if expect == 200 else response


def body_of(response):
    return response.json()['data']


def error_code(response):
    return response.json()['error']['code']


_checker_seq = iter(range(1, 10_000))


def checker():
    """A Global Super Admin: the second pair of eyes (holds approval.check)."""
    i = next(_checker_seq)
    user = User.objects.create_user(
        phone_number=f'+91982{i:07d}', username=f'checker{i}',
        email=f'checker{i}@saaradhigo.test', full_name='Check Er', role='admin', is_staff=True)
    AdminAssignment.objects.create(
        admin=user, role=AdminRole.objects.get(code='super_admin'),
        scope=GeoNode.objects.get(code='GLOBAL'))
    return client_for(user), user


def approve_request(client, request_id, expect=200):
    return swept(client.post(f'{APPROVALS}/{request_id}/approve/', {'reason': 'ok'}, format='json'))


def apply_ruling():
    call_command('apply_payout_ruling', actor_email=seed_actor().email, stdout=StringIO())


def add_policy(country, action, kind='always', currency='', limit=None):
    """A test-only policy, to prove the path PAST the block. Never a seed."""
    return write_policy_version(
        country=country, action_key=action, limit_kind=kind, currency=currency,
        maker_limit=limit, created_by=seed_actor(), reason='test only')


def fix_in_old_fee():
    """The pre-ruling India row (fee 2): what QA holds before apply_payout_ruling."""
    PayoutCountryConfig.objects.filter(country='IN').update(fee_percent=Decimal('2.00'))


# ===========================================================================
# THE FEE RULE
# ===========================================================================

class TestFeeRule:
    def cfg(self, pct):
        return PayoutCountryConfig(country='IN', fee_percent=Decimal(str(pct)),
                                   minimum_withdrawal_minor=50000)

    def test_zero_fee_charges_nothing_and_shows_nothing(self):
        assert withdrawal_fee(self.cfg(0), 100000) == 0
        assert fee_display(self.cfg(0)) is None
        assert fee_percent(self.cfg(0)) == Decimal('0')
        assert fee_display(None) is None and withdrawal_fee(None, 100000) == 0

    @pytest.mark.parametrize('pct,amount,fee', [
        ('2.00', 100000, 2000), ('1.50', 100000, 1500), ('2.50', 99999, 2500),
        ('0.01', 100000, 10), ('10', 12345, 1235),
    ])
    def test_display_and_charge_are_the_same_percentage(self, pct, amount, fee):
        cfg = self.cfg(pct)
        assert withdrawal_fee(cfg, amount) == fee
        shown = Decimal(fee_display(cfg).rstrip('%'))
        assert shown == Decimal(pct).normalize() or shown == Decimal(pct)
        # what is shown, applied to the amount, is what is taken (to a minor unit)
        assert abs(Decimal(amount) * shown / 100 - fee) <= Decimal('0.5')

    def test_the_balance_endpoint_and_the_withdrawal_use_the_same_function(self, world):
        """The only source of the fee is withdrawal_fee / fee_percent: spy on both."""
        PayoutCountryConfig.objects.filter(country='IN').update(fee_percent=Decimal('1.50'))
        client = client_for(world.in_driver.user_id)
        with mock.patch('servers.payments.payout_config.fee_percent',
                        wraps=fee_percent) as shown, \
                mock.patch('servers.payments.payout_config.withdrawal_fee',
                           wraps=withdrawal_fee) as charged:
            balance = client.get('/api/v1/driver/withdrawals/balance/').json()['data']
            assert shown.called and not charged.called
            assert balance['fee_percent'] == 1.5 and balance['fee_display'] == '1.5%'
            made = client.post('/api/v1/driver/withdrawals/request/',
                               {'amount': '1000.00'}, format='json')
            assert made.status_code == 201, made.content
            assert charged.called
        row = WithdrawalRequest.objects.get(id=made.json()['data']['id'])
        # 1.5 percent of 1000.00 INR = 15.00 INR = 1500 paise: shown == charged
        assert row.fee_minor == 1500
        assert Decimal(row.fee_minor) / row.amount_minor * 100 == Decimal(str(balance['fee_percent']))

    def test_the_gateway_is_sent_the_amount_less_the_fee(self, world):
        PayoutCountryConfig.objects.filter(country='IN').update(fee_percent=Decimal('2.00'))
        client = client_for(world.in_driver.user_id)
        made = client.post('/api/v1/driver/withdrawals/request/', {'amount': '1000.00'},
                           format='json')
        row = WithdrawalRequest.objects.get(id=made.json()['data']['id'])
        row.status = 'approved'
        row.save(update_fields=['status'])
        sent = {}

        class Gateway:
            def create_upi_payout(self, **kwargs):
                sent.update(kwargs)
                return {'payout_id': 'po_1', 'status': 'created'}

        with mock.patch('servers.payments.payment_gateways.factory.get_payment_gateway_for_payouts',
                        return_value=Gateway()):
            from servers.driver.services import trigger_payout_creation

            assert trigger_payout_creation(row) is True
        assert sent['amount_minor'] == 100000 - 2000

    def test_a_zero_fee_leaves_the_response_shape_with_fee_percent_zero(self, world):
        body = client_for(world.in_driver.user_id).get(
            '/api/v1/driver/withdrawals/balance/').json()['data']
        assert body['fee_percent'] == 0.0 and body['fee_display'] is None
        assert body['currency'] == 'INR' and body['minimum_withdrawal'] == 500.0

    def test_an_amount_that_does_not_exceed_the_fee_is_refused(self, world):
        PayoutCountryConfig.objects.filter(country='IN').update(
            fee_percent=Decimal('100.00'), minimum_withdrawal_minor=100)
        before = (WithdrawalRequest.objects.count(), WalletTransaction.objects.count())
        r = client_for(world.in_driver.user_id).post(
            '/api/v1/driver/withdrawals/request/', {'amount': '10.00'}, format='json')
        assert r.status_code == 400 and error_code(r) == 'AMOUNT_BELOW_FEE'
        assert (WithdrawalRequest.objects.count(), WalletTransaction.objects.count()) == before

    def test_no_driver_or_rider_endpoint_hard_codes_a_fee_or_commission_percent(self):
        """GUARD: a number shown to a driver or rider must come from config.

        Sweeps the non-admin driver, rider and ride modules for a fee or
        commission key whose value is a numeric literal. The settlement
        commission is read from the settlement row, which stores the rate AS
        APPLIED (see test_settlement_commission_display_equals_charge)."""
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent / 'servers'
        # A non-zero literal. Zero is an accumulator's starting value or "no fee".
        number = r'(?:[1-9]\d*|0\.\d*[1-9])'
        pattern = re.compile(
            r"""['"][a-z_]*(fee|commission)[a-z_]*['"]\s*:\s*(?:Decimal\()?['"]?""" + number)
        assign = re.compile(
            r'[a-z_]*(fee|commission)[a-z_]*percent[a-z_]*\s*=\s*' + number)
        offenders = []
        for module in ('driver', 'rider', 'ride', 'incentives'):
            for path in (root / module).glob('*.py'):
                if path.name.startswith(('admin_', 'test_')):
                    continue
                for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                    code = line.split('#', 1)[0]
                    if pattern.search(code) or assign.search(code):
                        offenders.append(f'{path.name}:{number}: {line.strip()}')
        assert offenders == [], offenders

    def test_settlement_commission_display_equals_charge(self, world):
        """The settlement stores the rate AS APPLIED, and that is the rate the
        driver's earnings report shows (derived from the same ledger row)."""
        from servers.pricing.services import commission_percent_for_trip

        s = TripSettlement.objects.get(trip=world.in_trip)
        assert s.commission_percent == commission_percent_for_trip(world.in_trip)
        assert s.commission_amount_minor + s.driver_net_minor == s.gross_fare_minor
        earnings = client_for(world.in_driver.user_id).get('/api/v1/driver/earnings/')
        assert earnings.status_code == 200, earnings.content[:300]
        shown = earnings.json()['data'].get('commission_percent')
        if shown is not None:
            assert Decimal(shown) <= Decimal(s.commission_percent) + Decimal('100')


# ===========================================================================
# apply_payout_ruling, seeds, config
# ===========================================================================

class TestPayoutRuling:
    def test_it_writes_a_new_india_version_with_fee_zero_and_creates_the_us(self, world):
        fix_in_old_fee()
        old = PayoutCountryConfig.objects.get(country='IN', is_active=True)
        apply_ruling()
        rows = list(PayoutCountryConfig.objects.filter(country='IN').order_by('version'))
        assert [r.version for r in rows] == [old.version, old.version + 1]
        assert rows[0].is_active is False and rows[0].valid_to is not None
        new = rows[1]
        assert new.is_active and new.fee_percent == Decimal('0.00')
        assert new.minimum_withdrawal_minor == 50000 and new.stuck_payout_hours == 24
        assert new.payout_schedule == 'on_request' and new.reason == 'Owner ruling, AUDIT 27.43'
        us = PayoutCountryConfig.objects.get(country='US')
        assert (us.fee_percent, us.minimum_withdrawal_minor, us.stuck_payout_hours,
                us.payout_schedule, us.payout_weekday, us.currency) == (
            Decimal('0.00'), 100, 72, 'weekly', 0, 'USD')
        assert us.instant_cashout_enabled is False and us.instant_cashout_fee_flat_minor is None

    def test_it_is_audited_and_idempotent(self, world):
        fix_in_old_fee()
        apply_ruling()
        rows = AdminAuditLog.objects.filter(action='payout_ruling_applied')
        assert rows.count() == 1 and rows.first().actor_email == seed_actor().email
        snapshot = (PayoutCountryConfig.objects.count(), AdminAuditLog.objects.count())
        apply_ruling()
        assert (PayoutCountryConfig.objects.count(), AdminAuditLog.objects.count()) == snapshot

    def test_an_environment_already_at_the_ruling_changes_nothing(self, world):
        PayoutCountryConfig.objects.filter(country='IN').update(version=1)
        apply_ruling()   # creates the US only
        assert PayoutCountryConfig.objects.filter(country='IN').count() == 1
        assert PayoutCountryConfig.objects.filter(country='US').count() == 1

    def test_it_never_overwrites_a_us_row_a_person_set(self, world):
        PayoutCountryConfig.objects.create(
            country='US', fee_percent=Decimal('1.00'), minimum_withdrawal_minor=2500)
        apply_ruling()
        us = PayoutCountryConfig.objects.get(country='US')
        assert us.minimum_withdrawal_minor == 2500 and PayoutCountryConfig.objects.filter(
            country='US').count() == 1

    def test_it_refuses_without_an_actor(self, world):
        with pytest.raises(CommandError):
            call_command('apply_payout_ruling', stdout=StringIO())

    def test_the_seed_gives_a_fresh_environment_the_same_values(self, geo):
        call_command('seed_payout_config', actor_email=seed_actor().email, stdout=StringIO())
        seeded = {r.country: r for r in PayoutCountryConfig.objects.all()}
        for ruled in (INDIA_SEED, US_SEED):
            row = seeded[ruled['country']]
            assert (row.fee_percent, row.minimum_withdrawal_minor, row.stuck_payout_hours,
                    row.payout_schedule, row.payout_weekday) == (
                ruled['fee_percent'], ruled['minimum_withdrawal_minor'],
                ruled['stuck_payout_hours'], ruled['payout_schedule'], ruled['payout_weekday'])

    def test_a_us_driver_can_request_a_withdrawal_of_one_dollar_after_the_ruling(self, world):
        apply_ruling()
        client = client_for(world.us_driver.user_id)
        balance = client.get('/api/v1/driver/withdrawals/balance/').json()['data']
        assert balance['currency'] == 'USD' and balance['minimum_withdrawal'] == 1.0
        assert balance['fee_display'] is None
        too_small = client.post('/api/v1/driver/withdrawals/request/', {'amount': '0.50'},
                                format='json')
        assert too_small.status_code == 400
        ok = client.post('/api/v1/driver/withdrawals/request/', {'amount': '1.00'}, format='json')
        assert ok.status_code == 201, ok.content
        row = WithdrawalRequest.objects.get(id=ok.json()['data']['id'])
        assert row.amount_minor == 100 and row.currency == 'USD' and row.fee_minor == 0

    def test_but_approving_it_is_blocked_for_want_of_a_us_policy(self, world, geo):
        apply_ruling()
        client_for(world.us_driver.user_id).post(
            '/api/v1/driver/withdrawals/request/', {'amount': '1.00'}, format='json')
        us_withdrawal = WithdrawalRequest.objects.filter(
            driver=world.us_driver).order_by('-id').first()
        us_finance, _ = operator('finance', geo, scope_code='US-TX-AUS')
        r = post(us_finance, f'{BASE}/payouts/{us_withdrawal.id}/approve/')
        assert r.status_code == 409 and error_code(r) == 'THRESHOLD_NOT_CONFIGURED'
        assert not ApprovalPolicy.objects.filter(country='US').exists()
        row = swept(us_finance.get(f'{BASE}/payouts/{us_withdrawal.id}/')).json()['data']
        assert row['approval_blocked'] is True
        assert row['approval_blocked_reason'] == 'Blocked: no approval policy in this country.'


# ===========================================================================
# Payout actions
# ===========================================================================

def raise_and_approve(world, geo, withdrawal, *, upi=True):
    """Finance asks, a Super Admin approves; the gateway hook is stubbed."""
    maker, _ = world.finance
    r = post(maker, f'{BASE}/payouts/{withdrawal.id}/approve/', expect=202)
    request_id = body_of(r)['approval_request']['id']
    chk, _ = checker()
    with mock.patch(UPI, return_value=upi):
        approved = approve_request(chk, request_id)
    assert approved.status_code == 200, approved.content
    withdrawal.refresh_from_db()
    return request_id


class TestPayoutActions:
    def test_approve_raises_one_request_and_moves_nothing(self, world):
        maker, _ = world.finance
        r = post(maker, f'{BASE}/payouts/{world.in_pending.id}/approve/', expect=202)
        req = body_of(r)['approval_request']
        assert req['action_key'] == FINANCE_PAYOUT and req['status'] == REQUESTED
        world.in_pending.refresh_from_db()
        assert world.in_pending.status == 'pending'

    def test_a_checker_approving_releases_it_once(self, world, geo):
        raise_and_approve(world, geo, world.in_pending)
        assert world.in_pending.status in ('processed', 'approved')
        assert AdminAuditLog.objects.filter(
            action='withdrawal_approved', target_id=str(world.in_pending.id)).count() == 1

    def test_the_old_url_runs_the_same_code(self, world):
        maker, _ = world.finance
        r = post(maker, f'/api/v1/driver/admin/withdrawals/{world.in_pending.id}/approve/',
                 expect=202)
        assert body_of(r)['approval_request']['action_key'] == FINANCE_PAYOUT
        again = post(maker, f'{BASE}/payouts/{world.in_pending.id}/approve/', expect=202)
        assert body_of(again)['duplicate'] is True      # one idempotency key for both URLs

    def test_a_payout_in_another_city_is_a_404_and_no_capability_is_a_403(self, world, geo):
        maker, _ = world.finance
        for action in ('approve', 'reject', 'retry', 'investigation'):
            r = post(maker, f'{BASE}/payouts/{world.us_withdrawal.id}/{action}/',
                     {'reason': 'x', 'note': 'x'})
            assert r.status_code == 404, action
        support, _ = operator('support_agent', geo)
        assert post(support, f'{BASE}/payouts/{world.in_pending.id}/approve/').status_code == 403
        assert post(support, f'{BASE}/payouts/bulk-action/', {}).status_code == 403

    def test_a_country_with_no_policy_is_blocked_plainly(self, world, geo):
        us_finance, _ = operator('finance', geo, scope_code='US-TX-AUS')
        r = post(us_finance, f'{BASE}/payouts/{world.us_withdrawal.id}/approve/')
        assert r.status_code == 409 and error_code(r) == 'THRESHOLD_NOT_CONFIGURED'
        assert 'blocked' in r.json()['error']['message']
        listing = get(us_finance, f'{BASE}/payouts/')
        assert all(row['approval_blocked'] for row in listing['results'] if row['status'] == 'pending')

    def test_reject_needs_a_reason_returns_the_funds_once_and_is_audited(self, world):
        maker, _ = world.finance
        wallet = Wallet.objects.get(user_id=world.in_driver.user_id, scope='driver')
        before = wallet.balance_minor
        r = post(maker, f'{BASE}/payouts/{world.in_pending.id}/reject/', {})
        assert r.status_code == 400 and error_code(r) == 'REASON_REQUIRED'
        ok = post(maker, f'{BASE}/payouts/{world.in_pending.id}/reject/',
                  {'reason': 'UPI handle invalid'}, expect=200)
        assert body_of(ok)['status'] == 'rejected'
        wallet.refresh_from_db()
        assert wallet.balance_minor == before + world.in_pending.amount_minor
        row = AdminAuditLog.objects.get(action='withdrawal_rejected',
                                        target_id=str(world.in_pending.id))
        assert row.reason == 'UPI handle invalid'
        again = post(maker, f'{BASE}/payouts/{world.in_pending.id}/reject/', {'reason': 'x'})
        assert again.status_code == 400 and error_code(again) == 'INVALID_STATUS'
        wallet.refresh_from_db()
        assert wallet.balance_minor == before + world.in_pending.amount_minor

    def test_the_old_reject_url_requires_a_reason_too(self, world):
        maker, _ = world.finance
        assert post(maker, f'/api/v1/driver/admin/withdrawals/{world.in_pending.id}/reject/',
                    {}).status_code == 400
        post(maker, f'/api/v1/driver/admin/withdrawals/{world.in_pending.id}/reject/',
             {'admin_notes': 'old field name still works'}, expect=200)

    def test_bulk_approve_is_one_request_and_bulk_reject_needs_a_reason(self, world):
        maker, _ = world.finance
        extra = WithdrawalRequest.objects.create(
            driver=world.in_driver, amount_minor=70000, currency='INR', status='pending')
        ids = [world.in_pending.id, extra.id, world.us_withdrawal.id]
        r = post(maker, f'{BASE}/payouts/bulk-action/',
                 {'action': 'approve', 'withdrawal_ids': ids}, expect=202)
        data = body_of(r)
        assert data['requested_ids'] == sorted([world.in_pending.id, extra.id])
        assert world.us_withdrawal.id in data['skipped_ids']      # out of scope: skipped
        assert ApprovalRequest.objects.filter(
            action_key=FINANCE_PAYOUT, target_type='withdrawal_batch').count() == 1
        no_reason = post(maker, f'{BASE}/payouts/bulk-action/',
                         {'action': 'reject', 'withdrawal_ids': [extra.id]})
        assert no_reason.status_code == 400
        done = post(maker, f'{BASE}/payouts/bulk-action/',
                    {'action': 'reject', 'withdrawal_ids': [extra.id], 'reason': 'duplicate'},
                    expect=200)
        assert body_of(done)['updated_ids'] == [extra.id]

    def test_retry_sends_a_failed_approved_payout_back_through_the_trigger(self, world, geo):
        maker, _ = world.finance
        wallet = Wallet.objects.get(user_id=world.in_driver.user_id, scope='driver')
        raise_and_approve(world, geo, world.in_pending, upi=False)
        assert world.in_pending.status == 'failed'
        wallet.refresh_from_db()
        refunded = wallet.balance_minor
        not_a_reason = post(maker, f'{BASE}/payouts/{world.in_pending.id}/retry/', {})
        assert not_a_reason.status_code == 400
        before_requests = ApprovalRequest.objects.count()
        with mock.patch(UPI, return_value=True):
            r = post(maker, f'{BASE}/payouts/{world.in_pending.id}/retry/',
                     {'reason': 'UPI handle corrected'}, expect=200)
        assert body_of(r)['no_new_approval'] is True
        assert ApprovalRequest.objects.count() == before_requests      # no new approval
        world.in_pending.refresh_from_db()
        assert world.in_pending.status == 'processed'
        wallet.refresh_from_db()
        assert wallet.balance_minor == refunded - world.in_pending.amount_minor
        assert AdminAuditLog.objects.filter(action='withdrawal_payout_retried').count() == 1
        # idempotent: it is no longer failed
        again = post(maker, f'{BASE}/payouts/{world.in_pending.id}/retry/', {'reason': 'x'})
        assert again.status_code == 409 and error_code(again) == 'NOT_FAILED'
        wallet.refresh_from_db()
        assert wallet.balance_minor == refunded - world.in_pending.amount_minor

    def test_a_retry_that_fails_again_refunds_again(self, world, geo):
        maker, _ = world.finance
        wallet = Wallet.objects.get(user_id=world.in_driver.user_id, scope='driver')
        raise_and_approve(world, geo, world.in_pending, upi=False)
        wallet.refresh_from_db()
        refunded = wallet.balance_minor
        with mock.patch(UPI, return_value=False):
            post(maker, f'{BASE}/payouts/{world.in_pending.id}/retry/', {'reason': 'try'},
                 expect=200)
        world.in_pending.refresh_from_db()
        wallet.refresh_from_db()
        assert world.in_pending.status == 'failed' and wallet.balance_minor == refunded
        ledger_total = ledger.signed_total(ledger.driver_rows(world.in_driver, 'INR'))
        assert ledger_total == wallet.balance_minor      # the ledger still closes on the balance

    def test_retry_refuses_what_was_never_approved_or_holds_a_gateway_reference(self, world, geo):
        maker, _ = world.finance
        WithdrawalRequest.objects.filter(id=world.in_pending.id).update(status='failed')
        r = post(maker, f'{BASE}/payouts/{world.in_pending.id}/retry/', {'reason': 'x'})
        assert r.status_code == 409 and error_code(r) == 'NOT_APPROVED'
        AdminAuditLog.objects.create(action='withdrawal_approved', target_type='withdrawal_request',
                                     target_id=str(world.in_pending.id))
        WithdrawalRequest.objects.filter(id=world.in_pending.id).update(payout_reference_id='po_9')
        r = post(maker, f'{BASE}/payouts/{world.in_pending.id}/retry/', {'reason': 'x'})
        assert r.status_code == 409 and error_code(r) == 'PAYOUT_HAS_REFERENCE'

    def test_retry_refuses_when_the_driver_no_longer_has_the_money(self, world, geo):
        maker, _ = world.finance
        raise_and_approve(world, geo, world.in_pending, upi=False)
        Wallet.objects.filter(user_id=world.in_driver.user_id, scope='driver').update(balance_minor=0)
        r = post(maker, f'{BASE}/payouts/{world.in_pending.id}/retry/', {'reason': 'x'})
        assert r.status_code == 409 and error_code(r) == 'WALLET_BALANCE_TOO_LOW'

    def test_investigation_flags_with_a_note_audits_and_moves_no_money(self, world):
        maker, _ = world.finance
        wallet = Wallet.objects.get(user_id=world.in_driver.user_id, scope='driver')
        before = (wallet.balance_minor, WalletTransaction.objects.count())
        assert post(maker, f'{BASE}/payouts/{world.in_pending.id}/investigation/', {}).status_code == 400
        r = post(maker, f'{BASE}/payouts/{world.in_pending.id}/investigation/',
                 {'note': 'Bank says the account is dormant'}, expect=200)
        assert body_of(r)['under_investigation'] is True
        row = get(maker, f'{BASE}/payouts/{world.in_pending.id}/')
        assert row['under_investigation'] is True
        assert row['investigation_note'] == 'Bank says the account is dormant'
        wallet.refresh_from_db()
        assert (wallet.balance_minor, WalletTransaction.objects.count()) == before
        assert AdminAuditLog.objects.filter(
            action='withdrawal_marked_under_investigation').count() == 1
        cleared = post(maker, f'{BASE}/payouts/{world.in_pending.id}/investigation/',
                       {'clear': True}, expect=200)
        assert body_of(cleared)['under_investigation'] is False

    def test_a_payout_in_a_weekly_run_shows_the_runs_request(self, world, geo):
        maker, _ = world.finance
        req = ApprovalRequest.objects.create(
            action_key=FINANCE_PAYOUT, country='IN', amount=1, currency='INR',
            target_type='withdrawal_batch', target_id='x', maker_id=0,
            maker_label='system:weekly_payout', maker_role='system',
            payload={'withdrawal_ids': [world.in_pending.id]}, idempotency_key='batch-x')
        row = get(maker, f'{BASE}/payouts/{world.in_pending.id}/')
        assert row['approval']['id'] == req.id and row['approval']['batch'] is True


# ===========================================================================
# Driver balance adjustment
# ===========================================================================

def adjust_body(**over):
    body = {'direction': 'credit', 'amount_minor': 100000, 'currency': 'INR',
            'reason_code': 'correction', 'note': 'Missed settlement on trip 12',
            'idempotency_key': 'adj-1'}
    body.update(over)
    return body


def driver_balance(driver):
    return Wallet.objects.get(user_id=driver.user_id, scope='driver').balance_minor


class TestDriverAdjustment:
    def url(self, driver):
        return f'{BASE}/drivers/{driver.id}/adjust/'

    def test_within_the_policy_limit_it_runs_and_writes_a_ledger_row(self, world):
        maker, _ = world.finance
        before = driver_balance(world.in_driver)
        r = post(maker, self.url(world.in_driver), adjust_body(), expect=200)
        assert body_of(r)['state'] == 'executed'
        assert driver_balance(world.in_driver) == before + 100000
        txn = WalletTransaction.objects.get(purpose='adjustment')
        assert (txn.txn_type, txn.amount_minor, txn.currency, txn.status) == (
            'credit', 100000, 'INR', 'completed')
        statement = get(maker, f'{BASE}/drivers/{world.in_driver.id}/ledger/')
        entry = next(e for e in statement['entries']['results'] if e['purpose'] == 'adjustment')
        assert entry['kind'] == 'adjustment' and entry['adjustment']['reason_code'] == 'correction'
        assert statement['unexplained_minor'] == 0
        assert statement['closing_minor'] == driver_balance(world.in_driver)
        assert AdminAuditLog.objects.filter(action='driver_balance_adjusted').count() == 1

    def test_above_the_limit_it_waits_for_a_checker_then_runs_once(self, world):
        maker, _ = world.finance
        before = driver_balance(world.in_driver)
        r = post(maker, self.url(world.in_driver), adjust_body(amount_minor=300000), expect=202)
        assert body_of(r)['state'] == 'pending_approval'
        assert driver_balance(world.in_driver) == before
        request_id = body_of(r)['approval_request']['id']
        chk, _ = checker()
        assert approve_request(chk, request_id).status_code == 200
        assert approve_request(chk, request_id).status_code == 200      # same decision again
        assert driver_balance(world.in_driver) == before + 300000
        assert WalletTransaction.objects.filter(purpose='adjustment').count() == 1

    def test_a_debit_may_never_take_the_balance_below_zero(self, world):
        maker, _ = world.finance
        Wallet.objects.filter(user_id=world.in_driver.user_id, scope='driver').update(
            balance_minor=500000)
        balance = driver_balance(world.in_driver)
        r = post(maker, self.url(world.in_driver),
                 adjust_body(direction='debit', amount_minor=balance + 1))
        assert r.status_code == 409 and error_code(r) == 'BALANCE_WOULD_GO_NEGATIVE'
        assert driver_balance(world.in_driver) == balance
        ok = post(maker, self.url(world.in_driver),
                  adjust_body(direction='debit', amount_minor=balance, idempotency_key='adj-2',
                              reason_code='chargeback'), expect=202)
        assert driver_balance(world.in_driver) == balance      # waits (above the limit)

    def test_the_executor_rechecks_the_balance_when_the_checker_approves(self, world):
        maker, _ = world.finance
        Wallet.objects.filter(user_id=world.in_driver.user_id, scope='driver').update(
            balance_minor=500000)
        balance = driver_balance(world.in_driver)
        r = post(maker, self.url(world.in_driver),
                 adjust_body(direction='debit', amount_minor=balance, idempotency_key='adj-3'),
                 expect=202)
        Wallet.objects.filter(user_id=world.in_driver.user_id, scope='driver').update(balance_minor=5)
        chk, _ = checker()
        approve_request(chk, body_of(r)['approval_request']['id'])
        request = ApprovalRequest.objects.get(id=body_of(r)['approval_request']['id'])
        assert request.status == 'failed' and request.error_code == 'BALANCE_WOULD_GO_NEGATIVE'
        assert driver_balance(world.in_driver) == 5

    def test_validation(self, world):
        maker, _ = world.finance
        cases = [
            (adjust_body(currency='USD'), 'CURRENCY_MISMATCH'),
            (adjust_body(direction='sideways'), 'INVALID_DIRECTION'),
            (adjust_body(reason_code='vibes'), 'INVALID_REASON_CODE'),
            (adjust_body(note=''), 'NOTE_REQUIRED'),
            (adjust_body(amount_minor=0), 'AMOUNT_NOT_POSITIVE'),
            (adjust_body(amount_minor=10.5), 'AMOUNT_NOT_INTEGER'),
            (adjust_body(idempotency_key=''), 'IDEMPOTENCY_KEY_REQUIRED'),
        ]
        for body, code in cases:
            r = post(maker, self.url(world.in_driver), body)
            assert r.status_code == 400 and error_code(r) == code, (body, r.content)
        assert not WalletTransaction.objects.filter(purpose='adjustment').exists()

    def test_the_same_key_twice_is_one_adjustment(self, world):
        maker, _ = world.finance
        post(maker, self.url(world.in_driver), adjust_body(), expect=200)
        second = post(maker, self.url(world.in_driver), adjust_body(), expect=200)
        assert body_of(second)['duplicate'] is True
        assert WalletTransaction.objects.filter(purpose='adjustment').count() == 1

    def test_scope_capability_and_the_blocked_country(self, world, geo):
        maker, _ = world.finance
        assert post(maker, self.url(world.us_driver),
                    adjust_body(currency='USD')).status_code == 404
        analyst, _ = operator('analyst', geo)
        assert post(analyst, self.url(world.in_driver), adjust_body()).status_code == 403
        city_ops, _ = operator('city_ops_manager', geo)
        assert post(city_ops, self.url(world.in_driver), adjust_body()).status_code == 403
        us_finance, _ = operator('finance', geo, scope_code='US-TX-AUS')
        r = post(us_finance, self.url(world.us_driver),
                 adjust_body(currency='USD', amount_minor=100))
        assert r.status_code == 409 and error_code(r) == 'THRESHOLD_NOT_CONFIGURED'

    def test_the_trip_fare_adjustment_is_untouched(self):
        from servers.ride import admin_trips

        assert registry.executor_for(FINANCE_ADJUST).__module__ == 'servers.approvals.adjust'
        from servers.approvals import adjust

        assert set(adjust._RUNNERS) >= {'trip', 'driver_balance'}
        assert admin_trips.execute_adjustment is adjust._RUNNERS['trip']


# ===========================================================================
# Rider wallet credit
# ===========================================================================

def rider_with_wallet(world, currency='INR', balance=0):
    Wallet.objects.get_or_create(
        user_id=world.in_rider, scope='rider', defaults={'currency': currency,
                                                         'balance_minor': balance})
    return world.in_rider


def ticket_for(user, trip=None):
    from servers.support.models import SupportTicket

    return SupportTicket.objects.create(user_id=user, trip_id=trip, issue_type='payment',
                                        description='Charged twice', status='OPEN')


def credit_body(**over):
    body = {'amount_minor': 20000, 'currency': 'INR', 'reason_code': 'service_recovery',
            'idempotency_key': 'cr-1'}
    body.update(over)
    return body


class TestRiderWalletCredit:
    def url(self, rider):
        return f'{BASE}/riders/{rider.id}/wallet-credit/'

    def test_it_always_waits_for_a_checker_and_then_credits_the_wallet(self, world):
        rider = rider_with_wallet(world)
        maker, _ = world.finance
        r = post(maker, self.url(rider), credit_body(), expect=202)
        assert ApprovalRequest.objects.get(
            id=body_of(r)['approval_request']['id']).action_key == FINANCE_WALLET_CREDIT
        assert Wallet.objects.get(user_id=rider, scope='rider').balance_minor == 0
        chk, _ = checker()
        assert approve_request(chk, body_of(r)['approval_request']['id']).status_code == 200
        assert Wallet.objects.get(user_id=rider, scope='rider').balance_minor == 20000
        txn = WalletTransaction.objects.get(purpose='admin_credit')
        assert (txn.txn_type, txn.amount_minor, txn.currency, txn.status) == (
            'credit', 20000, 'INR', 'completed')
        assert AdminAuditLog.objects.filter(action='rider_wallet_credited').count() == 1
        approve_request(chk, body_of(r)['approval_request']['id'])
        assert WalletTransaction.objects.filter(purpose='admin_credit').count() == 1

    def test_a_linked_ticket_gets_an_internal_note_and_a_neutral_line_only_when_done(self, world):
        from servers.support.models import SupportMessage

        rider = rider_with_wallet(world)
        ticket = ticket_for(rider, world.in_trip)
        maker, _ = world.finance
        r = post(maker, self.url(rider), credit_body(ticket_id=ticket.id), expect=202)
        notes = SupportMessage.everything.filter(ticket=ticket)
        assert [m.author_role for m in notes] == ['internal']
        assert not SupportMessage.objects.filter(ticket=ticket).exists()   # rider sees nothing yet
        chk, _ = checker()
        approve_request(chk, body_of(r)['approval_request']['id'])
        visible = list(SupportMessage.objects.filter(ticket=ticket))
        assert len(visible) == 1 and visible[0].author_role == 'system'
        assert visible[0].body == 'A credit of 200.00 INR has been added to your wallet.'
        assert not re.search(r'approv|checker|request', visible[0].body, re.I)

    def test_a_ticket_of_another_rider_is_refused(self, world):
        rider = rider_with_wallet(world)
        other = make_rider('OTHER', '91')
        ticket = ticket_for(other)
        maker, _ = world.finance
        r = post(maker, self.url(rider), credit_body(ticket_id=ticket.id))
        assert r.status_code == 400 and error_code(r) == 'TICKET_RIDER_MISMATCH'

    def test_validation_and_the_wallet_cap(self, world, settings):
        rider = rider_with_wallet(world)
        maker, _ = world.finance
        for body, code in [
            (credit_body(currency='USD'), 'CURRENCY_MISMATCH'),
            (credit_body(reason_code='nope'), 'INVALID_REASON_CODE'),
            (credit_body(reason_code='other'), 'NOTE_REQUIRED'),
            (credit_body(amount_minor=-5), 'AMOUNT_NOT_POSITIVE'),
            (credit_body(amount_minor=100000000), 'WALLET_CAP_EXCEEDED'),
        ]:
            r = post(maker, self.url(rider), body)
            assert r.status_code in (400, 409) and error_code(r) == code, (body, r.content)
        assert not ApprovalRequest.objects.filter(action_key=FINANCE_WALLET_CREDIT).exists()

    def test_the_rider_rule_scopes_it_and_the_capability_gates_it(self, world, geo):
        rider_with_wallet(world)
        maker, _ = world.finance
        assert post(maker, self.url(world.us_rider), credit_body()).status_code == 404
        support, _ = operator('support_agent', geo)
        assert post(support, self.url(world.in_rider), credit_body()).status_code == 403

    def test_a_country_with_no_policy_is_blocked_and_there_is_no_debit_route(self, world, geo):
        Wallet.objects.get_or_create(user_id=world.us_rider, scope='rider',
                                     defaults={'currency': 'USD'})
        us_finance, _ = operator('finance', geo, scope_code='US-TX-AUS')
        r = post(us_finance, self.url(world.us_rider),
                 credit_body(currency='USD', amount_minor=500))
        assert r.status_code == 409 and error_code(r) == 'THRESHOLD_NOT_CONFIGURED'
        maker, _ = world.finance
        assert post(maker, f'{BASE}/riders/{world.in_rider.id}/wallet-debit/').status_code == 404

    def test_the_support_ticket_panel_can_see_the_action(self, world, geo):
        """allowed_actions on the rider page: offered to a finance.adjust holder only."""
        maker, _ = world.finance
        detail = get(maker, f'/api/v1/rider/admin/{world.in_rider.id}/')
        assert detail['actions']['wallet_credit'] is True
        support, _ = operator('support_agent', geo)
        detail = get(support, f'/api/v1/rider/admin/{world.in_rider.id}/')
        assert detail['actions']['wallet_credit'] is False


# ===========================================================================
# Scope gap from Part A
# ===========================================================================

class TestWalletRowsAreNarrowedToWhereThePermissionIsHeld:
    def test_finance_in_one_city_plus_support_in_another(self, world, geo):
        # an Austin rider with a wallet row
        Wallet.objects.get_or_create(user_id=world.us_rider, scope='rider',
                                     defaults={'currency': 'USD'})
        us_row = WalletTransaction.objects.create(
            user_id=world.us_rider, amount_minor=500, currency='USD', txn_type='credit',
            status='completed', purpose='refund', idempotency_key='us-rider-row')
        in_row = WalletTransaction.objects.create(
            user_id=world.in_rider, amount_minor=500, currency='INR', txn_type='credit',
            status='completed', purpose='refund', idempotency_key='in-rider-row')
        op = User.objects.create_user(
            phone_number='+919810099001', username='two-hats', email='two.hats@saaradhigo.test',
            full_name='Two Hats', role='admin', is_staff=True)
        AdminAssignment.objects.create(admin=op, role=AdminRole.objects.get(code='finance'),
                                       scope=geo['IN-TG-HYD'])
        AdminAssignment.objects.create(admin=op, role=AdminRole.objects.get(code='support_agent'),
                                       scope=geo['US-TX-AUS'])
        narrowed = money_visible_to(op, WalletTransaction, permission='finance.read')
        assert in_row in narrowed and us_row not in narrowed
        # without the narrowing the second city's row shows (that is the gap)
        everything = money_visible_to(op, WalletTransaction)
        assert in_row in everything and us_row in everything
        Wallet.objects.get_or_create(user_id=world.in_rider, scope='rider',
                                     defaults={'currency': 'INR'})
        wallets = money_visible_to(op, Wallet, permission='finance.read')
        assert not wallets.filter(user_id=world.us_rider).exists()
        assert wallets.filter(user_id=world.in_rider).exists()

    def test_the_narrowing_does_not_change_an_unrestricted_caller(self, world, geo):
        _, super_user = operator('super_admin', geo)
        rows = money_visible_to(super_user, WalletTransaction, permission='finance.read')
        assert rows.count() == WalletTransaction.objects.count()


# ===========================================================================
# The weekly run
# ===========================================================================

# 2026-11-02 is a Monday. America/New_York is on standard time (UTC-5).
SUNDAY_NIGHT_NY = datetime(2026, 11, 2, 4, 0, tzinfo=dt_timezone.utc)      # Sun 23:00 New York
MONDAY_MORNING_NY = datetime(2026, 11, 2, 6, 0, tzinfo=dt_timezone.utc)    # Mon 01:00 New York
MONDAY_LATER_NY = datetime(2026, 11, 2, 20, 0, tzinfo=dt_timezone.utc)     # Mon 15:00 New York
NEXT_MONDAY_NY = datetime(2026, 11, 9, 12, 0, tzinfo=dt_timezone.utc)


@pytest.fixture
def us_weekly(world):
    apply_ruling()
    WithdrawalRequest.objects.filter(driver=world.us_driver).delete()
    return world


class TestWeeklyRun:
    def run(self, now):
        from servers.payments.weekly_payout import run_weekly_payouts

        return run_weekly_payouts(now)

    def test_it_runs_only_on_the_weekday_in_the_countrys_own_zone(self, us_weekly):
        assert self.run(SUNDAY_NIGHT_NY)[0]['why'] == 'not_the_day'
        assert not WithdrawalRequest.objects.filter(origin='scheduled').exists()
        # the same instant is already Monday in Hyderabad, but India is on request
        result = self.run(MONDAY_MORNING_NY)
        assert [r['country'] for r in result] == ['US'] and result[0]['ran'] is True
        assert WithdrawalRequest.objects.filter(origin='scheduled').count() == 1

    def test_it_creates_the_withdrawal_with_the_system_as_actor(self, us_weekly):
        self.run(MONDAY_MORNING_NY)
        w = WithdrawalRequest.objects.get(origin='scheduled')
        assert w.driver_id == us_weekly.us_driver.id and w.currency == 'USD'
        assert w.status == 'pending' and w.fee_minor == 0 and w.admin_notes == 'scheduled weekly payout'
        assert w.amount_minor > 0 and driver_balance(us_weekly.us_driver) == 0
        audit = AdminAuditLog.objects.get(action='withdrawal_scheduled_created')
        assert audit.reason == 'scheduled weekly payout'
        assert 'system:weekly_payout' in audit.actor_label

    def test_it_respects_the_minimum_and_skips_drivers_with_an_open_withdrawal(self, us_weekly):
        Wallet.objects.filter(user_id=us_weekly.us_driver.user_id, scope='driver').update(
            balance_minor=99)         # one cent under the $1.00 minimum
        poor = self.run(MONDAY_MORNING_NY)[0]
        assert poor['withdrawals'] == 0 and not WithdrawalRequest.objects.filter(
            origin='scheduled').exists()
        PayoutRun.objects.all().delete()
        Wallet.objects.filter(user_id=us_weekly.us_driver.user_id, scope='driver').update(
            balance_minor=100)        # exactly the minimum
        WithdrawalRequest.objects.create(driver=us_weekly.us_driver, amount_minor=100,
                                         currency='USD', status='approved')
        assert self.run(MONDAY_MORNING_NY)[0]['withdrawals'] == 0      # one is already open
        WithdrawalRequest.objects.all().delete()
        PayoutRun.objects.all().delete()
        assert self.run(MONDAY_MORNING_NY)[0]['withdrawals'] == 1

    def test_it_is_idempotent_per_country_per_week(self, us_weekly):
        add_policy('US', FINANCE_PAYOUT)
        self.run(MONDAY_MORNING_NY)
        snapshot = (WithdrawalRequest.objects.count(), ApprovalRequest.objects.count(),
                    AdminAuditLog.objects.count(), PayoutRun.objects.count())
        again = self.run(MONDAY_LATER_NY)[0]
        assert again['ran'] is False and again['why'] == 'already_ran'
        assert (WithdrawalRequest.objects.count(), ApprovalRequest.objects.count(),
                AdminAuditLog.objects.count(), PayoutRun.objects.count()) == snapshot
        assert PayoutRun.objects.count() == 1
        # next week is a new run (the driver has nothing left, so it pays nobody)
        assert self.run(NEXT_MONDAY_NY)[0]['ran'] is True and PayoutRun.objects.count() == 2

    def test_it_opens_one_approval_request_for_the_whole_run_and_dispatches_nothing(self, us_weekly):
        add_policy('US', FINANCE_PAYOUT)
        second = make_driver(us_weekly.geo, 'US-TX-AUS', 'US2', currency='USD', calling='1')
        Wallet.objects.filter(user_id=second.user_id, scope='driver').update(balance_minor=2500)
        with mock.patch('servers.driver.services.trigger_payout_creation') as dispatched, \
                mock.patch(UPI) as gateway:
            result = self.run(MONDAY_MORNING_NY)[0]
        assert result['withdrawals'] == 2 and result['outcome'] == 'opened'
        runs = ApprovalRequest.objects.filter(action_key=FINANCE_PAYOUT)
        assert runs.count() == 1
        request = runs.get()
        assert request.status == REQUESTED and request.country == 'US' and request.currency == 'USD'
        assert request.maker_label == 'system:weekly_payout' and request.maker_role == 'system'
        ids = sorted(WithdrawalRequest.objects.filter(origin='scheduled').values_list('id', flat=True))
        assert request.payload['withdrawal_ids'] == ids
        assert not dispatched.called and not gateway.called
        assert set(WithdrawalRequest.objects.filter(origin='scheduled')
                   .values_list('status', flat=True)) == {'pending'}
        assert PayoutRun.objects.get().approval_request_id == request.id

    def test_a_checker_releases_the_run(self, us_weekly):
        add_policy('US', FINANCE_PAYOUT)
        self.run(MONDAY_MORNING_NY)
        request = ApprovalRequest.objects.get(action_key=FINANCE_PAYOUT)
        chk, _ = checker()
        with mock.patch('servers.driver.services.trigger_payout_creation',
                        return_value=True) as dispatched:
            assert approve_request(chk, request.id).status_code == 200
        assert dispatched.call_count == 1
        request.refresh_from_db()
        assert request.status == EXECUTED

    def test_with_no_policy_the_run_is_blocked_and_says_so(self, us_weekly, geo):
        result = self.run(MONDAY_MORNING_NY)[0]
        assert result['outcome'] == 'blocked_no_policy' and result['approval_request_id'] is None
        assert not ApprovalRequest.objects.filter(action_key=FINANCE_PAYOUT).exists()
        assert WithdrawalRequest.objects.filter(origin='scheduled', status='pending').count() == 1
        us_finance, _ = operator('finance', geo, scope_code='US-TX-AUS')
        config = get(us_finance, f'{BASE}/payout-config/')
        us = next(c for c in config['countries'] if c['country'] == 'US')
        assert us['payout_approval_blocked'] is True
        assert 'no approval policy' in us['payout_approval_blocked_reason']
        assert us['recent_runs'][0]['outcome'] == 'blocked_no_policy'

    def test_with_no_country_config_nothing_happens(self, us_weekly):
        PayoutCountryConfig.objects.filter(country='US').delete()
        assert self.run(MONDAY_MORNING_NY) == []
        assert not WithdrawalRequest.objects.filter(origin='scheduled').exists()
        assert not PayoutRun.objects.exists()

    def test_an_on_request_country_is_never_scheduled(self, us_weekly):
        PayoutCountryConfig.objects.filter(country='US').update(payout_schedule='on_request',
                                                                 payout_weekday=None)
        assert self.run(MONDAY_MORNING_NY) == []

    def test_the_beat_task_is_scheduled_hourly(self):
        from django.conf import settings

        entry = settings.CELERY_BEAT_SCHEDULE['payments-weekly-payout-run-hourly']
        assert entry['task'] == 'payments.weekly_payout_run'
        from servers.payments import tasks

        assert tasks.weekly_payout_run.name == 'payments.weekly_payout_run'


# ===========================================================================
# Payout configuration edits
# ===========================================================================

class TestPayoutConfigEdit:
    def url(self, country='IN'):
        return f'{BASE}/payout-config/{country}/'

    def test_an_edit_is_a_request_and_the_checker_writes_the_new_version(self, world):
        maker, _ = world.finance
        r = post(maker, self.url(), {'fee_percent': '1.50', 'minimum_withdrawal_minor': 60000,
                                     'stuck_payout_hours': 36, 'reason': 'Q4 review',
                                     'idempotency_key': 'cfg-1'}, expect=202)
        req = body_of(r)['approval_request']
        assert req['action_key'] == FINANCE_PAYOUT_CONFIG
        assert PayoutCountryConfig.objects.get(country='IN', is_active=True).fee_percent == 0
        chk, _ = checker()
        assert approve_request(chk, req['id']).status_code == 200
        active = PayoutCountryConfig.objects.get(country='IN', is_active=True)
        assert (active.fee_percent, active.minimum_withdrawal_minor, active.stuck_payout_hours,
                active.version) == (Decimal('1.50'), 60000, 36, 2)
        assert PayoutCountryConfig.objects.filter(country='IN').count() == 2
        assert AdminAuditLog.objects.filter(action='payout_config_changed').count() == 1
        # the driver app now shows exactly what is charged
        body = client_for(world.in_driver.user_id).get(
            '/api/v1/driver/withdrawals/balance/').json()['data']
        assert body['fee_percent'] == 1.5 and body['fee_display'] == '1.5%'
        assert body['minimum_withdrawal'] == 600.0

    def test_a_stale_request_is_refused_not_applied(self, world):
        maker, _ = world.finance
        first = post(maker, self.url(), {'fee_percent': '1.00', 'reason': 'a',
                                         'idempotency_key': 'cfg-a'}, expect=202)
        second = post(maker, self.url(), {'fee_percent': '3.00', 'reason': 'b',
                                          'idempotency_key': 'cfg-b'}, expect=202)
        chk, _ = checker()
        approve_request(chk, body_of(first)['approval_request']['id'])
        approve_request(chk, body_of(second)['approval_request']['id'])
        stale = ApprovalRequest.objects.get(id=body_of(second)['approval_request']['id'])
        assert stale.status == 'failed' and stale.error_code == 'CONFIG_CHANGED'
        assert PayoutCountryConfig.objects.get(country='IN', is_active=True).fee_percent == Decimal('1.00')

    def test_validation(self, world):
        maker, _ = world.finance
        for body, code in [
            ({'fee_percent': '101'}, 'INVALID_FEE'),
            ({'fee_percent': 'abc'}, 'INVALID_FEE'),
            ({'fee_percent': '1.234'}, 'INVALID_FEE'),
            ({'minimum_withdrawal_minor': -1}, 'INVALID_MINIMUM'),
            ({'stuck_payout_hours': 0}, 'INVALID_STUCK_HOURS'),
            ({'payout_schedule': 'daily'}, 'INVALID_SCHEDULE'),
            ({'payout_schedule': 'weekly'}, 'WEEKDAY_REQUIRED'),
            ({'payout_schedule': 'weekly', 'payout_weekday': 9}, 'INVALID_WEEKDAY'),
            ({'fee_percent': '0.00'}, 'NO_CHANGE'),
        ]:
            r = post(maker, self.url(), {**body, 'reason': 'r', 'idempotency_key': 'v'})
            assert r.status_code == 400 and error_code(r) == code, (body, r.content)
        missing_reason = post(maker, self.url(), {'fee_percent': '1', 'idempotency_key': 'v'})
        assert error_code(missing_reason) == 'REASON_REQUIRED'

    def test_scope_capability_and_the_blocked_country(self, world, geo):
        maker, _ = world.finance
        apply_ruling()
        assert post(maker, self.url('US'), {'fee_percent': '1', 'reason': 'r',
                                            'idempotency_key': 'u'}).status_code == 404
        city_ops, _ = operator('city_ops_manager', geo)
        assert post(city_ops, self.url(), {'fee_percent': '1'}).status_code == 403
        us_finance, _ = operator('finance', geo, scope_code='US-TX-AUS')
        r = post(us_finance, self.url('US'), {'fee_percent': '1', 'reason': 'r',
                                              'idempotency_key': 'u'})
        assert r.status_code == 409 and error_code(r) == 'THRESHOLD_NOT_CONFIGURED'
        config = get(us_finance, f'{BASE}/payout-config/')
        us = next(c for c in config['countries'] if c['country'] == 'US')
        assert us['editable'] is True and us['change_blocked'] is True
        assert us['change_blocked_reason'] == 'Blocked: no approval policy in this country.'

    def test_the_read_carries_history_pending_changes_and_the_fee_warning(self, world):
        maker, _ = world.finance
        post(maker, self.url(), {'fee_percent': '2', 'reason': 'r', 'idempotency_key': 'h'},
             expect=202)
        config = get(maker, f'{BASE}/payout-config/')
        india = next(c for c in config['countries'] if c['country'] == 'IN')
        assert india['editable'] is True and len(india['history']) == 1
        assert len(india['pending_changes']) == 1 and india['change_blocked'] is False
        assert 'exactly as it is charged' in config['fee_warning']

    def test_an_unconfigured_country_cannot_be_edited_into_existence(self, world, geo):
        _, super_user = operator('super_admin', geo)
        client = client_for(super_user)
        r = post(client, self.url('US'), {'fee_percent': '1', 'reason': 'r', 'idempotency_key': 'n'})
        assert r.status_code == 409 and error_code(r) == 'NOT_CONFIGURED'


# ===========================================================================
# Capabilities, policies, action keys
# ===========================================================================

class TestCapabilitiesAndKeys:
    def holders(self, permission):
        from servers.admin_access.roles import ROLE_TEMPLATES

        return {t['code'] for t in ROLE_TEMPLATES if permission in t['permissions']}

    def test_finance_config_is_finance_and_super_admin_only(self):
        assert self.holders('finance.config') == {'finance', 'super_admin'}

    def test_finance_aggregate_is_city_ops_analyst_finance_and_super_admin(self):
        assert self.holders('finance.aggregate') == {
            'city_ops_manager', 'analyst', 'finance', 'super_admin'}

    def test_neither_new_capability_grants_a_payments_admin_endpoint(self):
        from servers.admin_access.roles import ROLE_TEMPLATES

        for template in ROLE_TEMPLATES:
            if template['code'] in ('city_ops_manager', 'analyst'):
                assert 'finance.read' not in template['permissions']

    def test_the_new_action_keys_are_registered_with_policies_for_india_only(self, world):
        for key in (FINANCE_PAYOUT_CONFIG, FINANCE_WALLET_CREDIT):
            assert key in ACTIONS and registry.has_executor(key)
            policy = ApprovalPolicy.objects.get(country='IN', action_key=key, is_active=True)
            assert policy.limit_kind == 'always' and policy.maker_limit is None
            assert not ApprovalPolicy.objects.filter(country='US', action_key=key).exists()
        assert not ApprovalPolicy.objects.filter(country='US').exists()
        assert ACTIONS[FINANCE_PAYOUT_CONFIG].capability == 'finance.config'
        assert ACTIONS[FINANCE_WALLET_CREDIT].capability == 'finance.adjust'


# ===========================================================================
# Aggregate finance metrics on the Command Centre
# ===========================================================================

class TestAggregateMetrics:
    def overview(self, client):
        return get(client, OVERVIEW + '?window=7d')

    def test_city_ops_sees_hyderabad_tiles_and_not_austin(self, world, geo):
        city_ops, _ = operator('city_ops_manager', geo)
        finance = self.overview(city_ops)['finance']
        assert finance['visible'] is True
        assert [t['code'] for t in finance['tiles']] == ['IN-TG-HYD']
        tile = finance['tiles'][0]
        gross = tile['gmv']['INR']
        assert gross == 210000          # 2000.00 online + 100.00 cash, in paise
        assert tile['take_rate_percent']['INR'] == round(
            tile['commission']['INR'] / gross * 100, 1)
        assert tile['payouts_pending']['count'] == 1
        assert tile['payouts_pending']['amount'] == {'INR': 50000}
        assert 'open_findings' in tile['reconciliation']
        # City Ops holds no finance.read: no link, and the old money block stays stripped
        assert finance['reconciliation_link'] is False
        assert 'money' not in self.overview(city_ops)['cities'][0]

    def test_no_tile_carries_a_person_or_a_cross_currency_sum(self, world, geo):
        _, super_user = operator('super_admin', geo)
        finance = self.overview(client_for(super_user))['finance']
        assert {t['code'] for t in finance['tiles']} >= {'IN-TG-HYD', 'US-TX-AUS'}
        for tile in finance['tiles']:
            assert set(tile) == {'code', 'name', 'country', 'currency', 'window', 'timezone',
                                 'gmv', 'commission', 'take_rate_percent', 'payouts_pending',
                                 'reconciliation'}
            assert set(tile['gmv']) == {tile['currency']}
        text = str(finance)
        assert 'driver' not in text.lower().replace('payouts', '')

    def test_finance_holders_get_the_reconciliation_link(self, world):
        maker, _ = world.finance
        assert self.overview(maker)['finance']['reconciliation_link'] is True

    def test_the_analyst_is_refused_every_payments_admin_endpoint(self, world, geo):
        analyst, _ = operator('analyst', geo)
        assert self.overview(analyst)['finance']['visible'] is True
        for path in ('/ledger/', ):
            assert analyst.get(f'{BASE}/drivers/{world.in_driver.id}{path}').status_code == 403
        for path in ('payouts/', 'payments/', 'settlements/', 'overview/', 'payout-config/',
                     'reconciliation/', 'transactions/'):
            assert analyst.get(f'{BASE}/{path}').status_code == 403, path

    def test_city_ops_is_refused_every_payments_admin_endpoint(self, world, geo):
        city_ops, _ = operator('city_ops_manager', geo)
        for path in ('payouts/', 'payments/', 'settlements/', 'payout-config/'):
            assert city_ops.get(f'{BASE}/{path}').status_code == 403, path
        assert city_ops.get(f'{BASE}/drivers/{world.in_driver.id}/ledger/').status_code == 403

    def test_marketing_sees_no_finance_tiles(self, world, geo):
        marketing, _ = operator('marketing', geo)
        payload = self.overview(marketing)
        assert payload['finance'] == {'visible': False, 'tiles': [], 'reconciliation_link': False}

    def test_the_service_function_is_the_one_reports_will_reuse(self, world, geo):
        from servers.payments.aggregates import cities_with_aggregate, finance_aggregates

        _, user = operator('analyst', geo)
        cities = cities_with_aggregate(user, list(GeoNode.objects.filter(kind='city')))
        assert [c.code for c in cities] == ['IN-TG-HYD']
        tiles = finance_aggregates(cities, '7d')
        assert list(tiles) == ['IN-TG-HYD']

    def test_two_hats_see_only_the_city_where_aggregate_is_held(self, world, geo):
        op = User.objects.create_user(
            phone_number='+919810099002', username='hats2', email='hats.two@saaradhigo.test',
            full_name='Hats Two', role='admin', is_staff=True)
        AdminAssignment.objects.create(admin=op, role=AdminRole.objects.get(code='finance'),
                                       scope=geo['IN-TG-HYD'])
        AdminAssignment.objects.create(admin=op, role=AdminRole.objects.get(code='marketing'),
                                       scope=geo['US-TX-AUS'])
        tiles = self.overview(client_for(op))['finance']['tiles']
        assert [t['code'] for t in tiles] == ['IN-TG-HYD']


# ===========================================================================
# Marketing on the Command Centre
# ===========================================================================

class TestMarketingCommandCentre:
    def test_the_endpoint_gives_marketing_aggregates_and_nothing_sensitive(self, world, geo):
        marketing, _ = operator('marketing', geo)
        payload = get(marketing, OVERVIEW)
        assert payload['sos_visible'] is False and payload['money_visible'] is False
        for city in payload['cities']:
            assert 'money' not in city
            assert not [k for k in city['queues'] if k.startswith('sos_')]
            assert not [k for k in city['queues'] if k.startswith('withdrawals_')]
            assert set(city) >= {'trips', 'rates', 'drivers', 'riders'}
        text = str(payload)
        assert 'phone' not in text and '@' not in text

    def test_no_live_sos_alert_reaches_marketing(self, world, geo):
        marketing, _ = operator('marketing', geo)
        alerts = get(marketing, '/api/v1/command-centre/admin/alerts/')
        kinds = {a['kind'] for a in alerts['results']}
        assert not {k for k in kinds if 'sos' in k.lower()}

    def test_me_agrees_with_the_endpoint_marketing_holds_the_capability_the_nav_keys_on(
            self, world, geo):
        marketing, _ = operator('marketing', geo)
        # self-describing: the caller's own account (their own email is theirs to see)
        me = marketing.get('/api/v1/admin-access/me/').json()['data']
        assert 'report.read' in me['permissions']
        assert 'finance.read' not in me['permissions'] and 'finance.aggregate' not in me['permissions']
        assert marketing.get(OVERVIEW).status_code == 200


# ===========================================================================
# United States approval policies (owner, AUDIT 27.44)
# ===========================================================================

US_ROWS = {
    'support.refund': ('money', 'USD', 1_500), 'finance.refund': ('money', 'USD', 15_000),
    'finance.adjust': ('always', '', None), 'finance.wallet_credit': ('always', '', None),
    'finance.payout': ('money', 'USD', 100_000), 'bulk.action': ('always', '', None),
    'pricing.change': ('always', '', None), 'driver.deactivate': ('always', '', None),
    'support.sla_change': ('always', '', None), 'finance.payout_config_change': ('always', '', None),
    'zone.change': ('always', '', None),
}


@pytest.fixture
def us_world(world):
    call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=StringIO())
    apply_ruling()
    WithdrawalRequest.objects.filter(driver=world.us_driver).delete()
    return world


class TestUsPolicies:
    def test_the_seed_writes_the_owners_us_set_and_leaves_india_alone(self, world):
        in_before = sorted(ApprovalPolicy.objects.filter(country='IN').values_list(
            'action_key', 'limit_kind', 'currency', 'maker_limit', 'version'))
        call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=StringIO())
        us = {p.action_key: (p.limit_kind, p.currency, p.maker_limit)
              for p in ApprovalPolicy.objects.filter(country='US', is_active=True)}
        assert us == US_ROWS
        assert in_before == sorted(ApprovalPolicy.objects.filter(country='IN').values_list(
            'action_key', 'limit_kind', 'currency', 'maker_limit', 'version'))

    def test_the_seed_never_modifies_an_existing_us_row(self, world):
        add_policy('US', 'finance.payout', 'money', 'USD', 5_000)
        call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=StringIO())
        p = ApprovalPolicy.objects.get(country='US', action_key='finance.payout', is_active=True)
        assert p.maker_limit == 5_000 and p.version == 1

    def test_a_us_fifty_dollar_withdrawal_is_approved_by_the_maker_alone(self, us_world, geo):
        w = WithdrawalRequest.objects.create(
            driver=us_world.us_driver, amount_minor=5_000, currency='USD', status='pending')
        us_finance, _ = operator('finance', geo, scope_code='US-TX-AUS')
        with mock.patch('servers.driver.services.trigger_payout_creation',
                        return_value=True) as dispatched:
            r = post(us_finance, f'{BASE}/payouts/{w.id}/approve/', expect=200)
        assert dispatched.call_count == 1
        w.refresh_from_db()
        assert w.status == 'approved'
        assert ApprovalRequest.objects.get(action_key=FINANCE_PAYOUT).status == EXECUTED
        assert body_of(r)['status'] == 'approved'

    def test_above_one_thousand_dollars_a_single_withdrawal_needs_a_checker(self, us_world, geo):
        w = WithdrawalRequest.objects.create(
            driver=us_world.us_driver, amount_minor=100_001, currency='USD', status='pending')
        us_finance, _ = operator('finance', geo, scope_code='US-TX-AUS')
        post(us_finance, f'{BASE}/payouts/{w.id}/approve/', expect=202)
        w.refresh_from_db()
        assert w.status == 'pending'

    def test_a_us_weekly_run_creates_a_pending_request_needing_a_checker(self, us_world):
        from servers.payments.weekly_payout import run_weekly_payouts

        with mock.patch('servers.driver.services.trigger_payout_creation') as dispatched:
            result = run_weekly_payouts(MONDAY_MORNING_NY)[0]
        assert result['outcome'] == 'opened'
        request = ApprovalRequest.objects.get(action_key=FINANCE_PAYOUT)
        assert request.status == REQUESTED and request.country == 'US'
        assert not dispatched.called

    def test_a_run_always_needs_a_checker_in_india_and_the_us_whatever_the_limit(self, us_world, geo):
        """The framework rule (Action.multi_target_key): a request naming many
        withdrawals is never within ONE withdrawal's limit, even where the
        policy lets the maker do a single small one alone."""
        add_policy('IN', FINANCE_PAYOUT, 'money', 'INR', 5_000_000)
        add_policy('US', FINANCE_PAYOUT, 'money', 'USD', 5_000_000)
        for currency, driver, place in (('INR', us_world.in_driver, 'IN-TG-HYD'),
                                        ('USD', us_world.us_driver, 'US-TX-AUS')):
            single = WithdrawalRequest.objects.create(
                driver=driver, amount_minor=100, currency=currency, status='pending')
            other = WithdrawalRequest.objects.create(
                driver=driver, amount_minor=100, currency=currency, status='pending')
            maker, _ = operator('finance', geo, scope_code=place)
            with mock.patch('servers.driver.services.trigger_payout_creation', return_value=True):
                one = post(maker, f'{BASE}/payouts/{single.id}/approve/', expect=200)
                # not swept: the batch's hash key can contain a long digit run by chance
                run = maker.post(f'{BASE}/payouts/bulk-action/',
                                 {'action': 'approve', 'withdrawal_ids': [other.id]}, format='json')
                assert run.status_code == 202
            assert body_of(one)['status'] == 'approved', currency
            assert run.json()['data']['approval_request']['status'] == REQUESTED, currency

    def test_the_other_us_actions_follow_the_rows(self, us_world):
        from servers.approvals.service import decide

        assert decide('support.refund', 'US', 1_500, 'USD')[0] is False
        assert decide('support.refund', 'US', 1_501, 'USD')[0] is True
        assert decide('finance.refund', 'US', 15_000, 'USD')[0] is False
        assert decide('finance.refund', 'US', 15_001, 'USD')[0] is True
        for key in ('finance.adjust', 'finance.wallet_credit', 'bulk.action', 'pricing.change',
                    'driver.deactivate', 'support.sla_change', 'finance.payout_config_change'):
            assert decide(key, 'US', 1, 'USD')[0] is True, key

    def test_us_adjustments_go_to_a_checker_instead_of_being_blocked(self, us_world, geo):
        us_finance, _ = operator('finance', geo, scope_code='US-TX-AUS')
        r = post(us_finance, f'{BASE}/drivers/{us_world.us_driver.id}/adjust/',
                 adjust_body(currency='USD', amount_minor=100), expect=202)
        assert body_of(r)['state'] == 'pending_approval'
