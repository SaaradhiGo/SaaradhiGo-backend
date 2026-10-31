"""Module 12 must not break what already works: PromoCode, PromoRedemption, IncentiveQuest.

  * The existing apply-promo endpoint and the existing driver incentives endpoint answer exactly as
    before, for codes and quests that existed before the module.
  * The additive data migration wraps every existing row in a campaign (no budget, no checker, no
    change to the old row) and links existing redemptions; it reverses cleanly.
  * A code created outside the module is wrapped on first redemption, so it still redeems.
"""

import importlib
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from promotions_support import (  # noqa: F401
    block_the_network, client_for, completed_trip, geo, hyd_driver, make_rider, settle, world,
)
from servers.driver.models import IncentiveQuest
from servers.promotions.models import Campaign
from servers.ride.models import PromoCode, PromoRedemption
from servers.ride.promos import apply_promo, redeem_promo

pytestmark = [pytest.mark.django_db]

D = Decimal


def old_code(code='OLDFLAT', **over):
    fields = dict(code=code, discount_type='flat', discount_flat_minor=2_500, currency='INR',
                  valid_from=timezone.now() - timedelta(days=1),
                  valid_to=timezone.now() + timedelta(days=5), max_per_user_redemptions=2)
    fields.update(over)
    return PromoCode.objects.create(**fields)


class TestTheExistingApplyEndpoint:
    def test_a_code_that_existed_before_still_applies(self, world):
        old_code()
        rider = make_rider('E1', '91')
        response = client_for(rider).post(
            '/api/v1/ride/promo/apply/',
            {'code': 'oldflat', 'fare': '200.00', 'pickup_lat': 17.445, 'pickup_long': 78.38},
            format='json')
        assert response.status_code == 200, response.content
        body = response.json()['data']
        assert (body['ok'], body['code'], body['discount_amount'], body['final_fare']) == (
            True, 'OLDFLAT', '25.00', '175.00')

    def test_the_old_refusals_are_unchanged(self, world):
        rider = make_rider('E2', '91')
        client = client_for(rider)
        for payload, code in (({'code': 'NOPE', 'fare': '200.00'}, 'PROMO_NOT_FOUND'),
                              ({'code': '', 'fare': '200.00'}, 'MISSING_FIELDS'),
                              ({'code': 'X', 'fare': 'abc'}, 'INVALID_TYPE')):
            response = client.post('/api/v1/ride/promo/apply/', payload, format='json')
            assert response.status_code == 400 and response.json()['error']['code'] == code

    def test_an_unwrapped_code_redeems_and_is_wrapped_on_the_spot(self, world):
        promo = old_code()
        rider = make_rider('E3', '91')
        trip = completed_trip(world, rider, hyd_driver(world, 'E3'))
        result = redeem_promo('OLDFLAT', rider, trip, D('200.00'), zone=world.hyd)
        assert result.ok and result.discount_amount == D('25.00')
        campaign = Campaign.objects.get(promo_code=promo)
        assert campaign.is_legacy and campaign.budget_minor is None and campaign.state == 'live'
        redemption = PromoRedemption.objects.get(trip=trip)
        assert redemption.campaign_id == campaign.id and redemption.state == 'committed'
        settle(trip)
        campaign.refresh_from_db()
        assert (campaign.committed_minor, campaign.spent_minor) == (0, 2_500)

    def test_the_global_counters_and_limits_still_apply(self, world):
        promo = old_code(max_total_redemptions=1, max_per_user_redemptions=1)
        r1, r2 = make_rider('E4', '91'), make_rider('E5', '91')
        driver = hyd_driver(world, 'E4')
        assert redeem_promo('OLDFLAT', r1, completed_trip(world, r1, driver), D('200.00'),
                            zone=world.hyd).ok
        refused = redeem_promo('OLDFLAT', r2, completed_trip(world, r2, driver), D('200.00'),
                               zone=world.hyd)
        assert refused.reason == 'PROMO_LIMIT_REACHED'
        promo.refresh_from_db()
        assert promo.redemption_count == 1

    def test_a_legacy_code_with_no_budget_never_exhausts(self, world):
        old_code()
        rider = make_rider('E6', '91')
        driver = hyd_driver(world, 'E6')
        for _ in range(2):
            assert redeem_promo('OLDFLAT', rider, completed_trip(world, rider, driver),
                                D('200.00'), zone=world.hyd).ok
        assert Campaign.objects.get(promo_code__code='OLDFLAT').state == 'live'


class TestTheExistingDriverEndpoint:
    def test_the_quest_list_and_progress_are_unchanged(self, world):
        IncentiveQuest.objects.create(
            title='Old quest', description='Ten trips', target_trips=2, reward_amount_minor=10_000,
            currency='INR', start_date=timezone.now() - timedelta(days=1),
            end_date=timezone.now() + timedelta(days=1))
        IncentiveQuest.objects.create(
            title='Inactive', target_trips=2, reward_amount_minor=10_000, currency='INR',
            is_active=False, start_date=timezone.now() - timedelta(days=1),
            end_date=timezone.now() + timedelta(days=1))
        driver = hyd_driver(world, 'Q1')
        completed_trip(world, make_rider('q1', '91'), driver)
        completed_trip(world, make_rider('q2', '91'), driver)
        client = client_for(driver.user_id)
        response = client.get('/api/v1/driver/driver/incentives/')
        assert response.status_code == 200, response.content
        rows = response.json()['data']
        assert [r['title'] for r in rows] == ['Old quest']
        row = rows[0]
        assert (row['target_trips'], row['progress_trips'], row['is_completed']) == (2, 2, True)
        assert row['reward_amount'] == '100.00'
        assert set(row) >= {'id', 'title', 'description', 'target_trips', 'reward_amount',
                            'start_date', 'end_date', 'progress_trips', 'is_completed'}


class TestTheMigration:
    def forward(self):
        module = importlib.import_module('servers.promotions.migrations.0002_wrap_existing_rows')
        from django.apps import apps

        module.forward(apps, None)
        return module

    def test_every_existing_row_is_wrapped_and_nothing_old_changes(self, world):
        from servers.pricing.models import ServiceZone

        zoned = old_code('ZONED', zone=ServiceZone.objects.get(code='IN-TG-HYD'))
        plain = old_code('PLAIN', discount_type='percent', discount_flat_minor=None,
                         discount_percent=D('10.00'), max_discount_amount_minor=3_000)
        past = old_code('PAST', valid_from=timezone.now() - timedelta(days=9),
                        valid_to=timezone.now() - timedelta(days=1))
        off = old_code('OFF', is_active=False)
        quest = IncentiveQuest.objects.create(
            title='Q', target_trips=5, reward_amount_minor=20_000, currency='INR',
            start_date=timezone.now() - timedelta(days=1),
            end_date=timezone.now() + timedelta(days=1))
        rider = make_rider('M1', '91')
        trip = completed_trip(world, rider, hyd_driver(world, 'M1'))
        redemption = PromoRedemption.objects.create(
            promo=plain, user=rider, trip=trip, currency='INR', discount_amount_minor=2_000)
        before = list(PromoCode.objects.order_by('id').values())

        self.forward()
        self.forward()                                   # idempotent

        assert list(PromoCode.objects.order_by('id').values()) == before      # nothing old changed
        assert Campaign.objects.filter(is_legacy=True).count() == 5
        by_code = {c.promo_code.code: c for c in Campaign.objects.filter(promo_code__isnull=False)}
        assert by_code['ZONED'].geo_node.code == 'IN-TG-HYD'
        assert by_code['PLAIN'].geo_node.code == 'IN' and by_code['PLAIN'].country == 'IN'
        assert (by_code['PLAIN'].state, by_code['PAST'].state, by_code['OFF'].state) == (
            'live', 'ended', 'paused')
        assert by_code['PLAIN'].config['discount_percent'] == '10.00'
        assert all(c.budget_minor is None and c.approval_request_id is None
                   for c in by_code.values())
        wrapped = Campaign.objects.get(quest=quest)
        assert (wrapped.kind, wrapped.state, wrapped.config['target_trips']) == (
            'driver_incentive', 'live', 5)
        redemption.refresh_from_db()
        assert redemption.campaign_id == by_code['PLAIN'].id and redemption.state == 'legacy'
        assert zoned and past and off

    def test_it_reverses_and_comes_back(self, world):
        old_code('REVERSE')
        self.forward()
        assert Campaign.objects.filter(is_legacy=True).count() == 1
        module = importlib.import_module('servers.promotions.migrations.0002_wrap_existing_rows')
        from django.apps import apps

        module.backward(apps, None)
        assert Campaign.objects.count() == 0 and PromoCode.objects.filter(code='REVERSE').exists()
        self.forward()
        assert Campaign.objects.filter(is_legacy=True).count() == 1
