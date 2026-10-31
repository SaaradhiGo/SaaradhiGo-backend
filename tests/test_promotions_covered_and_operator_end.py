"""Module 12, two rulings (AUDIT 27.51) the first cut got wrong, now pinned.

1. A promo applied to a booked trip is always honoured for that trip, including a trip an operator
   ENDS. STANDARD rule: the rider pays (capped fare - discount), the discount re-worked on the capped
   fare under the promo's own terms, driver earnings and commission on the full capped fare, the
   discount platform-funded. PLATFORM-FUNDED rule: the rider pays 0, so the reservation is released.
2. A promo may cover the WHOLE fare. The trip then settles with no payment and no gateway order:
   driver gross, commission and net identical to a trip with a smaller promo, the full discount as
   the platform-funded ledger entry, create-order answers 409 NOT_CHARGED, the receipt shows the
   discount and 0 charged, and a cash driver (who collects 0) is credited the discount.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from finance_world import client_for, make_rider, make_trip, settle, zone
from servers.payments.models import Payment, TripSettlement
from servers.promotions import rider_promos
from servers.promotions.models import DISCOUNT_FUNDED, Campaign, LedgerEntry
from servers.ride.models import PromoCode, PromoRedemption
from servers.ride.promos import redeem_promo
from servers.rider.models import Wallet, WalletTransaction
from test_sos_support_zones_matrix import geo, operator  # noqa: F401 - fixtures
from test_stale_started_release import card, release, preview, started  # noqa: F401
from test_stale_trip_actions import world  # noqa: F401 - fixture

pytestmark = pytest.mark.django_db

D = Decimal


def live_promo(code, *, discount_type='flat', flat=None, percent=None, budget=100_000):
    now = timezone.now()
    promo = PromoCode.objects.create(
        code=code, discount_type=discount_type, discount_flat_minor=flat,
        discount_percent=percent, currency='INR', max_per_user_redemptions=5,
        valid_from=now - timedelta(days=1), valid_to=now + timedelta(days=5))
    return Campaign.objects.create(
        kind='rider_promo', name=code, state='live', country='IN', currency='INR',
        budget_minor=budget, starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=5),
        promo_code=promo, config={'code': code, 'discount_type': discount_type})


def promo_on(trip, code):
    result = redeem_promo(code, trip.user_id, trip, D(trip.estimated_fare), zone=trip.zone)
    assert result.ok, result.reason
    return result


def wallet_of(driver):
    return Wallet.objects.get(user_id=driver.user_id, scope=Wallet.SCOPE_DRIVER)


# ===========================================================================
# An operator ends a booked trip that holds a promo
# ===========================================================================

class TestOperatorEnd:
    def test_standard_rule_honours_the_promo_on_the_capped_fare(self, geo, world, card):  # noqa: F811
        campaign = live_promo('END20', flat=2_000)
        trip = started(world, card, km=2, method='cash')          # fare 49.00, upfront 53.00
        promo_on(trip, 'END20')
        client, _ = operator('city_ops_manager', geo)

        data = preview(client, trip, 'other').json()['data']
        assert data['capped_fare_minor'] == 4_900 and data['rider_pays_minor'] == 4_900
        assert (data['promo_discount_minor'], data['rider_pays_after_promo_minor']) == (2_000, 2_900)

        done = release(client, trip, 'other').json()['data']
        assert (done['promo_discount_minor'], done['rider_pays_after_promo_minor']) == (2_000, 2_900)
        trip.refresh_from_db()
        assert trip.final_fare_minor == 4_900                        # the FULL capped fare
        assert rider_promos.payable_minor(trip) == 2_900
        # Driver economics are on the full capped fare: 20% commission = 980, net 3,920.
        s = TripSettlement.objects.get(trip=trip)
        assert (s.gross_fare_minor, s.commission_amount_minor, s.driver_net_minor) == (4_900, 980, 3_920)
        assert Payment.objects.get(trip_id=trip).amount_minor == 2_900
        # The discount is platform-funded; the cash driver collected less and is credited it.
        assert LedgerEntry.objects.get(kind=DISCOUNT_FUNDED).amount_minor == 2_000
        credit = WalletTransaction.objects.get(purpose='promo_discount_platform_funded')
        assert (credit.amount_minor, credit.user_id_id) == (2_000, trip.driver_id.user_id_id)
        campaign.refresh_from_db()
        assert (campaign.committed_minor, campaign.spent_minor) == (0, 2_000)
        assert PromoRedemption.objects.get(trip=trip).state == 'settled'

    def test_the_discount_is_re_worked_under_the_promos_own_terms(self, geo, world, card):  # noqa: F811
        campaign = live_promo('HALF', discount_type='percent', percent=D('50.00'))
        trip = started(world, card, km=2, method='cash')          # reserved 50% of 53.00 = 26.50
        promo_on(trip, 'HALF')
        assert PromoRedemption.objects.get(trip=trip).discount_amount_minor == 2_650
        campaign.refresh_from_db()
        assert campaign.committed_minor == 2_650
        client, _ = operator('city_ops_manager', geo)
        assert preview(client, trip, 'other').json()['data']['promo_discount_minor'] == 2_450
        release(client, trip, 'other')
        redemption = PromoRedemption.objects.get(trip=trip)
        assert (redemption.discount_amount_minor, redemption.state) == (2_450, 'settled')   # 50% of 49.00
        campaign.refresh_from_db()
        assert (campaign.committed_minor, campaign.spent_minor) == (0, 2_450)
        trip.refresh_from_db()
        assert rider_promos.payable_minor(trip) == 2_450

    def test_an_online_prepaid_amount_above_what_is_owed_is_refunded(self, geo, world, card):  # noqa: F811
        live_promo('END20', flat=2_000)
        trip = started(world, card, km=2, method='cash')
        promo_on(trip, 'END20')
        client, _ = operator('city_ops_manager', geo)
        data = release(client, trip, 'other').json()['data']
        assert data['rider_pays_after_promo_minor'] == 2_900

    def test_platform_funded_rule_releases_the_reservation(self, geo, world, card):  # noqa: F811
        campaign = live_promo('END20', flat=2_000)
        trip = started(world, card, km=2, method='cash')
        promo_on(trip, 'END20')
        client, _ = operator('city_ops_manager', geo)
        data = preview(client, trip, 'safety_incident').json()['data']
        assert data['rider_pays_minor'] == 0
        assert (data['promo_discount_minor'], data['rider_pays_after_promo_minor']) == (0, 0)
        release(client, trip, 'safety_incident')
        campaign.refresh_from_db()
        assert (campaign.committed_minor, campaign.spent_minor) == (0, 0)
        assert PromoRedemption.objects.get(trip=trip).state == 'released'
        assert not LedgerEntry.objects.filter(kind=DISCOUNT_FUNDED).exists()
        trip.refresh_from_db()
        assert rider_promos.discount_minor(trip) == 0

    def test_a_trip_with_no_promo_is_unchanged(self, geo, world, card):  # noqa: F811
        trip = started(world, card, km=2, method='cash')
        client, _ = operator('city_ops_manager', geo)
        data = preview(client, trip, 'other').json()['data']
        assert (data['promo_discount_minor'], data['rider_pays_after_promo_minor']) == (0, 4_900)


# ===========================================================================
# A promo that covers the whole fare
# ===========================================================================

@pytest.fixture
def hyd(geo):  # noqa: F811
    return zone('IN-TG-HYD', geo['IN-TG-HYD'])


def driver_for(geo, tag):  # noqa: F811
    from finance_world import make_driver

    return make_driver(geo, 'IN-TG-HYD', tag, currency='INR', calling='91')


class TestFullyCovered:
    @pytest.mark.parametrize('method', ['cash', 'online'])
    def test_driver_economics_are_identical_to_a_smaller_promo(self, geo, hyd, method):  # noqa: F811
        from servers.ride.utils import create_payment_on_complete

        live_promo('ALLOFIT', flat=50_000)
        live_promo('SOME', flat=5_000)
        d_all, d_some = driver_for(geo, 'CV1'), driver_for(geo, 'CV2')
        t_all = make_trip(hyd, make_rider('CV1', '91'), d_all, fare='200.00', method=method)
        t_some = make_trip(hyd, make_rider('CV2', '91'), d_some, fare='200.00', method=method)
        assert promo_on(t_all, 'ALLOFIT').final_fare == D('0.00')
        promo_on(t_some, 'SOME')
        assert rider_promos.payable_minor(t_all) == 0 and rider_promos.is_fully_covered(t_all)
        for t in (t_all, t_some):
            create_payment_on_complete(t)
        if method == 'online':
            settle(t_some)      # a smaller promo still waits for the gateway webhook; this one did not

        a, b = TripSettlement.objects.get(trip=t_all), TripSettlement.objects.get(trip=t_some)
        assert (a.gross_fare_minor, a.commission_percent, a.commission_amount_minor, a.driver_net_minor) == (
            b.gross_fare_minor, b.commission_percent, b.commission_amount_minor, b.driver_net_minor) \
            == (20_000, D('15.00'), 3_000, 17_000)
        # No payment for the rider to make, and nothing created for a gateway.
        assert not Payment.objects.filter(trip_id=t_all).exists()
        t_all.refresh_from_db()
        assert t_all.payment_status == 'completed'
        # The full discount is the platform-funded entry; the campaign spends it.
        entry = LedgerEntry.objects.get(kind=DISCOUNT_FUNDED, trip=t_all)
        assert entry.amount_minor == 20_000
        # Driver wallet: online = net; cash = commission debit plus the credited discount, so that
        # (cash held) + (wallet) is the same 17,000 as with the smaller promo.
        if method == 'online':
            assert wallet_of(d_all).balance_minor == wallet_of(d_some).balance_minor == 17_000
            assert not WalletTransaction.objects.filter(
                purpose='promo_discount_platform_funded', user_id=d_all.user_id).exists()
        else:
            held_all, held_some = 0, rider_promos.payable_minor(t_some)
            assert held_all + wallet_of(d_all).balance_minor == 17_000
            assert held_some + wallet_of(d_some).balance_minor == 17_000
            credit = WalletTransaction.objects.get(
                purpose='promo_discount_platform_funded', user_id=d_all.user_id)
            assert credit.amount_minor == 20_000

    def test_create_order_answers_not_charged(self, geo, hyd):  # noqa: F811
        live_promo('ALLOFIT', flat=50_000)
        rider = make_rider('CV3', '91')
        trip = make_trip(hyd, rider, driver_for(geo, 'CV3'), fare='200.00', method='online')
        promo_on(trip, 'ALLOFIT')
        response = client_for(rider).post('/api/v1/payments/create-order/', {'trip_id': trip.id},
                                          format='json')
        assert response.status_code == 409
        assert response.json()['error']['code'] == 'NOT_CHARGED'
        assert not Payment.objects.filter(trip_id=trip).exists()

    def test_the_receipt_shows_the_discount_and_zero_charged(self, geo, hyd):  # noqa: F811
        from servers.ride.receipts import promo_lines

        live_promo('ALLOFIT', flat=50_000)
        trip = make_trip(hyd, make_rider('CV4', '91'), driver_for(geo, 'CV4'), fare='200.00')
        promo_on(trip, 'ALLOFIT')
        [(label, discount, charged)] = promo_lines(trip)
        assert label == 'Promo discount (ALLOFIT)'
        assert (discount, charged) == (D('200.00'), D('0.00'))

    def test_a_promo_that_does_not_cover_the_fare_still_pays_the_driver_the_same(self, geo, hyd):  # noqa: F811
        live_promo('SOME', flat=5_000)
        trip = make_trip(hyd, make_rider('CV5', '91'), driver_for(geo, 'CV5'), fare='200.00')
        promo_on(trip, 'SOME')
        assert not rider_promos.is_fully_covered(trip)
        settle(trip)
        assert TripSettlement.objects.get(trip=trip).driver_net_minor == 17_000
