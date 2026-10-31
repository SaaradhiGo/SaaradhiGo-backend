"""Module 12: referrals. Reward after N trips, three blocks, the fraud queue, the budget cap.

THE RULINGS (owner, 2026-10-31, AUDIT 27.51)
--------------------------------------------
* The reward is earned only after the referee completes N trips (N is set per campaign, so per
  country).
* No reward across the same DEVICE, PHONE NUMBER or PAYMENT INSTRUMENT. A blocked reward goes to
  the fraud queue (`fraud.RiskFlag`) and, for a rider, the existing `RiderFlag('promo_abuse')`.
* A budget cap per campaign.

WHAT THE PLATFORM STORES (and so what is compared): no device id exists, so the push token stands
in (and a device hash the app may send at attach); phone numbers are compared by their last ten digits
including the emergency contact; payout instruments (UPI, bank account) exist for DRIVERS only, a rider
has none stored. See `servers/promotions/referrals.py`.
"""

from unittest import mock

import pytest

from finance_world import make_driver
from promotions_support import (  # noqa: F401
    ADM, approve_latest, block_the_network, body_of, client_for, completed_trip, create,
    error_code, geo, get, hyd_driver, launch, make_rider, post, referral_payload, world,
)
from servers.fraud.models import RiskFlag
from servers.promotions import referrals
from servers.promotions.models import (
    ATTACHED, BLOCKED, QUALIFIED, REFERRAL_REWARD, REWARDED, Campaign, LedgerEntry, Referral,
)
from servers.rider.models import RiderFlag, Wallet, WalletTransaction

pytestmark = [pytest.mark.django_db]

API = '/api/v1/promotions'


@pytest.fixture
def campaign(world):
    return launch(world.mkt[0], referral_payload('rider'))


@pytest.fixture
def driver_campaign(world):
    return launch(world.ops[0], referral_payload('driver', scope='IN-TG-HYD'))


def code_of(user):
    return client_for(user).get(f'{API}/referrals/mine/').json()['data']['code']


def attach(referee, code, expect=201, **extra):
    response = client_for(referee).post(f'{API}/referrals/attach/', {'code': code, **extra},
                                        format='json')
    assert response.status_code == expect, (response.status_code, response.content[:300])
    return response


def pair(world):
    """A referrer and a referee rider, attached under the live campaign."""
    referrer, referee = make_rider('REF', '91'), make_rider('NEW', '91')
    attach(referee, code_of(referrer))
    return referrer, referee


def referee_trips(world, referee, n, start=0):
    driver = hyd_driver(world, f'RD{referee.id}')
    out = []
    for i in range(start, start + n):
        trip = completed_trip(world, referee, driver)
        referrals.on_trip_completed(trip)
        out.append(trip)
    return out


def rider_balance(user):
    wallet = Wallet.objects.filter(user_id=user, scope=Wallet.SCOPE_RIDER).first()
    return wallet.balance_minor if wallet else 0


class TestAttribution:
    def test_every_user_has_one_stable_shareable_code(self, world, campaign):
        rider = make_rider('C1', '91')
        first = client_for(rider).get(f'{API}/referrals/mine/').json()['data']
        again = client_for(rider).get(f'{API}/referrals/mine/').json()['data']
        assert first['code'] == again['code'] and len(first['code']) == 8
        assert first['offer']['required_trips'] == 2
        assert first['offer']['referrer_reward_minor'] == 10_000 and first['referrals'] == []

    def test_attach_records_the_referee_under_the_live_campaign(self, world, campaign):
        referrer, referee = pair(world)
        referral = Referral.objects.get(referee=referee)
        assert (referral.referrer_id, referral.campaign_id, referral.status) == (
            referrer.id, campaign.id, ATTACHED)
        mine = client_for(referee).get(f'{API}/referrals/mine/').json()['data']
        assert mine['attached_to'] == {'status': 'attached', 'trips_completed': 0,
                                       'required_trips': 2}
        theirs = client_for(referrer).get(f'{API}/referrals/mine/').json()['data']
        assert theirs['referrals'][0]['status'] == 'attached'
        assert theirs['referrals'][0]['required_trips'] == 2

    def test_the_refusals(self, world, campaign):
        referrer, referee = make_rider('A', '91'), make_rider('B', '91')
        code = code_of(referrer)
        for who, body, status, name in (
                (referee, {'code': ''}, 400, 'CODE_REQUIRED'),
                (referee, {'code': 'NOSUCHCOD'}, 404, 'CODE_NOT_FOUND'),
                (referrer, {'code': code}, 400, 'SELF_REFERRAL')):
            response = client_for(who).post(f'{API}/referrals/attach/', body, format='json')
            assert response.status_code == status and error_code(response) == name
        attach(referee, code)
        assert error_code(client_for(referee).post(
            f'{API}/referrals/attach/', {'code': code}, format='json')) == 'ALREADY_REFERRED'

    def test_a_driver_cannot_use_a_rider_code(self, world, campaign):
        code = code_of(make_rider('A', '91'))
        driver = hyd_driver(world, 'X')
        response = client_for(driver.user_id).post(f'{API}/referrals/attach/', {'code': code},
                                                   format='json')
        assert response.status_code == 404 and error_code(response) == 'CODE_NOT_FOUND'

    def test_a_code_must_be_used_before_the_first_trip(self, world, campaign):
        referrer, veteran = make_rider('A', '91'), make_rider('V', '91')
        completed_trip(world, veteran, hyd_driver(world, 'V'))
        response = client_for(veteran).post(f'{API}/referrals/attach/',
                                            {'code': code_of(referrer)}, format='json')
        assert response.status_code == 409 and error_code(response) == 'TOO_LATE'

    def test_with_no_live_campaign_there_is_nothing_to_attach_to(self, world):
        referrer, referee = make_rider('A', '91'), make_rider('B', '91')
        response = client_for(referee).post(f'{API}/referrals/attach/',
                                            {'code': code_of(referrer)}, format='json')
        assert response.status_code == 409 and error_code(response) == 'NO_ACTIVE_CAMPAIGN'

    def test_each_country_has_its_own_threshold(self, world, campaign):
        us = referral_payload('rider', scope='US', name='Refer a US rider')
        us['config']['required_trips'] = 3
        us_campaign = launch(world.super[0], us)
        assert us_campaign.currency == 'USD'
        us_referrer, us_referee = make_rider('UA', '1'), make_rider('UB', '1')
        attach(us_referee, code_of(us_referrer))
        in_referrer, in_referee = make_rider('IA', '91'), make_rider('IB', '91')
        attach(in_referee, code_of(in_referrer))
        assert Referral.objects.get(referee=us_referee).campaign_id == us_campaign.id
        assert Referral.objects.get(referee=in_referee).campaign_id == campaign.id
        us_mine = client_for(us_referrer).get(f'{API}/referrals/mine/').json()['data']
        assert us_mine['referrals'][0]['required_trips'] == 3

    def test_the_app_may_send_a_device_id_and_only_a_hash_is_kept(self, world, campaign):
        referrer, referee = make_rider('A', '91'), make_rider('B', '91')
        attach(referee, code_of(referrer), device_id='handset-9000')
        referral = Referral.objects.get(referee=referee)
        assert referral.device_hash and 'handset-9000' not in referral.device_hash
        assert len(referral.device_hash) == 64


class TestTheReward:
    def test_nothing_is_paid_before_the_threshold(self, world, campaign):
        referrer, referee = pair(world)
        referee_trips(world, referee, 1)
        referral = Referral.objects.get(referee=referee)
        assert (referral.status, referral.trips_completed) == (ATTACHED, 1)
        assert rider_balance(referrer) == 0 and rider_balance(referee) == 0

    def test_the_second_trip_pays_both_riders_as_wallet_credit(self, world, campaign):
        referrer, referee = pair(world)
        referee_trips(world, referee, 2)
        referral = Referral.objects.get(referee=referee)
        assert referral.status == REWARDED and referral.rewarded_at
        assert (referral.referrer_reward_minor, referral.referee_reward_minor) == (10_000, 5_000)
        assert rider_balance(referrer) == 10_000 and rider_balance(referee) == 5_000
        txn = WalletTransaction.objects.get(user_id=referrer, purpose__startswith='promo:referral')
        assert (txn.txn_type, txn.amount_minor) == ('credit', 10_000)
        assert LedgerEntry.objects.filter(kind=REFERRAL_REWARD).count() == 2
        campaign.refresh_from_db()
        assert campaign.spent_minor == 15_000

    def test_a_replayed_trip_counts_once_and_pays_once(self, world, campaign):
        referrer, referee = pair(world)
        trips = referee_trips(world, referee, 2)
        for trip in trips:
            referrals.on_trip_completed(trip)
        extra = referee_trips(world, referee, 1, start=5)
        assert extra
        assert rider_balance(referrer) == 10_000
        assert WalletTransaction.objects.filter(purpose__startswith='promo:referral').count() == 2

    def test_a_referral_reward_can_be_for_one_side_only(self, world):
        payload = referral_payload('rider')
        payload['config'].update(referrer_reward_minor=7_000, referee_reward_minor=0)
        launch(world.mkt[0], payload)
        referrer, referee = pair(world)
        referee_trips(world, referee, 2)
        assert rider_balance(referrer) == 7_000 and rider_balance(referee) == 0

    def test_rewards_are_announced_through_the_dispatcher(self, world, campaign):
        referrer, referee = pair(world)
        with mock.patch('servers.notifications.dispatcher.notify') as notify:
            referee_trips(world, referee, 2)
        events = {(call.args[0], call.args[1].pk) for call in notify.call_args_list}
        assert events == {('promotions.referral_reward', referrer.pk),
                          ('promotions.referral_reward', referee.pk)}

    def test_a_driver_referral_pays_a_ledger_credit_with_no_commission(self, world,
                                                                       driver_campaign):
        referrer = hyd_driver(world, 'DR1')
        referee = hyd_driver(world, 'DR2')
        attach(referee.user_id, code_of(referrer.user_id))
        rider = make_rider('P', '91')
        for _ in range(2):
            referrals.on_trip_completed(completed_trip(world, rider, referee))
        referral = Referral.objects.get(referee=referee.user_id)
        assert referral.status == REWARDED
        for driver, amount in ((referrer, 10_000), (referee, 5_000)):
            txn = WalletTransaction.objects.get(user_id=driver.user_id, purpose='referral_reward')
            assert (txn.txn_type, txn.amount_minor, txn.status) == ('credit', amount, 'completed')
            wallet = Wallet.objects.get(user_id=driver.user_id, scope=Wallet.SCOPE_DRIVER)
            assert wallet.balance_minor == amount            # nothing came off it
        summary = client_for(referrer.user_id).get('/api/v1/driver/earnings/summary/').json()['data']
        assert summary['referral_rewards'] == '100.00' and summary['incentive_rewards'] == '0.00'
        data = get(world.finance[0], f'/api/v1/payments/admin/drivers/{referrer.id}/ledger/')
        line = [e for e in data['entries']['results'] if e['purpose'] == 'referral_reward'][0]
        assert (line['kind'], line['commission'], line['tax']) == (
            'referral_reward', 'none', 'not configured')


class TestBlocks:
    """Each of the three blocks stops the reward and raises a fraud-queue item."""

    def blocked(self, referrer, referee, world, trips=2):
        attach(referee, code_of(referrer))
        referee_trips(world, referee, trips)
        return Referral.objects.get(referee=referee)

    def assert_blocked(self, referral, signal, referrer, referee):
        assert referral.status == BLOCKED and referral.blocked_signals == [signal]
        assert rider_balance(referrer) == 0 and rider_balance(referee) == 0
        assert not LedgerEntry.objects.filter(kind=REFERRAL_REWARD).exists()
        flag = RiskFlag.objects.get(kind='referral_abuse', source_ref=f'referral:{referral.pk}')
        assert flag.status == 'open' and flag.subject_id == referrer.id
        assert flag.evidence['signals'] == [signal]
        assert flag.anchor_node is not None

    def test_the_same_phone_number(self, world, campaign):
        referrer = make_rider('S1', '91')
        referee = make_rider('S2', '91')
        referee.phone_number = '+91 ' + referrer.phone_number[-10:]   # the same number, spaced
        referee.save(update_fields=['phone_number'])
        referral = self.blocked(referrer, referee, world)
        self.assert_blocked(referral, 'same_phone', referrer, referee)

    def test_an_emergency_contact_that_is_the_other_persons_number(self, world, campaign):
        referrer, referee = make_rider('S3', '91'), make_rider('S4', '91')
        referee.emergency_contact = referrer.phone_number
        referee.save(update_fields=['emergency_contact'])
        referral = self.blocked(referrer, referee, world)
        self.assert_blocked(referral, 'same_phone', referrer, referee)

    def test_the_same_device(self, world, campaign):
        referrer, referee = make_rider('S5', '91'), make_rider('S6', '91')
        for user in (referrer, referee):
            user.fcm_token = 'push-token-shared-handset'
            user.save(update_fields=['fcm_token'])
        referral = self.blocked(referrer, referee, world)
        self.assert_blocked(referral, 'same_device', referrer, referee)

    def test_the_device_id_the_app_sends_is_compared_with_the_referrers_push_token(
            self, world, campaign):
        referrer, referee = make_rider('S7', '91'), make_rider('S8', '91')
        referrer.fcm_token = 'handset-4242'
        referrer.save(update_fields=['fcm_token'])
        attach(referee, code_of(referrer), device_id='handset-4242')
        referee_trips(world, referee, 2)
        referral = Referral.objects.get(referee=referee)
        self.assert_blocked(referral, 'same_device', referrer, referee)

    def test_the_same_payout_instrument_between_two_drivers(self, world, driver_campaign):
        referrer = make_driver(world.geo, 'IN-TG-HYD', 'I1', currency='INR', calling='91')
        referee = make_driver(world.geo, 'IN-TG-HYD', 'I2', currency='INR', calling='91')
        referee.upi_id = referrer.upi_id.upper()                      # the same UPI id
        referee.save(update_fields=['upi_id'])
        attach(referee.user_id, code_of(referrer.user_id))
        rider = make_rider('P', '91')
        for _ in range(2):
            referrals.on_trip_completed(completed_trip(world, rider, referee))
        referral = Referral.objects.get(referee=referee.user_id)
        assert referral.status == BLOCKED and referral.blocked_signals == ['same_instrument']
        assert not WalletTransaction.objects.filter(purpose='referral_reward').exists()
        flag = RiskFlag.objects.get(source_ref=f'referral:{referral.pk}')
        assert flag.evidence['signals'] == ['same_instrument'] and flag.evidence['role'] == 'driver'

    def test_a_bank_account_counts_as_an_instrument_too(self, world, driver_campaign):
        from servers.driver.models import DriverBankAccount

        referrer = make_driver(world.geo, 'IN-TG-HYD', 'B1', currency='INR', calling='91')
        referee = make_driver(world.geo, 'IN-TG-HYD', 'B2', currency='INR', calling='91')
        referee.upi_id = 'someone.else@okaxis'
        referee.save(update_fields=['upi_id'])
        for driver in (referrer, referee):
            DriverBankAccount.objects.create(driver=driver, bank_name='Test Bank',
                                             account_number='000111222', ifsc_code='TEST0000001')
        assert referrals.abuse_signals(referrer.user_id, referee.user_id) == ['same_instrument']

    def test_a_rider_blocked_referral_also_flags_the_rider_for_promo_abuse(self, world, campaign):
        referrer, referee = make_rider('S9', '91'), make_rider('S10', '91')
        referee.emergency_contact = referrer.phone_number
        referee.save(update_fields=['emergency_contact'])
        self.blocked(referrer, referee, world)
        flag = RiderFlag.objects.get(rider__user_id=referrer, reason='promo_abuse')
        assert flag.cleared_at is None and 'same_phone' in flag.note

    def test_evidence_carries_no_raw_personal_data(self, world, campaign):
        referrer, referee = make_rider('S11', '91'), make_rider('S12', '91')
        referee.fcm_token = referrer.fcm_token = 'push-token-secret-value'
        referee.emergency_contact = referrer.phone_number
        referee.save(update_fields=['fcm_token', 'emergency_contact'])
        referrer.save(update_fields=['fcm_token'])
        referral = self.blocked(referrer, referee, world)
        flag = RiskFlag.objects.get(source_ref=f'referral:{referral.pk}')
        text = str(flag.evidence)
        assert set(flag.evidence['signals']) == {'same_device', 'same_phone'}
        for secret in (referrer.phone_number, referrer.phone_number[-10:],
                       'push-token-secret-value'):
            assert secret not in text

    def test_a_blocked_referral_is_not_paid_later_and_counts_nothing_more(self, world, campaign):
        referrer, referee = make_rider('S13', '91'), make_rider('S14', '91')
        referee.emergency_contact = referrer.phone_number
        referee.save(update_fields=['emergency_contact'])
        referral = self.blocked(referrer, referee, world)
        referee_trips(world, referee, 2, start=10)
        referral.refresh_from_db()
        assert referral.status == BLOCKED
        assert referrals.retry_qualified() == 0
        assert RiskFlag.objects.filter(kind='referral_abuse').count() == 1

    def test_the_app_is_told_only_that_a_reward_is_under_review(self, world, campaign):
        referrer, referee = make_rider('S15', '91'), make_rider('S16', '91')
        referee.emergency_contact = referrer.phone_number
        referee.save(update_fields=['emergency_contact'])
        self.blocked(referrer, referee, world)
        mine = client_for(referrer).get(f'{API}/referrals/mine/').json()['data']
        assert mine['referrals'][0]['note'] == 'under review'
        assert 'same_phone' not in str(mine)


class TestTheBudgetCap:
    def test_an_unaffordable_reward_is_not_paid_and_is_paid_after_an_approved_increase(self, world):
        campaign = launch(world.mkt[0], referral_payload('rider', budget_minor=15_000))
        first, second = pair(world), (make_rider('R3', '91'), make_rider('R4', '91'))
        attach(second[1], code_of(second[0]))
        referee_trips(world, first[1], 2)
        referee_trips(world, second[1], 2)
        paid, waiting = Referral.objects.get(referee=first[1]), Referral.objects.get(
            referee=second[1])
        assert paid.status == REWARDED
        assert waiting.status == QUALIFIED
        assert rider_balance(second[0]) == 0 and rider_balance(second[1]) == 0
        campaign.refresh_from_db()
        assert campaign.state == 'exhausted' and campaign.spent_minor == 15_000
        assert referrals.retry_qualified() == 0           # still no budget: still waiting

        sent = post(world.mkt[0], f'{ADM}/campaigns/{campaign.id}/budget-increase/',
                    {'budget_minor': 60_000, 'reason': 'more referrals'}, expect=202)
        approve_latest(body_of(sent)['approval_request']['id'])
        from servers.promotions.workflow import sweep
        assert sweep()['referrals_paid'] == 1
        waiting.refresh_from_db()
        assert waiting.status == REWARDED
        assert rider_balance(second[0]) == 10_000 and rider_balance(second[1]) == 5_000

    def test_a_wallet_at_its_balance_cap_leaves_the_referral_qualified_and_spends_nothing(
            self, world, campaign, settings):
        settings.RIDER_CREDIT_BALANCE_CAP = '50.00'
        referrer, referee = pair(world)
        referee_trips(world, referee, 2)
        referral = Referral.objects.get(referee=referee)
        assert referral.status == QUALIFIED                 # 100.00 does not fit a 50.00 cap
        campaign.refresh_from_db()
        assert campaign.spent_minor == 0 and not LedgerEntry.objects.exists()


class TestConsole:
    def test_the_activity_shows_masked_people_and_the_blocked_signal(self, world, campaign):
        referrer, referee = make_rider('K1', '91'), make_rider('K2', '91')
        referee.emergency_contact = referrer.phone_number
        referee.save(update_fields=['emergency_contact'])
        attach(referee, code_of(referrer))
        referee_trips(world, referee, 2)
        data = get(world.mkt[0], f'{ADM}/campaigns/{campaign.id}/activity/')
        row = data['results'][0]
        assert data['activity'] == 'referrals'
        assert row['referrer'] == f'rider #{referrer.id}' and row['referee'] == f'rider #{referee.id}'
        assert row['status'] == 'blocked' and row['blocked_signals'] == ['same_phone']
        assert (row['trips_completed'], row['required_trips']) == (2, 2)

    def test_the_fraud_list_is_read_only_and_scoped(self, world, campaign):
        referrer, referee = make_rider('K3', '91'), make_rider('K4', '91')
        referee.emergency_contact = referrer.phone_number
        referee.save(update_fields=['emergency_contact'])
        attach(referee, code_of(referrer))
        referee_trips(world, referee, 2)
        rows = get(world.mkt[0], '/api/v1/fraud/admin/flags/')['results']
        assert len(rows) == 1
        flag = rows[0]
        assert flag['kind'] == 'referral_abuse' and flag['status'] == 'open'
        assert flag['subject']['ref'] == f'rider #{referrer.id}'
        assert flag['evidence']['signals'] == ['same_phone']
        # This rider has no home city and no trip: they sit at the country, which a Hyderabad
        # operator's scope does not cover.
        assert get(world.ops[0], '/api/v1/fraud/admin/flags/')['count'] == 0
        assert get(world.mkt[0], '/api/v1/fraud/admin/flags/?kind=referral_abuse')['count'] == 1
        assert get(world.mkt[0], '/api/v1/fraud/admin/flags/?status=dismissed')['count'] == 0
        for who in ('finance', 'analyst', 'support'):
            assert getattr(world, who)[0].get('/api/v1/fraud/admin/flags/').status_code == 403
        assert get(world.mkt_us[0], '/api/v1/fraud/admin/flags/')['results'] == []
        for method in ('post', 'put', 'patch', 'delete'):
            response = getattr(world.mkt[0], method)('/api/v1/fraud/admin/flags/', {}, format='json')
            assert response.status_code == 405

    def test_one_blocked_referral_raises_one_flag(self, world, campaign):
        referrer, referee = make_rider('K5', '91'), make_rider('K6', '91')
        referee.emergency_contact = referrer.phone_number
        referee.save(update_fields=['emergency_contact'])
        attach(referee, code_of(referrer))
        referee_trips(world, referee, 3)
        assert RiskFlag.objects.count() == 1 and Campaign.objects.filter(pk=campaign.pk).exists()
        create  # noqa: B018 - imported for the other test modules' shared surface
