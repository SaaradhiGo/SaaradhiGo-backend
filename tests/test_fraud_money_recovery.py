"""Ruling (b), AUDIT 27.56: a confirmed promo abuse / collusion flag may claw back linked rewards
through a Finance adjustment request (always a checker), never more than the reward; one payout is
reduced by at most `recovery_max_percent` (50) and the rest carries over; a rider's recovery is
taken from the wallet only and never makes it negative.
"""

from datetime import datetime, timedelta, timezone as dt_tz

import pytest
from django.utils import timezone

from fraud_money_support import (  # noqa: F401
    ADM, APPROVALS, approve, block_the_network, body_of, campaign, client_for, credit, decide,
    error_code, farming_flag, flag_url, geo, get, hyd_driver, make_flag, make_rider, operator,
    paid_out, post, referral_reward, reward, seed_money, us_driver, world,
)
from servers.admin_audit.models import AdminAuditLog
from servers.driver.models import WithdrawalRequest
from servers.fraud import money
from servers.fraud.models import RecoveryDebt, RecoveryDeduction, RewardHold
from servers.payments import ledger
from servers.payments.models import PayoutCountryConfig
from servers.payments.weekly_payout import run_country
from servers.rider.models import Wallet, WalletTransaction, get_wallet

pytestmark = pytest.mark.django_db

RECOVER = 'recover/'


@pytest.fixture
def money_world(world):
    seed_money()
    return world


def recover(world, flag, amount, *, expect=202, key='r1', client=None):
    return post(client or world.super[0], flag_url(flag, RECOVER),
                {'amount_minor': amount, 'note': 'Recovering the abused reward',
                 'idempotency_key': key}, expect=expect)


def confirmed_paid_driver_flag(w, tag, minor=30_000, camp=None):
    """A driver whose linked reward was already PAID OUT, on a flag confirmed as promo abuse."""
    drv = hyd_driver(w, tag)
    camp = camp or campaign(w)
    at = timezone.now() - timedelta(days=5)
    reward(drv.user_id, camp, minor, at=at)
    paid_out(drv, minor, at=at + timedelta(days=1))
    credit(drv.user_id, 5_000)
    flag = farming_flag(w, drv.user_id, camp)
    assert decide(w.ops[0], flag, 'confirm', 'promo_abuse').status_code == 200
    flag.refresh_from_db()
    return drv, camp, flag


class TestTheRequest:
    def test_it_is_a_finance_adjustment_request_and_always_needs_a_checker(self, money_world):
        w = money_world
        drv, camp, flag = confirmed_paid_driver_flag(w, 'R1')
        # 100.00 rupees is far below the India maker limit for adjustments; still a checker.
        data = body_of(recover(w, flag, 10_000))
        request = data['approval_request']
        assert request['action_key'] == 'finance.adjust'
        assert request['status'] == 'requested' and data['state'] == 'pending_approval'
        assert request['payload']['reason_code'] == 'fraud_recovery'
        assert request['payload']['flag_id'] == flag.pk
        assert not RecoveryDebt.objects.exists()                     # nothing moved yet

    def test_the_maker_cannot_approve_their_own_request(self, money_world):
        w = money_world
        drv, camp, flag = confirmed_paid_driver_flag(w, 'R2')
        request_id = body_of(recover(w, flag, 10_000))['approval_request']['id']
        r = post(w.super[0], f'{APPROVALS}/{request_id}/approve/', {'reason': 'ok'})
        assert r.status_code == 403
        assert not RecoveryDebt.objects.exists()

    def test_more_than_the_reward_is_refused(self, money_world):
        w = money_world
        drv, camp, flag = confirmed_paid_driver_flag(w, 'R3', minor=30_000)
        r = recover(w, flag, 30_001, expect=400)
        assert error_code(r) == 'AMOUNT_EXCEEDS_REWARD'
        recover(w, flag, 30_000)                                      # exactly the reward is fine

    def test_a_second_request_cannot_pass_the_reward_together_with_the_first(self, money_world):
        w = money_world
        drv, camp, flag = confirmed_paid_driver_flag(w, 'R4', minor=30_000)
        recover(w, flag, 20_000, key='a')
        assert error_code(recover(w, flag, 10_001, key='b', expect=400)) == 'AMOUNT_EXCEEDS_REWARD'
        recover(w, flag, 10_000, key='c')

    def test_bad_amounts_are_refused(self, money_world):
        w = money_world
        drv, camp, flag = confirmed_paid_driver_flag(w, 'R5')
        for bad in (0, -5, '100', 1.5, None, True):
            assert error_code(recover(w, flag, bad, expect=400)) == 'INVALID_AMOUNT', bad

    def test_only_a_confirmed_promo_abuse_or_collusion_flag_can_recover(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'R6')
        camp = campaign(w)
        reward(drv.user_id, camp, 30_000)
        open_flag = farming_flag(w, drv.user_id, camp)
        assert error_code(recover(w, open_flag, 1_000, expect=409)) == 'RECOVERY_NOT_APPLICABLE'
        # Confirmed, but for a reason that is not about rewards.
        decide(w.ops[0], open_flag, 'confirm', 'gps_spoofing')
        assert error_code(recover(w, open_flag, 1_000, expect=409)) == 'RECOVERY_NOT_APPLICABLE'
        # Dismissed.
        other = hyd_driver(w, 'R6b')
        camp2 = campaign(w, name='Two')
        reward(other.user_id, camp2, 30_000)
        flag2 = farming_flag(w, other.user_id, camp2)
        decide(w.ops[0], flag2, 'dismiss', 'false_positive')
        assert error_code(recover(w, flag2, 1_000, expect=409)) == 'RECOVERY_NOT_APPLICABLE'

    def test_it_needs_both_fraud_review_and_finance_adjust(self, money_world):
        w = money_world
        drv, camp, flag = confirmed_paid_driver_flag(w, 'R7')
        for who in (w.ops, w.finance, w.mkt, w.analyst):                # none holds both
            r = post(who[0], flag_url(flag, RECOVER),
                     {'amount_minor': 1_000, 'note': 'Recovering it', 'idempotency_key': 'z'})
            assert r.status_code == 403, who[1].full_name
        assert post(w.ops_us[0], flag_url(flag, RECOVER),
                    {'amount_minor': 1_000, 'note': 'x' * 6, 'idempotency_key': 'z'}
                    ).status_code == 404

    def test_the_audit_names_the_request_and_the_flag(self, money_world):
        w = money_world
        drv, camp, flag = confirmed_paid_driver_flag(w, 'R8')
        request_id = body_of(recover(w, flag, 10_000))['approval_request']['id']
        assert approve(request_id).status_code == 200
        row = AdminAuditLog.objects.get(action='fraud_recovery_executed')
        assert row.target_id == str(flag.pk)
        assert row.after['approval_request_id'] == request_id and row.after['debt_minor'] == 10_000


class TestWhenItRuns:
    def test_a_paid_reward_becomes_a_debt_not_an_immediate_debit(self, money_world):
        w = money_world
        drv, camp, flag = confirmed_paid_driver_flag(w, 'E1')
        balance = get_wallet(drv.user_id, Wallet.SCOPE_DRIVER).balance_minor
        request_id = body_of(recover(w, flag, 30_000))['approval_request']['id']
        assert approve(request_id).status_code == 200
        debt = RecoveryDebt.objects.get()
        assert (debt.amount_minor, debt.recovered_minor, debt.forfeited_minor) == (30_000, 0, 0)
        assert debt.remaining_minor == 30_000 and debt.role == 'driver'
        assert get_wallet(drv.user_id, Wallet.SCOPE_DRIVER).balance_minor == balance
        assert not WalletTransaction.objects.filter(purpose='reward_recovery').exists()

    def test_a_held_reward_is_forfeited_first_and_needs_no_debt(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'E2')
        camp = campaign(w)
        reward(drv.user_id, camp, 30_000)
        credit(drv.user_id, 20_000)
        flag = farming_flag(w, drv.user_id, camp)
        money.hold_for_flag(flag)
        decide(w.ops[0], flag, 'confirm', 'promo_abuse')
        before = get_wallet(drv.user_id, Wallet.SCOPE_DRIVER).balance_minor
        request_id = body_of(recover(w, flag, 30_000))['approval_request']['id']
        assert approve(request_id).status_code == 200
        hold = RewardHold.objects.get()
        debt = RecoveryDebt.objects.get()
        assert hold.status == 'forfeited' and hold.forfeited_via_id == request_id
        assert (debt.forfeited_minor, debt.amount_minor) == (30_000, 0) and debt.closed_at
        assert get_wallet(drv.user_id, Wallet.SCOPE_DRIVER).balance_minor == before - 30_000
        line = WalletTransaction.objects.get(purpose='reward_recovery')
        assert (line.txn_type, line.amount_minor, line.status) == ('debit', 30_000, 'completed')
        assert money.held_minor(drv.user_id) == 0
        # Trip earnings are untouched.
        assert WalletTransaction.objects.filter(
            user_id=drv.user_id, purpose='trip_earnings', amount_minor=20_000).exists()

    def test_executing_twice_changes_nothing(self, money_world):
        w = money_world
        drv, camp, flag = confirmed_paid_driver_flag(w, 'E3')
        request_id = body_of(recover(w, flag, 30_000))['approval_request']['id']
        approve(request_id)
        from servers.approvals.models import ApprovalRequest

        row = ApprovalRequest.objects.get(pk=request_id)
        out = money.execute_recovery(row)
        assert out.get('duplicate') is True and RecoveryDebt.objects.count() == 1

    def test_the_statement_names_the_line_without_a_fraud_word(self, money_world):
        w = money_world
        assert ledger.ENTRY_LABELS['reward_recovery'] == 'Recovery of reward (reviewed)'
        drv, camp, flag = confirmed_paid_driver_flag(w, 'E4')
        assert money.RECOVERY_PURPOSE == 'reward_recovery' and 'fraud' not in money.RECOVERY_PURPOSE


class TestCapAndCarryOver:
    """The US weekly run: 50 percent of one payout at most, the rest carries to the next."""

    def _us_debt(self, w, drv, minor):
        camp = campaign(w, node='US-TX-AUS', country='US', currency='USD')
        at = timezone.now() - timedelta(days=9)
        reward(drv.user_id, camp, minor, at=at)
        paid_out(drv, minor, at=at + timedelta(days=1))
        flag = farming_flag(w, drv.user_id, camp, node='US-TX-AUS')
        assert decide(w.super[0], flag, 'confirm', 'promo_abuse').status_code == 200
        request_id = body_of(recover(w, flag, minor, key='us'))['approval_request']['id']
        assert approve(request_id).status_code == 200
        return RecoveryDebt.objects.get(user=drv.user_id), flag

    def test_at_most_half_of_one_payout_and_the_rest_carries_to_the_next(self, money_world):
        w = money_world
        drv = us_driver(w, 'C1')
        debt, flag = self._us_debt(w, drv, 8_000)                    # $80.00 owed
        config = PayoutCountryConfig.objects.get(country='US', is_active=True)
        assert config.recovery_max_percent == 50

        credit(drv.user_id, 10_000, currency='USD')                  # $100.00 of earnings
        first = run_country(config, datetime(2026, 11, 2, 18, 0, tzinfo=dt_tz.utc))
        assert first['ran'] and first['withdrawals'] == 1
        w1 = WithdrawalRequest.objects.filter(driver=drv, origin='scheduled').latest('id')
        debt.refresh_from_db()
        assert w1.amount_minor == 5_000                              # $100 less 50 percent
        assert debt.recovered_minor == 5_000 and debt.remaining_minor == 3_000
        d1 = RecoveryDeduction.objects.get()
        assert (d1.amount_minor, d1.payout_gross_minor, d1.percent_cap) == (5_000, 10_000, 50)
        line = WalletTransaction.objects.get(purpose='reward_recovery')
        assert (line.txn_type, line.amount_minor) == ('debit', 5_000)
        assert get_wallet(drv.user_id, Wallet.SCOPE_DRIVER).balance_minor == 0
        assert not debt.closed_at

        WithdrawalRequest.objects.filter(pk=w1.pk).update(status='completed')
        credit(drv.user_id, 10_000, currency='USD')
        second = run_country(config, datetime(2026, 11, 9, 18, 0, tzinfo=dt_tz.utc))
        assert second['ran'] and second['withdrawals'] == 1
        w2 = WithdrawalRequest.objects.filter(driver=drv, origin='scheduled').latest('id')
        debt.refresh_from_db()
        assert w2.amount_minor == 7_000                              # only the $30 that remained
        assert debt.recovered_minor == 8_000 and debt.remaining_minor == 0 and debt.closed_at
        assert RecoveryDeduction.objects.count() == 2
        assert debt.recovered_minor <= debt.amount_minor              # never more than approved

    def test_a_payout_never_falls_below_the_minimum_because_of_a_recovery(self, money_world):
        w = money_world
        drv = us_driver(w, 'C2')
        debt, flag = self._us_debt(w, drv, 8_000)
        config = PayoutCountryConfig.objects.get(country='US', is_active=True)
        credit(drv.user_id, 150, currency='USD')                     # $1.50, minimum is $1.00
        run_country(config, datetime(2026, 11, 2, 18, 0, tzinfo=dt_tz.utc))
        w1 = WithdrawalRequest.objects.filter(driver=drv, origin='scheduled').latest('id')
        debt.refresh_from_db()
        assert w1.amount_minor >= 100
        assert debt.recovered_minor == 150 - w1.amount_minor <= 75

    def test_the_cap_follows_the_countrys_config(self, money_world):
        from conftest import seed_actor
        from servers.admin_audit.models import actor_snapshot
        from servers.payments.payout_config_write import values_of, write_config_version

        w = money_world
        drv = us_driver(w, 'C3')
        debt, flag = self._us_debt(w, drv, 8_000)
        active = PayoutCountryConfig.objects.get(country='US', is_active=True)
        config, _ = write_config_version(
            'US', {**values_of(active), 'recovery_max_percent': 20}, reason='test',
            snapshot=actor_snapshot(seed_actor()))
        credit(drv.user_id, 10_000, currency='USD')
        run_country(config, datetime(2026, 11, 2, 18, 0, tzinfo=dt_tz.utc))
        debt.refresh_from_db()
        assert debt.recovered_minor == 2_000

    def test_a_driver_requested_withdrawal_carries_the_same_cap(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'C4')
        camp = campaign(w)
        at = timezone.now() - timedelta(days=5)
        reward(drv.user_id, camp, 80_000, at=at)
        paid_out(drv, 80_000, at=at + timedelta(days=1))
        flag = farming_flag(w, drv.user_id, camp)
        decide(w.ops[0], flag, 'confirm', 'promo_abuse')
        approve(body_of(recover(w, flag, 80_000))['approval_request']['id'])
        credit(drv.user_id, 200_000)                                  # Rs 2,000
        r = client_for(drv.user_id).post('/api/v1/driver/withdrawals/request/',
                                         {'amount': '1000.00'}, format='json')
        assert r.status_code == 201, r.content
        debt = RecoveryDebt.objects.get()
        assert debt.recovered_minor == 50_000                         # 50 percent of Rs 1,000
        w1 = WithdrawalRequest.objects.filter(driver=drv).latest('id')
        assert w1.amount_minor == 50_000
        assert get_wallet(drv.user_id, Wallet.SCOPE_DRIVER).balance_minor == 100_000


class TestRiders:
    def _rider_flag(self, w, rider, friend, camp, ref):
        flag = make_flag(rider, w.geo['IN-TG-HYD'], kind='referral_abuse', evidence={
            'referral_id': ref.pk, 'signals': ['same_device'],
            'refs': {'counterpart_user_ids': [friend.pk], 'campaign_ids': [camp.pk]}})
        assert decide(w.ops[0], flag, 'confirm', 'promo_abuse').status_code == 200
        return flag

    def test_it_comes_from_the_wallet_only_and_never_below_zero(self, money_world):
        w = money_world
        rider, friend = make_rider('V1', '91'), make_rider('V2', '91')
        camp = campaign(w, kind='referral', name='Refer')
        entry, ref = referral_reward(rider, friend, camp, 10_000)
        wallet = get_wallet(rider, Wallet.SCOPE_RIDER, lock=True)
        wallet.balance_minor = 4_000                                  # the rest was spent
        wallet.save()
        flag = self._rider_flag(w, rider, friend, camp, ref)
        request_id = body_of(recover(w, flag, 10_000))['approval_request']['id']
        assert approve(request_id).status_code == 200
        wallet.refresh_from_db()
        debt = RecoveryDebt.objects.get()
        assert wallet.balance_minor == 0 and wallet.balance_minor >= 0
        assert (debt.role, debt.recovered_minor, debt.uncollected_minor) == ('rider', 4_000, 6_000)
        assert debt.closed_at and debt.remaining_minor == 0          # nothing else is collected
        line = WalletTransaction.objects.get(user_id=rider, purpose='reward_recovery')
        assert line.amount_minor == 4_000

    def test_an_empty_wallet_is_left_alone(self, money_world):
        w = money_world
        rider, friend = make_rider('V3', '91'), make_rider('V4', '91')
        camp = campaign(w, kind='referral', name='Refer')
        entry, ref = referral_reward(rider, friend, camp, 10_000)
        wallet = get_wallet(rider, Wallet.SCOPE_RIDER, lock=True)
        wallet.balance_minor = 0
        wallet.save()
        flag = self._rider_flag(w, rider, friend, camp, ref)
        approve(body_of(recover(w, flag, 10_000))['approval_request']['id'])
        wallet.refresh_from_db()
        debt = RecoveryDebt.objects.get()
        assert wallet.balance_minor == 0 and debt.recovered_minor == 0
        assert debt.uncollected_minor == 10_000
        assert not WalletTransaction.objects.filter(purpose='reward_recovery').exists()

    def test_the_rider_wallet_line_is_labelled_without_a_fraud_word(self, money_world):
        w = money_world
        rider, friend = make_rider('V5', '91'), make_rider('V6', '91')
        camp = campaign(w, kind='referral', name='Refer')
        entry, ref = referral_reward(rider, friend, camp, 10_000)
        flag = self._rider_flag(w, rider, friend, camp, ref)
        approve(body_of(recover(w, flag, 10_000))['approval_request']['id'])
        body = client_for(rider).get('/api/v1/rider/wallet/transactions/').content.decode()
        assert 'Recovery of reward (reviewed)' in body and 'fraud' not in body.lower()
        driver_body = ''.join(
            client_for(rider).get('/api/v1/rider/wallet/balance/').content.decode().lower())
        assert 'fraud' not in driver_body

    def test_the_driver_sees_the_recovery_line_and_what_is_left(self, money_world):
        w = money_world
        drv, camp, flag = confirmed_paid_driver_flag(w, 'D9', minor=30_000)
        approve(body_of(recover(w, flag, 30_000))['approval_request']['id'])
        summary = client_for(drv.user_id).get('/api/v1/driver/earnings/summary/').json()['data']
        assert summary['reward_recovery_label'] == 'Recovery of reward (reviewed)'
        assert summary['reward_recovery_remaining'] == '300.00'
        assert 'fraud' not in str(summary).lower()
