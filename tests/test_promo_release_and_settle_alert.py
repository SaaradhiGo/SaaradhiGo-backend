"""Released promo budget revives a campaign; a failed promo settlement alerts Finance (AUDIT 27.78).

Second independent review, low 1 and low 2 (owner: fix both; low 2 gets a Finance alert as with
other money failures).
"""

from decimal import Decimal as D

import pytest

from promotions_support import (  # noqa: F401
    block_the_network, completed_trip, geo, hyd_driver, launch, make_rider, promo_payload, settle,
    world,
)
from servers.command_centre.models import FINANCE_ALERT_KINDS, OperatorAlert
from servers.payments.models import TripSettlement
from servers.promotions import budget
from servers.promotions.models import EXHAUSTED, LIVE

pytestmark = pytest.mark.django_db


@pytest.fixture
def promo(world):
    return launch(world.mkt[0], promo_payload())


class TestReleasedBudgetIsUsableAgain:
    def test_an_exhausted_campaign_is_live_again_when_released_money_can_fund_a_discount(
            self, promo):
        unit = budget.min_viable_minor(promo)
        while True:
            try:
                budget.reserve(promo.id, unit)
            except budget.Refused:
                break
        promo.refresh_from_db()
        assert promo.state == EXHAUSTED
        budget.release(promo.id, unit)
        promo.refresh_from_db()
        assert promo.state == LIVE and promo.exhausted_at is None

    def test_too_little_released_keeps_it_exhausted(self, promo):
        unit = budget.min_viable_minor(promo)
        while True:
            try:
                budget.reserve(promo.id, unit)
            except budget.Refused:
                break
        budget.release(promo.id, 1)
        promo.refresh_from_db()
        remaining = promo.budget_minor - promo.committed_minor - promo.spent_minor
        assert (promo.state == EXHAUSTED) is (remaining < unit)

    def test_a_cancelled_trip_gives_the_code_its_redemption_back(self, world, promo):
        from servers.promotions.rider_promos import release_for_trip
        from servers.ride.models import PromoCode
        from servers.ride.promos import redeem_promo

        trip = completed_trip(world, make_rider('R', '91'), hyd_driver(world, 'R'))
        assert redeem_promo('WELCOME50', trip.user_id, trip, D('200.00'), zone=world.hyd).ok
        code = PromoCode.objects.get(code='WELCOME50')
        assert code.redemption_count == 1
        release_for_trip(trip)
        release_for_trip(trip)                      # idempotent: never below zero, never twice
        code.refresh_from_db()
        assert code.redemption_count == 0


class TestAFailedPromoSettlementAlertsFinance:
    def test_the_cash_driver_short_is_a_critical_finance_alert(self, world, promo, monkeypatch):
        from servers.ride.promos import redeem_promo

        trip = completed_trip(world, make_rider('S', '91'), hyd_driver(world, 'S'), method='cash')
        assert redeem_promo('WELCOME50', trip.user_id, trip, D('200.00'), zone=world.hyd).ok

        def broken(*a, **k):
            raise RuntimeError('ledger unavailable')

        monkeypatch.setattr('servers.promotions.rider_promos.settle_trip', broken)
        settle(trip)
        assert TripSettlement.objects.filter(trip=trip).exists()      # the settlement stands
        alert = OperatorAlert.objects.get(kind='finance.promo_settlement_failed')
        assert alert.kind in FINANCE_ALERT_KINDS and alert.severity == 'critical'
        assert alert.payload['trip_id'] == trip.id and 'short the discount' in alert.title

    def test_a_working_settlement_raises_nothing(self, world, promo):
        from servers.ride.promos import redeem_promo

        trip = completed_trip(world, make_rider('W', '91'), hyd_driver(world, 'W'), method='cash')
        assert redeem_promo('WELCOME50', trip.user_id, trip, D('200.00'), zone=world.hyd).ok
        settle(trip)
        assert not OperatorAlert.objects.filter(kind='finance.promo_settlement_failed').exists()
