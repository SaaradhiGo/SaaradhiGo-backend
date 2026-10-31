"""Module 12: a rider promo is PLATFORM-FUNDED. The driver's economics do not move.

THE RULING (owner, 2026-10-31, AUDIT 27.51)
-------------------------------------------
Driver earnings and commission are calculated on the FULL fare. A promo never reduces what a driver
earns; the rider pays the fare less the discount and the platform absorbs the difference, recorded as
a `promo_discount_platform_funded` ledger entry.

WHAT WAS CHECKED FIRST (and is stated in the module report)
-----------------------------------------------------------
Today's settlement (`driver.utils.credit_driver_wallet`) reads `trip.final_fare or
trip.estimated_fare`, and `PromoRedemption`/`redeem_promo` were not wired into booking at all (no
caller). The OLD design, in `base.money.charged_fare`'s own words, was that a promo would write the
DISCOUNTED amount into `final_fare` - and settlement would then have computed the driver's gross and
commission on the discounted fare. That path is closed: `final_fare` is never written by a promo, and
`TestDriverEconomicsAreIdentical` pins it.
"""

from decimal import Decimal
from unittest import mock

import pytest

from promotions_support import (  # noqa: F401
    ADM, block_the_network, client_for, completed_trip, geo, get, hyd_driver, launch, make_rider,
    post, promo_payload, settle, world,
)
from servers.payments.models import Payment, TripSettlement
from servers.promotions import budget, rider_promos
from servers.promotions.models import DISCOUNT_FUNDED, LedgerEntry
from servers.ride.models import PromoRedemption
from servers.ride.promos import apply_promo, redeem_promo
from servers.rider.models import Wallet, WalletTransaction

pytestmark = [pytest.mark.django_db]

D = Decimal


def book(world, rider, driver, *, fare='200.00', method='online', code='WELCOME50'):
    """A trip that held a promo from booking (the reservation `rider_promos.book` makes)."""
    trip = completed_trip(world, rider, driver, fare=fare, method=method)
    result = redeem_promo(code, rider, trip, D(fare), zone=world.hyd)
    assert result.ok, result.reason
    return trip, result


def driver_wallet(driver):
    return Wallet.objects.get(user_id=driver.user_id, scope=Wallet.SCOPE_DRIVER)


@pytest.fixture
def promo(world):
    return launch(world.mkt[0], promo_payload())


class TestDriverEconomicsAreIdentical:
    """THE TEST THE RULING NEEDS: with and without a promo, the driver is paid the same."""

    @pytest.mark.parametrize('method', ['online', 'cash'])
    def test_gross_commission_and_net_are_the_same_with_and_without_a_promo(
            self, world, promo, method):
        d_plain, d_promo = hyd_driver(world, 'P1'), hyd_driver(world, 'P2')
        plain = completed_trip(world, make_rider('P1', '91'), d_plain, method=method)
        promoted, result = book(world, make_rider('P2', '91'), d_promo, method=method)
        assert result.discount_amount == D('50.00') and result.final_fare == D('150.00')
        settle(plain)
        settle(promoted)

        a, b = TripSettlement.objects.get(trip=plain), TripSettlement.objects.get(trip=promoted)
        assert (a.gross_fare_minor, a.commission_percent, a.commission_amount_minor,
                a.driver_net_minor) == (20_000, D('15.00'), 3_000, 17_000)
        assert (b.gross_fare_minor, b.commission_percent, b.commission_amount_minor,
                b.driver_net_minor) == (a.gross_fare_minor, a.commission_percent,
                                        a.commission_amount_minor, a.driver_net_minor)

        main = 'trip_earnings' if method == 'online' else 'trip_commission'
        row_a = WalletTransaction.objects.get(user_id=d_plain.user_id, purpose=main)
        row_b = WalletTransaction.objects.get(user_id=d_promo.user_id, purpose=main)
        assert row_a.amount_minor == row_b.amount_minor

    def test_an_online_driver_balance_is_identical(self, world, promo):
        d_plain, d_promo = hyd_driver(world, 'O1'), hyd_driver(world, 'O2')
        plain = completed_trip(world, make_rider('O1', '91'), d_plain)
        promoted, _ = book(world, make_rider('O2', '91'), d_promo)
        settle(plain)
        settle(promoted)
        assert driver_wallet(d_plain).balance_minor == driver_wallet(d_promo).balance_minor == 17_000
        # Nothing was credited for the discount: the platform collected less from the rider.
        assert not WalletTransaction.objects.filter(
            purpose='promo_discount_platform_funded').exists()

    def test_a_cash_driver_is_paid_the_discount_they_did_not_collect(self, world, promo):
        """In cash the driver holds 150, not 200. The platform pays the missing 50, so the driver
        ends exactly where they would without the promo: cash + wallet is the same."""
        d_plain, d_promo = hyd_driver(world, 'C1'), hyd_driver(world, 'C2')
        plain = completed_trip(world, make_rider('C1', '91'), d_plain, method='cash')
        promoted, _ = book(world, make_rider('C2', '91'), d_promo, method='cash')
        settle(plain)
        settle(promoted)

        cash_plain, cash_promo = 20_000, rider_promos.payable_minor(promoted)
        assert cash_promo == 15_000
        total_plain = cash_plain + driver_wallet(d_plain).balance_minor
        total_promo = cash_promo + driver_wallet(d_promo).balance_minor
        assert total_plain == total_promo == 17_000
        credit = WalletTransaction.objects.get(purpose='promo_discount_platform_funded')
        assert (credit.user_id_id, credit.amount_minor, credit.txn_type) == (
            d_promo.user_id_id, 5_000, 'credit')
        assert credit.reference_id == f'TRIP_{promoted.id}'

    def test_the_driver_never_sees_the_trip_fare_change(self, world, promo):
        trip, _ = book(world, make_rider('F', '91'), hyd_driver(world, 'F'))
        trip.refresh_from_db()
        assert trip.estimated_fare_minor == trip.final_fare_minor == 20_000

    def test_settling_twice_counts_the_discount_once(self, world, promo):
        trip, _ = book(world, make_rider('T', '91'), hyd_driver(world, 'T'), method='cash')
        settle(trip)
        settle(trip)
        assert LedgerEntry.objects.filter(kind=DISCOUNT_FUNDED).count() == 1
        assert WalletTransaction.objects.filter(
            purpose='promo_discount_platform_funded').count() == 1
        promo.refresh_from_db()
        assert (promo.committed_minor, promo.spent_minor) == (0, 5_000)

    def test_the_platform_funded_entry_records_the_discount(self, world, promo):
        rider = make_rider('L', '91')
        trip, _ = book(world, rider, hyd_driver(world, 'L'))
        settle(trip)
        entry = LedgerEntry.objects.get(kind=DISCOUNT_FUNDED)
        assert (entry.amount_minor, entry.currency, entry.trip_id, entry.user_id,
                entry.campaign_id) == (5_000, 'INR', trip.id, rider.id, promo.id)
        assert entry.wallet_transaction_id is None          # online: no driver balance moved
        redemption = PromoRedemption.objects.get(trip=trip)
        assert redemption.state == 'settled' and redemption.settled_at
        promo.refresh_from_db()
        assert (promo.committed_minor, promo.spent_minor) == (0, 5_000)


class TestWhatTheRiderPays:
    def test_the_payable_is_the_fare_less_the_discount(self, world, promo):
        trip, _ = book(world, make_rider('R1', '91'), hyd_driver(world, 'R1'))
        assert rider_promos.fare_minor(trip) == 20_000
        assert rider_promos.discount_minor(trip) == 5_000
        assert rider_promos.payable_minor(trip) == 15_000
        assert rider_promos.payable(trip) == D('150.00')

    def test_a_trip_with_no_promo_pays_the_fare(self, world):
        trip = completed_trip(world, make_rider('R2', '91'), hyd_driver(world, 'R2'))
        assert rider_promos.payable_minor(trip) == 20_000

    def test_the_payment_row_is_for_what_the_rider_pays(self, world, promo):
        from servers.ride.utils import create_payment_on_complete

        trip, _ = book(world, make_rider('R3', '91'), hyd_driver(world, 'R3'), method='cash')
        create_payment_on_complete(trip)
        payment = Payment.objects.get(trip_id=trip)
        assert payment.amount_minor == 15_000
        # ... while settlement ran on the full fare.
        assert TripSettlement.objects.get(trip=trip).gross_fare_minor == 20_000

    def test_the_online_order_is_for_what_the_rider_pays(self, world, promo):
        rider = make_rider('R4', '91')
        trip, _ = book(world, rider, hyd_driver(world, 'R4'))
        trip.payment_status = 'pending'
        trip.save(update_fields=['payment_status'])
        with mock.patch('servers.payments.views.get_payment_gateway') as gateway:
            gateway.return_value.supported_currencies.return_value = ('INR',)
            gateway.return_value.get_name.return_value = 'cashfree'
            gateway.return_value.create_order.return_value = {
                'order_id': 'order_promo_1', 'payment_session_id': 'session_1'}
            response = client_for(rider).post('/api/v1/payments/create-order/',
                                              {'trip_id': trip.id}, format='json')
        assert response.status_code == 201, response.content
        assert gateway.return_value.create_order.call_args.kwargs['amount_minor'] == 15_000
        assert Payment.objects.get(trip_id=trip).amount_minor == 15_000

    def test_the_trip_detail_tells_both_apps_what_the_rider_pays(self, world, promo):
        """The cash a driver collects on a cash trip is `rider_payable`, not the fare."""
        rider, driver = make_rider('R7', '91'), hyd_driver(world, 'R7')
        trip, _ = book(world, rider, driver, method='cash')
        for user in (rider, driver.user_id):
            data = client_for(user).get(f'/api/v1/ride/trip/{trip.id}/').json()['data']
            assert data['estimated_fare'] == '200.00'                 # the full fare, unchanged
            assert (data['promo_discount'], data['rider_payable']) == ('50.00', '150.00')
        plain = completed_trip(world, rider, driver)
        data = client_for(rider).get(f'/api/v1/ride/trip/{plain.id}/').json()['data']
        assert (data['promo_discount'], data['rider_payable']) == (None, None)

    def test_a_promo_may_cover_the_whole_fare(self, world):
        launch(world.mkt[0], promo_payload(
            budget_minor=500_000, config={'code': 'BIGONE', 'discount_type': 'flat',
                                          'discount_flat_minor': 90_000}))
        result = apply_promo('BIGONE', make_rider('R5', '91'), D('200.00'), zone=world.hyd)
        assert result.ok and result.discount_amount == D('200.00') and result.final_fare == D('0.00')

    def test_a_platform_funded_end_carries_no_promo(self, world, promo):
        trip, _ = book(world, make_rider('R6', '91'), hyd_driver(world, 'R6'))
        trip.end_rule = 'platform_funded'
        trip.save(update_fields=['end_rule'])
        assert rider_promos.discount_minor(trip) == 0
        assert rider_promos.release_for_trip(trip) == 5_000
        promo.refresh_from_db()
        assert promo.committed_minor == 0


class TestBookedTripsAreHonoured:
    def test_a_booked_trip_keeps_its_discount_when_the_campaign_is_cancelled(self, world, promo):
        trip, _ = book(world, make_rider('H1', '91'), hyd_driver(world, 'H1'))
        post(world.mkt[0], f'{ADM}/campaigns/{promo.id}/cancel/', {'reason': 'stop'}, expect=200)
        promo.refresh_from_db()
        assert promo.state == 'cancelled' and promo.committed_minor == 5_000
        assert rider_promos.payable_minor(trip) == 15_000
        settle(trip)
        promo.refresh_from_db()
        assert (promo.committed_minor, promo.spent_minor) == (0, 5_000)
        assert LedgerEntry.objects.filter(kind=DISCOUNT_FUNDED, trip=trip).count() == 1

    def test_a_cancelled_campaign_takes_no_new_application(self, world, promo):
        post(world.mkt[0], f'{ADM}/campaigns/{promo.id}/cancel/', {'reason': 'stop'}, expect=200)
        result = apply_promo('WELCOME50', make_rider('H2', '91'), D('200.00'), zone=world.hyd)
        assert not result.ok and result.reason == 'PROMO_NOT_FOUND'

    def test_a_paused_campaign_takes_no_new_application_and_resume_restores_it(self, world, promo):
        post(world.mkt[0], f'{ADM}/campaigns/{promo.id}/pause/', {}, expect=200)
        rider = make_rider('H3', '91')
        assert not apply_promo('WELCOME50', rider, D('200.00'), zone=world.hyd).ok
        post(world.mkt[0], f'{ADM}/campaigns/{promo.id}/resume/', {}, expect=200)
        assert apply_promo('WELCOME50', rider, D('200.00'), zone=world.hyd).ok


class TestCancelledTripsReleaseTheirReservation:
    def test_a_cancelled_trip_gives_the_budget_back_and_the_rider_may_use_the_code_again(
            self, world, promo):
        rider = make_rider('X1', '91')
        trip, _ = book(world, rider, hyd_driver(world, 'X1'))
        promo.refresh_from_db()
        assert promo.committed_minor == 5_000
        assert not apply_promo('WELCOME50', rider, D('200.00'), zone=world.hyd).ok   # limit 1
        assert rider_promos.release_for_trip(trip) == 5_000
        promo.refresh_from_db()
        assert promo.committed_minor == 0
        assert PromoRedemption.objects.get(trip=trip).state == 'released'
        assert apply_promo('WELCOME50', rider, D('200.00'), zone=world.hyd).ok
        assert rider_promos.release_for_trip(trip) == 0        # idempotent

    def test_the_sweep_releases_the_reservation_of_a_trip_cancelled_anywhere(self, world, promo):
        from finance_world import status_obj
        from servers.promotions.workflow import sweep

        trip, _ = book(world, make_rider('X2', '91'), hyd_driver(world, 'X2'))
        trip.status_id = status_obj('cancelled')
        trip.save(update_fields=['status_id'])
        assert sweep()['released'] == 1
        promo.refresh_from_db()
        assert promo.committed_minor == 0
        assert sweep()['released'] == 0


class TestTheBudgetIsAHardCap:
    def test_the_last_discount_is_taken_once_and_then_the_campaign_stops(self, world):
        campaign = launch(world.mkt[0], promo_payload(
            budget_minor=5_000, config={'code': 'LASTONE', 'discount_type': 'flat',
                                        'discount_flat_minor': 5_000, 'max_per_user': 5}))
        first, second = make_rider('B1', '91'), make_rider('B2', '91')
        driver = hyd_driver(world, 'B')
        t1 = completed_trip(world, first, driver)
        t2 = completed_trip(world, second, driver)
        assert redeem_promo('LASTONE', first, t1, D('200.00'), zone=world.hyd).ok
        refused = redeem_promo('LASTONE', second, t2, D('200.00'), zone=world.hyd)
        assert not refused.ok and refused.reason in ('PROMO_NOT_FOUND', 'PROMO_BUDGET_EXHAUSTED')
        campaign.refresh_from_db()
        assert campaign.state == 'exhausted' and campaign.committed_minor == 5_000
        assert not PromoRedemption.objects.filter(trip=t2).exists()

    def test_exhaustion_raises_an_info_alert_once(self, world):
        from servers.command_centre.models import OperatorAlert

        campaign = launch(world.mkt[0], promo_payload(budget_minor=5_000))
        budget.reserve(campaign.pk, 5_000)
        alerts = OperatorAlert.objects.filter(kind='promotion.budget_exhausted')
        assert alerts.count() == 1
        alert = alerts.get()
        assert alert.severity == 'info' and alert.city.code == 'IN-TG-HYD'
        assert alert.payload['campaign_id'] == campaign.pk

    def test_strict_cap_refuses_a_discount_that_does_not_fit(self, world):
        campaign = launch(world.mkt[0], promo_payload(
            budget_minor=8_000, config={'code': 'FITS', 'discount_type': 'flat',
                                        'discount_flat_minor': 5_000, 'max_per_user': 5}))
        budget.reserve(campaign.pk, 5_000)
        campaign.refresh_from_db()
        # 3,000 is left and cannot fund another 5,000 discount: the campaign has stopped.
        assert campaign.state == 'exhausted'
        with pytest.raises(budget.Refused):
            budget.reserve(campaign.pk, 5_000)

    def test_a_new_application_that_does_not_fit_is_always_refused(self, world):
        """There is no lenient mode: a remainder smaller than the discount admits nothing."""
        campaign = launch(world.mkt[0], promo_payload(
            budget_minor=6_000, config={'code': 'LOOSE', 'discount_type': 'flat',
                                        'discount_flat_minor': 5_000, 'max_per_user': 5}))
        riders = [make_rider(f'O{i}', '91') for i in range(2)]
        driver = hyd_driver(world, 'OV')
        trips = [completed_trip(world, r, driver) for r in riders]
        assert redeem_promo('LOOSE', riders[0], trips[0], D('200.00'), zone=world.hyd).ok
        assert not redeem_promo('LOOSE', riders[1], trips[1], D('200.00'), zone=world.hyd).ok
        campaign.refresh_from_db()
        assert campaign.committed_minor == 5_000 and campaign.overshoot_minor == 0
        assert not hasattr(campaign, 'strict_cap')

    def test_a_honoured_trip_may_finish_above_the_cap_and_is_recorded_and_alerted(self, world):
        """The ONLY way spend passes the cap: a booked trip honoured at a larger amount."""
        from servers.command_centre.models import OperatorAlert

        campaign = launch(world.mkt[0], promo_payload(budget_minor=6_000))
        budget.reserve(campaign.pk, 5_000)
        budget.adjust_reservation(campaign.pk, 5_000, 7_000)       # the final discount differs
        campaign.refresh_from_db()
        assert campaign.committed_minor == 7_000 and campaign.overshoot_minor == 1_000
        assert campaign.state == 'exhausted'
        alert = OperatorAlert.objects.get(kind='promotion.budget_overshoot')
        assert alert.severity == 'warning' and alert.payload['overshoot_minor'] == 1_000
        budget.settle(campaign.pk, 7_000)                          # honoured whatever the state
        campaign.refresh_from_db()
        assert (campaign.committed_minor, campaign.spent_minor) == (0, 7_000)
        assert campaign.overshoot_minor == 1_000

    def test_the_overshoot_alert_is_a_finance_alert_and_marketing_never_sees_it(self, world):
        from servers.command_centre.models import FINANCE_ALERT_KINDS, OperatorAlert
        from servers.promotions.models import Campaign

        assert 'promotion.budget_overshoot' in FINANCE_ALERT_KINDS
        assert 'promotion.budget_exhausted' not in FINANCE_ALERT_KINDS
        campaign = launch(world.mkt[0], promo_payload(budget_minor=6_000))
        budget.reserve(campaign.pk, 5_000)
        budget.adjust_reservation(campaign.pk, 5_000, 7_000)
        assert Campaign.objects.get(pk=campaign.pk).overshoot_minor == 1_000
        seen = lambda who: {a['kind'] for a in get(  # noqa: E731
            getattr(world, who)[0], '/api/v1/command-centre/admin/alerts/')['results']}
        assert 'promotion.budget_overshoot' in seen('finance')
        assert 'promotion.budget_overshoot' not in seen('mkt')
        assert 'promotion.budget_exhausted' in seen('mkt')
        assert OperatorAlert.objects.filter(kind='promotion.budget_overshoot').count() == 1

    def test_the_right_people_can_acknowledge_each_alert(self, world):
        from servers.command_centre.models import OperatorAlert

        campaign = launch(world.mkt[0], promo_payload(budget_minor=6_000))
        budget.reserve(campaign.pk, 5_000)
        budget.adjust_reservation(campaign.pk, 5_000, 7_000)
        exhausted = OperatorAlert.objects.get(kind='promotion.budget_exhausted')
        overshoot = OperatorAlert.objects.get(kind='promotion.budget_overshoot')
        base = '/api/v1/command-centre/admin/alerts'
        # Finance takes the money alert; Marketing the budget one; neither takes the other's.
        assert world.mkt[0].post(f'{base}/{overshoot.id}/acknowledge/', {}, format='json'
                                 ).status_code == 404
        assert world.finance[0].post(f'{base}/{overshoot.id}/acknowledge/', {'note': 'seen'},
                                     format='json').status_code == 200
        assert world.mkt[0].post(f'{base}/{exhausted.id}/acknowledge/', {'note': 'seen'},
                                 format='json').status_code == 200

    def test_reserve_release_and_settle_keep_the_counters_consistent(self, world):
        campaign = launch(world.mkt[0], promo_payload(budget_minor=50_000))
        budget.reserve(campaign.pk, 5_000)
        budget.reserve(campaign.pk, 5_000)
        budget.release(campaign.pk, 5_000)
        budget.settle(campaign.pk, 5_000)
        campaign.refresh_from_db()
        assert (campaign.committed_minor, campaign.spent_minor) == (0, 5_000)
        assert campaign.remaining_minor == 45_000

    def test_the_database_refuses_a_negative_counter(self, world):
        from django.db import IntegrityError, transaction

        campaign = launch(world.mkt[0], promo_payload())
        with pytest.raises(IntegrityError), transaction.atomic():
            type(campaign).objects.filter(pk=campaign.pk).update(spent_minor=-1)


class TestAutoAppliedPromos:
    def test_the_best_live_auto_promo_is_applied_at_booking(self, world):
        from servers.promotions import rider_promos as rp

        launch(world.mkt[0], promo_payload(
            name='Auto ten', config={'auto_apply': True, 'discount_type': 'flat',
                                     'discount_flat_minor': 1_000}))
        launch(world.mkt[0], promo_payload(
            name='Auto twenty', config={'auto_apply': True, 'discount_type': 'flat',
                                        'discount_flat_minor': 2_000}))
        rider = make_rider('A1', '91')
        trip = completed_trip(world, rider, hyd_driver(world, 'A1'))
        result = rp.book(rider, trip, D('200.00'), world.hyd, '')
        assert result is not None and result.discount_amount == D('20.00')
        assert rp.payable_minor(trip) == 18_000

    def test_a_typed_code_that_cannot_apply_refuses_the_booking(self, world, promo):
        rider = make_rider('A2', '91')
        trip = completed_trip(world, rider, hyd_driver(world, 'A2'))
        with pytest.raises(rider_promos.PromoRefused) as exc:
            rider_promos.book(rider, trip, D('200.00'), world.hyd, 'NOSUCHCODE')
        assert exc.value.reason == 'PROMO_NOT_FOUND'

    def test_no_code_and_no_auto_promo_books_at_the_full_fare(self, world):
        rider = make_rider('A3', '91')
        trip = completed_trip(world, rider, hyd_driver(world, 'A3'))
        assert rider_promos.book(rider, trip, D('200.00'), world.hyd, '') is None

    def test_new_riders_only_excludes_a_rider_with_a_completed_trip(self, world):
        launch(world.mkt[0], promo_payload(
            config={'code': 'NEWONLY', 'discount_type': 'flat', 'discount_flat_minor': 1_000,
                    'new_riders_only': True}))
        veteran = make_rider('A4', '91')
        completed_trip(world, veteran, hyd_driver(world, 'A4'))
        assert apply_promo('NEWONLY', veteran, D('200.00'), zone=world.hyd).reason == \
            'PROMO_NEW_RIDERS_ONLY'
        assert apply_promo('NEWONLY', make_rider('A5', '91'), D('200.00'), zone=world.hyd).ok

    def test_a_city_campaign_does_not_serve_another_citys_zone(self, world):
        launch(world.mkt[0], promo_payload())
        result = apply_promo('WELCOME50', make_rider('A6', '91'), D('200.00'), zone=world.aus)
        assert not result.ok and result.reason == 'PROMO_WRONG_ZONE'
