"""The admin trip page's fare card says who paid what (owner ruling, AUDIT 27.74).

"Fare ₹159.72 · Promo −₹20 (paid by SaaradhiGo) · Rider paid ₹139.72", the cash the driver collected
on a cash trip, and the same treatment for referral rewards and operator early ends.
"""

from decimal import Decimal as D

import pytest

from promotions_support import (  # noqa: F401
    block_the_network, completed_trip, geo, hyd_driver, launch, make_rider, promo_payload, world,
)
from servers.payments.models import Payment
from servers.ride.fare_card import fare_card

pytestmark = pytest.mark.django_db


def _kinds(card):
    return [(line['kind'], line['amount_minor']) for line in card['lines']]


@pytest.fixture
def promo(world):
    return launch(world.mkt[0], promo_payload())


def _promo_trip(world, method, tag, fare='160.00'):
    from servers.ride.promos import redeem_promo

    trip = completed_trip(world, make_rider(tag, '91'), hyd_driver(world, tag), fare=fare,
                          method=method)
    assert redeem_promo('WELCOME50', trip.user_id, trip, D(fare), zone=world.hyd).ok
    return trip


class TestPromoTrips:
    def test_a_promo_cash_trip_reads_as_the_money_moved(self, world, promo):
        trip = _promo_trip(world, 'cash', 'C')
        Payment.objects.create(trip_id=trip, user_id=trip.user_id, amount_minor=11_000,
                               method='cash', status='completed')
        card = fare_card(trip)
        assert _kinds(card) == [('fare', 16_000), ('promo', -5_000), ('rider_paid', 11_000)]
        promo_line = card['lines'][1]
        assert promo_line['funded_by'] == 'SaaradhiGo' and promo_line['note'] == 'paid by SaaradhiGo'
        assert card['cash'] == {'collected': True, 'amount_minor': 11_000,
                                'label': 'Driver collected in cash',
                                'platform_pays_driver_minor': 5_000}
        assert [line['kind'] for line in card['summary']] == ['fare', 'promo', 'rider_paid']

    def test_cash_not_yet_confirmed_is_still_to_collect(self, world, promo):
        trip = _promo_trip(world, 'cash', 'P')
        trip.payment_status = 'pending'
        trip.save(update_fields=['payment_status'])
        card = fare_card(trip)
        assert card['cash']['collected'] is False
        assert card['cash']['label'] == 'Driver to collect in cash'
        assert _kinds(card)[-1] == ('rider_owes', 11_000)

    def test_an_online_promo_trip_has_no_cash_line(self, world, promo):
        trip = _promo_trip(world, 'online', 'O')
        Payment.objects.create(trip_id=trip, user_id=trip.user_id, amount_minor=11_000,
                               method='online', status='completed')
        card = fare_card(trip)
        assert card['cash'] is None
        assert card['lines'][-1] == {'kind': 'rider_paid', 'label': 'Rider paid',
                                     'amount_minor': 11_000, 'method': 'online'}


class TestOtherTrips:
    def test_a_plain_wallet_trip(self, world):
        trip = completed_trip(world, make_rider('W', '91'), hyd_driver(world, 'W'), method='wallet')
        Payment.objects.create(trip_id=trip, user_id=trip.user_id, amount_minor=20_000,
                               method='wallet', status='completed')
        assert _kinds(fare_card(trip)) == [('fare', 20_000), ('rider_paid', 20_000)]

    def test_a_refund_is_its_own_line(self, world):
        from servers.ride.models import TripMoneyAction

        trip = completed_trip(world, make_rider('R', '91'), hyd_driver(world, 'R'))
        Payment.objects.create(trip_id=trip, user_id=trip.user_id, amount_minor=20_000,
                               method='online', status='completed')
        TripMoneyAction.objects.create(trip=trip, kind=TripMoneyAction.REFUND, amount_minor=5_000,
                                       currency='INR', method=TripMoneyAction.GATEWAY,
                                       status=TripMoneyAction.COMPLETED, reason='Support')
        assert _kinds(fare_card(trip))[-1] == ('refunded', -5_000)

    def test_the_referral_rewards_this_trip_earned(self, world):
        from servers.promotions.models import REWARDED, Referral, ReferralTrip

        referrer, rider = make_rider('A', '91'), make_rider('B', '91')
        trip = completed_trip(world, rider, hyd_driver(world, 'B'))
        campaign = launch(world.mkt[0], __import__('promotions_support').referral_payload())
        referral = Referral.objects.create(
            campaign=campaign, referrer=referrer, referee=rider, code_used='A1B2C3',
            status=REWARDED, trips_completed=1, referrer_reward_minor=5_000,
            referee_reward_minor=2_500, currency='INR')
        ReferralTrip.objects.create(referral=referral, trip=trip)
        rewards = fare_card(trip)['referral_rewards']
        assert [(r['to'], r['amount_minor'], r['funded_by']) for r in rewards] == [
            ('rider', 2_500, 'SaaradhiGo'), ('referrer', 5_000, 'SaaradhiGo')]

    def test_an_operator_early_end(self, world):
        from servers.ride.models import FarePricing

        trip = completed_trip(world, make_rider('E', '91'), hyd_driver(world, 'E'), fare='100.00',
                              method='cash')
        trip.end_rule, trip.end_reason_code = 'standard', 'driver_unresponsive'
        trip.save(update_fields=['end_rule', 'end_reason_code'])
        FarePricing.objects.create(
            trip_id=trip, base_fare=D('60'), distance_fare=D('30'), time_fare=D('7.89'),
            surge_multiplier=D('1.00'), total_fare=D('97.89'),
            snapshot_type=FarePricing.SNAPSHOT_OPERATOR_END, min_fare_applied=True,
            actual_distance_km=D('0.93'))
        early = fare_card(trip)['early_end']
        assert early['rule'] == 'standard' and early['min_fare_applied'] is True
        assert early['distance_km'] == '0.93' and early['funded_by'] is None

    def test_a_platform_funded_end_says_who_paid_the_driver(self, world):
        trip = completed_trip(world, make_rider('F', '91'), hyd_driver(world, 'F'), fare='0.00')
        trip.end_rule, trip.platform_funded_minor = 'platform_funded', 8_500
        trip.save(update_fields=['end_rule', 'platform_funded_minor'])
        early = fare_card(trip)['early_end']
        assert (early['platform_paid_driver_minor'], early['funded_by']) == (8_500, 'SaaradhiGo')


def test_the_admin_trip_detail_carries_the_card(world, promo):
    from test_finance_actions import checker

    trip = _promo_trip(world, 'cash', 'D')
    client, _ = checker()
    data = client.get(f'/api/v1/ride/admin/trips/{trip.id}/').json()['data']
    assert [line['kind'] for line in data['fare_card']['summary']] == ['fare', 'promo', 'rider_paid']
