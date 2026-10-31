"""Ruling (a), AUDIT 27.56: an open flag holds ONLY the unpaid incentive / referral reward linked to
the flagged activity, never trip earnings; the hold auto-releases after `reward_hold_days` (7, per
country) if the flag is not decided; a dismissal releases it at once; the driver or rider sees
"pending review" and never the word fraud.
"""

from datetime import datetime, timedelta, timezone as dt_tz

import pytest
from django.utils import timezone

from fraud_money_support import (  # noqa: F401
    ADM, block_the_network, body_of, campaign, client_for, credit, decide, error_code, farming_flag,
    flag_url, geo, get, hyd_driver, make_flag, make_rider, operator, paid_out, post,
    referral_reward, reward, seed_money, us_driver, world,
)
from servers.driver.services import calculate_available_balance
from servers.fraud import money
from servers.fraud.models import RewardHold
from servers.payments.models import PayoutCountryConfig
from servers.rider.models import Wallet, WalletTransaction, get_wallet

pytestmark = pytest.mark.django_db


@pytest.fixture
def money_world(world):
    seed_money()
    return world


class TestWhatIsHeld:
    def test_only_the_linked_unpaid_reward_is_held_never_trip_earnings(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'H1')
        user = drv.user_id
        credit(user, 90_000)                                  # trip earnings
        linked = campaign(w, name='Farm A')
        other = campaign(w, name='Other B')
        reward(user, linked, 30_000)
        reward(user, other, 10_000)
        earnings_before = list(WalletTransaction.objects.filter(
            user_id=user, purpose='trip_earnings').values_list('id', 'amount_minor', 'status'))
        wallet_before = get_wallet(user, Wallet.SCOPE_DRIVER).balance_minor
        flag = farming_flag(w, user, linked)

        holds = money.hold_for_flag(flag)

        assert [(h.amount_minor, h.kind) for h in holds] == [(30_000, 'incentive_reward')]
        assert money.held_minor(user) == 30_000
        # The wallet itself is untouched: a hold is a reserve, not a debit.
        assert get_wallet(user, Wallet.SCOPE_DRIVER).balance_minor == wallet_before
        assert list(WalletTransaction.objects.filter(
            user_id=user, purpose='trip_earnings').values_list('id', 'amount_minor', 'status')
        ) == earnings_before
        assert not WalletTransaction.objects.filter(
            user_id=user, purpose__in=('trip_commission', 'platform_funded_trip_end')).exists()
        # What the driver can withdraw excludes the held reward and nothing else.
        assert calculate_available_balance(drv) * 100 == wallet_before - 30_000

    def test_a_reward_already_paid_out_is_not_held(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'H2')
        camp = campaign(w)
        at = timezone.now() - timedelta(days=3)
        reward(drv.user_id, camp, 30_000, at=at)
        paid_out(drv, 30_000, at=at + timedelta(days=1))
        assert money.hold_for_flag(farming_flag(w, drv.user_id, camp)) == []

    def test_a_hold_never_exceeds_what_the_wallet_still_holds(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'H3')
        camp = campaign(w)
        reward(drv.user_id, camp, 30_000)
        wallet = get_wallet(drv.user_id, Wallet.SCOPE_DRIVER, lock=True)
        wallet.balance_minor = 12_000                         # most of it was spent or deducted
        wallet.save()
        (hold,) = money.hold_for_flag(farming_flag(w, drv.user_id, camp))
        assert hold.amount_minor == 12_000 and hold.reward_minor == 30_000

    def test_holding_twice_changes_nothing(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'H4')
        camp = campaign(w)
        reward(drv.user_id, camp, 30_000)
        flag = farming_flag(w, drv.user_id, camp)
        assert len(money.hold_for_flag(flag)) == 1
        assert money.hold_for_flag(flag) == []
        assert RewardHold.objects.count() == 1

    def test_a_flag_kind_with_no_reward_link_holds_nothing(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'H5')
        camp = campaign(w)
        reward(drv.user_id, camp, 30_000)
        flag = make_flag(drv.user_id, w.geo['IN-TG-HYD'], kind='gps_anomaly')
        assert money.hold_for_flag(flag) == []

    def test_a_decided_flag_holds_nothing_new(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'H6')
        camp = campaign(w)
        reward(drv.user_id, camp, 30_000)
        flag = farming_flag(w, drv.user_id, camp, status='dismissed')
        assert money.hold_for_flag(flag) == []

    def test_the_hook_in_raising_holds_when_a_rule_raises_the_flag(self, money_world):
        from servers.fraud import raising
        from servers.fraud.rules import RULES
        from servers.fraud.rules.base import Hit

        w = money_world
        drv = hyd_driver(w, 'H7')
        camp = campaign(w)
        reward(drv.user_id, camp, 30_000)
        hit = Hit(subject=drv.user_id, severity='medium', score=60, signals=['short_trips'],
                  refs={'campaign_ids': [camp.pk]}, measures={})
        flag, outcome = raising.raise_hit(RULES['incentive_farming'], None, hit)
        assert outcome == 'created'
        assert money.held_minor(drv.user_id) == 30_000
        assert RewardHold.objects.get().flag_id == flag.pk

    def test_collusion_links_the_incentive_that_counted_a_flagged_trip(self, money_world):
        from finance_world import make_trip
        from servers.promotions.models import IncentiveTrip

        w = money_world
        drv = hyd_driver(w, 'H8')
        rider = make_rider('C8', '91')
        camp = campaign(w)
        unrelated = campaign(w, name='Unrelated')
        trip = make_trip(w.hyd, rider, drv, fare='200.00')
        IncentiveTrip.objects.create(campaign=camp, driver=drv, trip=trip)
        reward(drv.user_id, camp, 30_000)
        reward(drv.user_id, unrelated, 5_000)
        flag = make_flag(drv.user_id, w.geo['IN-TG-HYD'], kind='collusion', evidence={
            'signals': ['repeat_pair'], 'refs': {'trip_ids': [trip.pk],
                                                 'counterpart_user_ids': [rider.pk]},
            'measures': {}, 'hit_count': 1})
        assert [h.amount_minor for h in money.hold_for_flag(flag)] == [30_000]

    def test_a_rider_referral_reward_is_held_on_the_riders_wallet(self, money_world):
        w = money_world
        rider, friend = make_rider('R9', '91'), make_rider('F9', '91')
        camp = campaign(w, kind='referral', name='Refer')
        entry, ref = referral_reward(rider, friend, camp, 10_000)
        flag = make_flag(rider, w.geo['IN-TG-HYD'], kind='referral_abuse', evidence={
            'referral_id': ref.pk, 'signals': ['same_device'],
            'refs': {'counterpart_user_ids': [friend.pk], 'campaign_ids': [camp.pk]}})
        (hold,) = money.hold_for_flag(flag)
        assert hold.role == 'rider' and hold.ledger_entry_id == entry.pk
        # Not spendable while held: the rider's wallet payment is refused for that part.
        from base.utils import wallet_payment

        refused = wallet_payment(rider, '100.00', reference_id='TRIP_1', idempotency_key='k-held')
        assert refused['success'] is False


class TestWhenItEnds:
    def test_auto_release_after_seven_days_if_the_flag_is_not_decided(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'T1')
        camp = campaign(w)
        reward(drv.user_id, camp, 30_000)
        (hold,) = money.hold_for_flag(farming_flag(w, drv.user_id, camp))
        assert hold.release_at - hold.held_at == timedelta(days=7)
        assert money.release_due(hold.held_at + timedelta(days=7, minutes=-1)) == 0
        assert money.held_minor(drv.user_id) == 30_000
        assert money.release_due(hold.held_at + timedelta(days=7, minutes=1)) == 1
        hold.refresh_from_db()
        assert hold.status == 'released' and hold.release_reason == 'auto_expired'
        assert money.held_minor(drv.user_id) == 0
        # The flag is still open and the reward is not held again.
        assert money.hold_for_flag(hold.flag) == []

    def test_the_hold_days_come_from_the_countrys_config(self, money_world):
        from servers.payments.payout_config_write import values_of, write_config_version
        from conftest import seed_actor
        from servers.admin_audit.models import actor_snapshot

        w = money_world
        active = PayoutCountryConfig.objects.get(country='US', is_active=True)
        assert (active.reward_hold_days, active.recovery_max_percent,
                active.payout_hold_max_days) == (7, 50, 14)
        write_config_version('US', {**values_of(active), 'reward_hold_days': 3},
                             reason='test', snapshot=actor_snapshot(seed_actor()))
        drv = us_driver(w)
        camp = campaign(w, node='US-TX-AUS', country='US', currency='USD')
        reward(drv.user_id, camp, 3_000, )
        (hold,) = money.hold_for_flag(farming_flag(w, drv.user_id, camp, node='US-TX-AUS'))
        assert hold.release_at - hold.held_at == timedelta(days=3)

    def test_a_dismissal_releases_the_hold_at_once(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'T3')
        camp = campaign(w)
        reward(drv.user_id, camp, 30_000)
        flag = farming_flag(w, drv.user_id, camp)
        money.hold_for_flag(flag)
        data = body_of(decide(w.ops[0], flag, 'dismiss', 'false_positive'))
        assert data['status'] == 'dismissed'
        assert RewardHold.objects.get().status == 'released'
        assert RewardHold.objects.get().release_reason == 'dismissed'
        assert money.held_minor(drv.user_id) == 0

    def test_confirming_promo_abuse_keeps_the_hold_for_finance_and_restarts_its_clock(
            self, money_world):
        w = money_world
        drv = hyd_driver(w, 'T4')
        camp = campaign(w)
        reward(drv.user_id, camp, 30_000)
        flag = farming_flag(w, drv.user_id, camp)
        money.hold_for_flag(flag)
        decide(w.ops[0], flag, 'confirm', 'promo_abuse')
        hold = RewardHold.objects.get()
        assert hold.status == 'held'
        assert abs((hold.release_at - timezone.now()) - timedelta(days=7)) < timedelta(minutes=1)
        # And it is still bounded: it lapses at the new time, as 'expired after confirm'.
        assert money.release_due(hold.release_at + timedelta(minutes=1)) == 1
        hold.refresh_from_db()
        assert hold.release_reason == 'expired_after_confirm'

    def test_confirming_for_a_reason_that_does_not_recover_releases_it(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'T5')
        camp = campaign(w)
        reward(drv.user_id, camp, 30_000)
        flag = farming_flag(w, drv.user_id, camp)
        money.hold_for_flag(flag)
        decide(w.ops[0], flag, 'confirm', 'other')
        hold = RewardHold.objects.get()
        assert hold.status == 'released' and hold.release_reason == 'confirmed_no_recovery'

    def test_nothing_is_forfeited_by_a_decision(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'T6')
        camp = campaign(w)
        reward(drv.user_id, camp, 30_000)
        flag = farming_flag(w, drv.user_id, camp)
        money.hold_for_flag(flag)
        before = get_wallet(drv.user_id, Wallet.SCOPE_DRIVER).balance_minor
        decide(w.ops[0], flag, 'confirm', 'promo_abuse')
        assert get_wallet(drv.user_id, Wallet.SCOPE_DRIVER).balance_minor == before
        assert not WalletTransaction.objects.filter(purpose='reward_recovery').exists()


class TestThePayout:
    def test_the_weekly_payout_leaves_a_held_reward_in_the_wallet(self, money_world):
        from servers.payments.models import PayoutCountryConfig
        from servers.payments.weekly_payout import run_country

        w = money_world
        drv = us_driver(w)
        credit(drv.user_id, 9_000, currency='USD')
        camp = campaign(w, node='US-TX-AUS', country='US', currency='USD')
        reward(drv.user_id, camp, 4_000)
        money.hold_for_flag(farming_flag(w, drv.user_id, camp, node='US-TX-AUS'))
        config = PayoutCountryConfig.objects.get(country='US', is_active=True)
        monday = datetime(2026, 11, 2, 18, 0, tzinfo=dt_tz.utc)
        result = run_country(config, monday)
        assert result['ran'] and result['withdrawals'] == 1
        from servers.driver.models import WithdrawalRequest

        withdrawal = WithdrawalRequest.objects.get(driver=drv)
        assert withdrawal.amount_minor == 9_000                      # earnings only
        assert get_wallet(drv.user_id, Wallet.SCOPE_DRIVER).balance_minor == 4_000

    def test_a_driver_cannot_withdraw_the_held_part(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'P2')
        credit(drv.user_id, 60_000)
        camp = campaign(w)
        reward(drv.user_id, camp, 30_000)
        money.hold_for_flag(farming_flag(w, drv.user_id, camp))
        client = client_for(drv.user_id)
        refused = client.post('/api/v1/driver/withdrawals/request/', {'amount': '800.00'},
                              format='json')
        assert refused.status_code == 400 and 'Insufficient' in refused.json()['error']['message']
        ok = client.post('/api/v1/driver/withdrawals/request/', {'amount': '600.00'},
                         format='json')
        assert ok.status_code == 201, ok.content


class TestWhatTheAppsSee:
    BAD = ('fraud', 'abuse', 'collusion', 'suspect')

    def test_the_driver_sees_pending_review_and_never_the_word_fraud(self, money_world):
        w = money_world
        drv = hyd_driver(w, 'A1')
        camp = campaign(w)
        reward(drv.user_id, camp, 30_000)
        flag = farming_flag(w, drv.user_id, camp)
        money.hold_for_flag(flag)
        decide(w.ops[0], flag, 'confirm', 'promo_abuse', note='Fraud ring, confirmed')
        client = client_for(drv.user_id)
        seen = {}
        for path in ('earnings/summary/', 'earnings/', 'withdrawals/balance/',
                     'withdrawals/history/', 'driver/incentives/', 'withdrawals/block-status/'):
            r = client.get(f'/api/v1/driver/{path}')
            assert r.status_code == 200, (path, r.content[:200])
            seen[path] = r.content.decode().lower()
        refused = client.post('/api/v1/driver/withdrawals/request/', {'amount': '9999.00'},
                              format='json')
        seen['refused'] = refused.content.decode().lower()
        r = client.get('/api/v1/promotions/referrals/mine/')
        seen['referrals'] = r.content.decode().lower()
        for path, text in seen.items():
            for word in self.BAD:
                assert word not in text, (path, word)
        summary = client.get('/api/v1/driver/earnings/summary/').json()['data']
        assert summary['rewards_pending_review_label'] == 'Pending review'
        assert summary['rewards_pending_review'] == '300.00'

    def test_the_incentive_row_says_pending_review(self, money_world):
        from servers.promotions.incentives import driver_view

        w = money_world
        drv = hyd_driver(w, 'A2')
        camp = campaign(w)
        reward(drv.user_id, camp, 30_000)
        money.hold_for_flag(farming_flag(w, drv.user_id, camp))
        rows = {r['id']: r for r in driver_view(drv)}
        assert rows[camp.pk]['reward_status'] == 'pending_review'
        assert rows[camp.pk]['reward_status_label'] == 'Pending review'

    def test_the_rider_sees_pending_review_and_never_the_word_fraud(self, money_world):
        w = money_world
        rider, friend = make_rider('A3', '91'), make_rider('A4', '91')
        camp = campaign(w, kind='referral', name='Refer')
        entry, ref = referral_reward(rider, friend, camp, 10_000)
        flag = make_flag(rider, w.geo['IN-TG-HYD'], kind='referral_abuse', evidence={
            'referral_id': ref.pk, 'signals': ['same_device'],
            'refs': {'counterpart_user_ids': [friend.pk], 'campaign_ids': [camp.pk]}})
        money.hold_for_flag(flag)
        client = client_for(rider)
        balance = client.get('/api/v1/rider/wallet/balance/').json()['data']
        assert balance['rewards_pending_review_label'] == 'Pending review'
        assert balance['rewards_pending_review'] == '100.00'
        for path in ('wallet/balance/', 'wallet/transactions/'):
            text = client.get(f'/api/v1/rider/{path}').content.decode().lower()
            assert 'fraud' not in text
        mine = client.get('/api/v1/promotions/referrals/mine/').json()['data']
        assert mine['referrals'][0]['reward_status'] == 'pending_review'
        assert 'fraud' not in str(mine).lower()
