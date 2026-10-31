"""Module 12: driver incentives. Progress, the reward, the hard cap, the payout line.

THE RULINGS (owner, 2026-10-31, AUDIT 27.51)
--------------------------------------------
Paid in the weekly payout, NO commission, shown as a separate line in the driver's earnings and
ledger. Tax is go-live blocker 15: nothing is computed or withheld, and the line says
"tax: not configured". The budget is a hard cap; the reward is reserved at the moment the threshold
is crossed (a driver who crosses after the budget is used earns nothing).
"""

from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone

from promotions_support import (  # noqa: F401
    ADM, block_the_network, client_for, completed_trip, geo, get, hyd_driver, incentive_payload,
    launch, make_rider, post, settle, when, world,
)
from servers.promotions import incentives
from servers.promotions.models import (
    COMPLETED, INCENTIVE_REWARD, IN_PROGRESS, UNFUNDED, Campaign, IncentiveProgress,
    IncentiveTrip, LedgerEntry,
)
from servers.rider.models import Wallet, WalletTransaction

pytestmark = [pytest.mark.django_db]


@pytest.fixture
def quest(world):
    return launch(world.ops[0], incentive_payload())


def do_trip(world, driver, tag, **kw):
    trip = completed_trip(world, make_rider(tag, '91'), driver, **kw)
    incentives.on_trip_completed(trip)
    return trip


def progress_of(quest, driver):
    return IncentiveProgress.objects.get(campaign=quest, driver=driver)


def balance(driver):
    return Wallet.objects.get(user_id=driver.user_id, scope=Wallet.SCOPE_DRIVER).balance_minor


class TestProgress:
    def test_completed_trips_count_toward_the_target(self, world, quest):
        driver = hyd_driver(world, 'D1')
        do_trip(world, driver, 'a')
        do_trip(world, driver, 'b')
        progress = progress_of(quest, driver)
        assert (progress.trips_counted, progress.status) == (2, IN_PROGRESS)
        assert not LedgerEntry.objects.filter(kind=INCENTIVE_REWARD).exists()

    def test_progress_is_idempotent_per_trip(self, world, quest):
        driver = hyd_driver(world, 'D2')
        trip = do_trip(world, driver, 'a')
        for _ in range(3):                       # a redelivered task, a second hook
            assert incentives.on_trip_completed(trip) == []
        assert progress_of(quest, driver).trips_counted == 1
        assert IncentiveTrip.objects.filter(campaign=quest, trip=trip).count() == 1

    def test_a_trip_an_operator_ended_does_not_count(self, world, quest):
        driver = hyd_driver(world, 'D3')
        trip = completed_trip(world, make_rider('o', '91'), driver)
        trip.end_rule = 'capped'
        trip.save(update_fields=['end_rule'])
        assert incentives.on_trip_completed(trip) == []

    def test_only_trips_inside_the_window_count(self, world, quest):
        driver = hyd_driver(world, 'D4')
        trip = completed_trip(world, make_rider('w', '91'), driver)
        trip.completed_at = timezone.now() - timedelta(days=2)       # before the quest began
        assert incentives.on_trip_completed(trip) == []

    def test_a_driver_outside_the_campaigns_place_is_not_counted(self, world, quest):
        from finance_world import make_driver, make_trip

        us_driver = make_driver(world.geo, 'US-TX-AUS', 'US', currency='USD', calling='1')
        trip = make_trip(world.aus, make_rider('u', '1'), us_driver, fare='20.00')
        assert incentives.on_trip_completed(trip) == []

    def test_only_a_live_campaign_counts(self, world):
        made = launch(world.ops[0], incentive_payload())
        post(world.ops[0], f'{ADM}/campaigns/{made.id}/pause/', {}, expect=200)
        driver = hyd_driver(world, 'D5')
        trip = completed_trip(world, make_rider('p', '91'), driver)
        assert incentives.on_trip_completed(trip) == []


class TestFilters:
    def test_a_zone_filter(self, world):
        from finance_world import zone

        zone('IN-TG-HYD-N', world.geo['IN-TG-HYD'], country='IN', state_code='TG')
        payload = incentive_payload()
        payload['config']['zone_code'] = 'IN-TG-HYD-N'
        campaign = launch(world.ops[0], payload)
        driver = hyd_driver(world, 'F1')
        trip = completed_trip(world, make_rider('z', '91'), driver)       # in IN-TG-HYD
        assert incentives.on_trip_completed(trip) == []
        trip.zone = type(world.hyd).objects.get(code='IN-TG-HYD-N')
        trip.save(update_fields=['zone'])
        assert len(incentives.on_trip_completed(trip)) == 1
        assert progress_of(campaign, driver).trips_counted == 1

    def test_a_time_of_day_filter_that_wraps_midnight(self, world):
        payload = incentive_payload(starts_at=when(days=-3))
        payload['config'].update(hour_from=22, hour_to=6)
        launch(world.ops[0], payload)
        driver = hyd_driver(world, 'F2')
        # Asia/Kolkata is UTC+5:30: 17:30 UTC is 23:00 local (counts); 06:30 UTC is noon (does not).
        yesterday = timezone.now() - timedelta(days=1)
        night = completed_trip(world, make_rider('n', '91'), driver)
        night.completed_at = yesterday.replace(hour=17, minute=30)
        day = completed_trip(world, make_rider('d', '91'), driver)
        day.completed_at = yesterday.replace(hour=6, minute=30)
        assert len(incentives.on_trip_completed(night)) == 1
        assert incentives.on_trip_completed(day) == []

    def test_a_vehicle_class_filter(self, world):
        payload = incentive_payload()
        payload['config']['vehicle_type'] = 'suv'
        from servers.driver.models import VehicleType

        VehicleType.objects.get_or_create(type='suv')
        launch(world.ops[0], payload)
        driver = hyd_driver(world, 'F3')               # drives a sedan
        trip = completed_trip(world, make_rider('v', '91'), driver)
        assert incentives.on_trip_completed(trip) == []


class TestTheReward:
    def complete(self, world, driver, n):
        for i in range(n):
            do_trip(world, driver, f'{driver.id}-{i}')

    def test_crossing_the_threshold_pays_the_reward_once_with_no_commission(self, world, quest):
        driver = hyd_driver(world, 'R1')
        self.complete(world, driver, 3)
        progress = progress_of(quest, driver)
        assert (progress.status, progress.reward_minor) == (COMPLETED, 30_000)
        txn = WalletTransaction.objects.get(user_id=driver.user_id, purpose='incentive_reward')
        assert (txn.amount_minor, txn.txn_type, txn.status) == (30_000, 'credit', 'completed')
        assert balance(driver) == 30_000            # the whole reward: no commission came off it
        entry = LedgerEntry.objects.get(kind=INCENTIVE_REWARD)
        assert (entry.amount_minor, entry.user_id, entry.campaign_id) == (
            30_000, driver.user_id_id, quest.id)
        quest.refresh_from_db()
        assert (quest.spent_minor, quest.committed_minor) == (30_000, 0)

    def test_more_trips_and_replays_never_pay_it_twice(self, world, quest):
        driver = hyd_driver(world, 'R2')
        self.complete(world, driver, 5)
        trip = completed_trip(world, make_rider('x', '91'), driver)
        incentives.on_trip_completed(trip)
        incentives.on_trip_completed(trip)
        assert WalletTransaction.objects.filter(purpose='incentive_reward').count() == 1
        assert balance(driver) == 30_000

    def test_the_budget_is_a_hard_cap_reserved_at_the_threshold(self, world):
        """One reward of budget: whoever crosses first is paid; the campaign then stops, so the
        driver who was one trip behind earns nothing and is not counted any more."""
        quest = launch(world.ops[0], incentive_payload(budget_minor=30_000))
        first, second = hyd_driver(world, 'R3'), hyd_driver(world, 'R4')
        self.complete(world, first, 2)
        self.complete(world, second, 2)
        self.complete(world, first, 1)                      # crosses first: reserved and paid
        self.complete(world, second, 1)                     # the budget is used up by now
        assert progress_of(quest, first).status == COMPLETED
        assert progress_of(quest, second).trips_counted == 2
        assert not WalletTransaction.objects.filter(user_id=second.user_id,
                                                    purpose='incentive_reward').exists()
        quest.refresh_from_db()
        assert quest.state == 'exhausted' and quest.spent_minor == 30_000

    def test_a_crossing_that_loses_the_race_for_the_last_budget_is_marked_unfunded(self, world):
        quest = launch(world.ops[0], incentive_payload())
        driver = hyd_driver(world, 'R3B')
        self.complete(world, driver, 2)
        # Another reward took the last of the budget between this driver's check and their trip.
        Campaign.objects.filter(pk=quest.pk).update(spent_minor=100_000)
        self.complete(world, driver, 1)
        progress = progress_of(quest, driver)
        assert (progress.status, progress.reward_minor) == (UNFUNDED, 0)
        assert not WalletTransaction.objects.filter(purpose='incentive_reward').exists()
        assert balance(driver) == 0

    def test_the_driver_sees_progress_and_a_used_up_budget(self, world):
        quest = launch(world.ops[0], incentive_payload(budget_minor=30_000))
        first, second = hyd_driver(world, 'R5'), hyd_driver(world, 'R6')
        self.complete(world, first, 2)
        self.complete(world, second, 2)
        self.complete(world, first, 1)
        row = client_for(second.user_id).get('/api/v1/promotions/driver/incentives/').json()
        item = row['data']['incentives'][0]
        assert item['id'] == quest.id and item['progress_trips'] == 2
        assert item['state'] == 'exhausted' and item['budget_used_up'] is True
        assert (item['commission'], item['tax']) == ('none', 'not configured')

    def test_the_driver_api_shows_a_live_quest_in_progress(self, world, quest):
        driver = hyd_driver(world, 'R7')
        do_trip(world, driver, 'p')
        data = client_for(driver.user_id).get('/api/v1/promotions/driver/incentives/').json()['data']
        item = data['incentives'][0]
        assert (item['progress_trips'], item['target_trips'], item['status']) == (1, 3, 'in_progress')
        assert item['reward_minor'] == 30_000 and item['currency'] == 'INR'

    def test_the_driver_api_is_for_drivers(self, world, quest):
        assert client_for(make_rider('r', '91')).get(
            '/api/v1/promotions/driver/incentives/').status_code == 403


class TestPaidWithTheWeeklyPayout:
    def test_the_reward_is_swept_into_the_next_weekly_payout(self, world):
        from finance_world import seed_in_payout_config
        from servers.driver.models import WithdrawalRequest
        from servers.payments.models import PayoutCountryConfig
        from servers.payments.weekly_payout import _create_one

        seed_in_payout_config()
        payload = incentive_payload(budget_minor=500_000)
        payload['config']['reward_minor'] = 60_000                      # above the 500.00 minimum
        launch(world.ops[0], payload)
        driver = hyd_driver(world, 'W1')
        for i in range(3):
            do_trip(world, driver, f'w{i}')
        config = PayoutCountryConfig.objects.get(country='IN', is_active=True)
        withdrawal = _create_one(driver, config)
        assert isinstance(withdrawal, WithdrawalRequest)
        assert withdrawal.amount_minor == 60_000 and withdrawal.origin == 'scheduled'

    def test_the_reward_is_its_own_line_in_the_drivers_earnings_and_ledger(self, world, quest):
        driver = hyd_driver(world, 'W2')
        for i in range(3):
            do_trip(world, driver, f'l{i}')
        settle(completed_trip(world, make_rider('s', '91'), driver, fare='100.00'))
        summary = client_for(driver.user_id).get('/api/v1/driver/earnings/summary/').json()['data']
        assert summary['incentive_rewards'] == '300.00' and summary['referral_rewards'] == '0.00'
        assert summary['incentive_commission'] == 'none'
        assert summary['incentive_tax'] == 'not configured'
        assert summary['total_earned'] == '100.00'            # trip fares only
        assert summary['total_commission'] == '15.00'         # on the fare, not on the reward

        data = get(world.finance[0], f'/api/v1/payments/admin/drivers/{driver.id}/ledger/')
        entries = [e for e in data['entries']['results'] if e['purpose'] == 'incentive_reward']
        assert len(entries) == 1
        line = entries[0]
        assert (line['kind'], line['direction'], line['amount_minor']) == (
            'incentive_reward', 'credit', 30_000)
        assert (line['commission'], line['tax']) == ('none', 'not configured')
        # The balance is still explained by the ledger: the reward is a row in it, not a patch.
        assert data['unexplained_minor'] == 0


class TestNotifications:
    def test_progress_and_completion_go_through_the_dispatcher(self, world, quest):
        driver = hyd_driver(world, 'N1')
        with mock.patch('servers.notifications.dispatcher.notify') as notify:
            for i in range(3):
                do_trip(world, driver, f'n{i}')
        events = [call.args[0] for call in notify.call_args_list]
        assert events == ['promotions.incentive_progress', 'promotions.incentive_progress',
                          'promotions.incentive_completed']
        completed = notify.call_args_list[-1]
        assert completed.args[1] == driver.user_id
        assert completed.args[2]['reward'] and completed.kwargs['idempotency_key']

    def test_the_seeded_templates_render_and_queue_in_app_and_push(self, world, quest):
        from io import StringIO

        from django.core.management import call_command

        from conftest import seed_actor
        from servers.notifications.models import NotificationMessage

        call_command('seed_notification_templates', actor_email=seed_actor().email,
                     stdout=StringIO())
        driver = hyd_driver(world, 'N2')
        for i in range(3):
            do_trip(world, driver, f'm{i}')
        rows = NotificationMessage.objects.filter(recipient_user_id=driver.user_id_id)
        keys = {r.template_key for r in rows}
        assert keys == {'promotions.incentive_progress', 'promotions.incentive_completed'}
        assert {r.channel for r in rows} == {'in_app', 'push'}
        done = rows.get(template_key='promotions.incentive_completed', channel='in_app')
        assert 'Weekend 3' in done.body and 'no commission' in done.body

    def test_promo_available_is_a_promotional_template_that_respects_opt_in(self, world):
        from io import StringIO

        from django.core.management import call_command

        from conftest import seed_actor
        from servers.notifications.models import NotificationTemplate

        call_command('seed_notification_templates', actor_email=seed_actor().email,
                     stdout=StringIO())
        templates = NotificationTemplate.objects.filter(key='promotions.promo_available')
        assert {(t.country, t.channel) for t in templates} == {
            ('IN', 'in_app'), ('IN', 'push'), ('US', 'in_app'), ('US', 'push')}
        assert {t.category for t in templates} == {'promotional'}
        assert {t.status for t in templates} == {'active'}


class TestLegacyQuestsKeepWorking:
    def test_the_existing_driver_endpoint_is_unchanged(self, world):
        from servers.driver.models import IncentiveQuest

        IncentiveQuest.objects.create(
            title='Old quest', target_trips=2, reward_amount_minor=10_000, currency='INR',
            start_date=timezone.now() - timedelta(days=1),
            end_date=timezone.now() + timedelta(days=1))
        driver = hyd_driver(world, 'L1')
        completed_trip(world, make_rider('l', '91'), driver)
        data = client_for(driver.user_id).get('/api/v1/driver/driver/incentives/')
        assert data.status_code == 200
        body = data.json()['data']
        assert body[0]['title'] == 'Old quest' and body[0]['progress_trips'] == 1
        assert body[0]['is_completed'] is False

    def test_a_wrapped_legacy_quest_is_never_paid_by_the_new_engine(self, world):
        from servers.driver.models import IncentiveQuest
        from servers.promotions.rider_promos import ensure_legacy_campaign  # noqa: F401

        quest = IncentiveQuest.objects.create(
            title='Legacy', target_trips=1, reward_amount_minor=10_000, currency='INR',
            start_date=timezone.now() - timedelta(days=1),
            end_date=timezone.now() + timedelta(days=1))
        Campaign.objects.create(
            kind='driver_incentive', name='Legacy', state='live', country='IN',
            geo_node=world.geo['IN'], currency='INR', is_legacy=True, quest=quest,
            starts_at=quest.start_date, ends_at=quest.end_date,
            config={'target_trips': 1, 'reward_minor': 10_000})
        driver = hyd_driver(world, 'L2')
        do_trip(world, driver, 'l2')
        assert not WalletTransaction.objects.filter(purpose='incentive_reward').exists()
