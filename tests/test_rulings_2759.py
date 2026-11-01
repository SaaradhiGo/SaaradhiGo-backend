"""Owner rulings of 2026-11-01 (AUDIT 27.59): US tax on a promo trip, and the platform-paid early end.

(Ruling 1, Marketing GMV in Reports, is tested in test_reports_api.py.)

TEST-ONLY NUMBERS. The 8.25 percent tax and 0.50 fee are fixtures chosen for the worked example in the
brief; no rate is decided anywhere in the code (go-live blocker #15, US accountant to confirm).

THE WORKED EXAMPLE (US, additive): fare 20.00, promo 5.00, tax 8.25%, fee 0.50.
  quote, no promo      20.00 + 1.65 + 0.50                 = 22.15
  with the promo       (20.00 - 5.00) + 1.24 + 0.50        = 16.74   (tax 15.00 x 8.25% = 1.2375 -> 1.24)
  tax no longer charged 1.65 - 1.24                        = 0.41
  the trip's fare       22.15 - 0.41                       = 21.74   (commission and driver earnings base)
  commission 25%        21.74 x 25% = 5.435 -> 5.44 ; driver net 16.30
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from base.money import to_minor
from servers.driver.utils import credit_driver_wallet
from servers.payments.models import PlatformExpense, TripSettlement
from servers.pricing import promo_tax
from servers.pricing.models import RateCard
from servers.promotions import budget, rider_promos
from servers.promotions.models import DISCOUNT_FUNDED, Campaign, LedgerEntry
from servers.ride import operator_end, receipts
from servers.ride.models import FarePricing, PromoCode, PromoRedemption, Trip
from servers.ride.promos import apply_promo, redeem_promo
from servers.rider.models import Wallet, WalletTransaction
from test_operator_end_us_tax import row, us, us_started  # noqa: F401 - fixtures and helpers
from test_sos_support_zones_matrix import geo, operator  # noqa: F401 - fixtures
from test_stale_started_release import card, release, started  # noqa: F401
from test_stale_trip_actions import world  # noqa: F401 - fixture

pytestmark = pytest.mark.django_db

D = Decimal


@pytest.fixture
def us825(us):  # noqa: F811
    """Austin at the worked example's numbers: 8.25 percent tax, 0.50 per-trip fee."""
    aus = us['aus']
    aus.per_trip_fee = D('0.50')
    aus.save()
    RateCard.objects.filter(zone=aus).delete()
    RateCard.objects.create(
        zone=aus, vehicle_type=us['sedan'], version=1, is_active=True,
        effective_from=timezone.now() - timedelta(days=30), base_fare=D('5.00'),
        per_km_fare=D('2.00'), per_min_fare=D('0.50'), min_fare=D('3.00'), gst_percent=D('8.25'))
    return us


def launch_promo(code='FIVE', *, flat_minor=500, currency='USD', country='US', kind='flat'):
    now = timezone.now()
    promo = PromoCode.objects.create(
        code=code, discount_type=kind, discount_flat_minor=flat_minor, currency=currency,
        max_per_user_redemptions=5, valid_from=now - timedelta(days=1),
        valid_to=now + timedelta(days=5))
    campaign = Campaign.objects.create(
        kind='rider_promo', name=code, state='live', country=country, currency=currency,
        budget_minor=100_000, starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=5),
        promo_code=promo, config={'code': code, 'discount_type': kind})
    return promo, campaign


def us_trip(us, *, fare=2215, method='online', percent=25):
    """A US trip as booking leaves it: the quote total as the estimate, no final fare yet."""
    trip = us_started(us, fare=fare, method=method)
    Trip.objects.filter(pk=trip.pk).update(commission_percent_snapshot=D(percent),
                                           final_fare_minor=None)
    trip.refresh_from_db()
    return trip


def driver_wallet(driver):
    return Wallet.objects.get(user_id=driver.user_id, scope=Wallet.SCOPE_DRIVER)


# ---------------------------------------------------------------------------
# The rule itself
# ---------------------------------------------------------------------------

class TestTheRule:
    def test_the_worked_example(self):
        pretax, tax, fee = promo_tax.split_additive(D('22.15'), D('8.25'), D('0.50'), 'USD')
        assert (pretax, tax, fee) == (D('20.00'), D('1.65'), D('0.50'))
        adj = promo_tax.adjust(pretax, D('8.25'), fee, D('5.00'), 'USD', tax)
        assert (adj['tax'], adj['relief'], adj['full'], adj['discount'], adj['payable']) == (
            D('1.24'), D('0.41'), D('21.74'), D('5.00'), D('16.74'))
        # rider total = (fare - discount) + tax(fare - discount) + fee
        assert adj['payable'] == (D('20.00') - D('5.00')) + D('1.24') + D('0.50')

    def test_no_discount_gives_back_the_quotes_own_tax(self):
        adj = promo_tax.adjust(D('20.00'), D('8.25'), D('0.50'), D('0.00'), 'USD', D('1.65'))
        assert (adj['tax'], adj['relief'], adj['full'], adj['payable']) == (
            D('1.65'), D('0.00'), D('22.15'), D('22.15'))

    def test_a_discount_larger_than_the_fare_floors_the_tax_at_zero(self):
        adj = promo_tax.adjust(D('20.00'), D('8.25'), D('0.50'), D('22.15'), 'USD', D('1.65'))
        assert adj['tax'] == D('0.00') and adj['relief'] == D('1.65')
        assert adj['full'] == D('20.50') and adj['discount'] == D('20.50')   # capped at the charge
        assert adj['payable'] == D('0.00')

    @pytest.mark.parametrize('total_minor', [1_000, 1_315, 2_215, 2_899, 5_555, 9_999])
    def test_the_split_of_any_quote_is_exact(self, total_minor):
        total = D(total_minor) / 100
        pretax, tax, fee = promo_tax.split_additive(total, D('8.25'), D('0.50'), 'USD')
        assert pretax + tax + fee == total
        assert tax == (pretax * D('8.25') / 100).quantize(D('0.01'), rounding='ROUND_HALF_UP')

    def test_an_inclusive_zone_has_no_context(self, world):  # noqa: F811
        assert promo_tax.additive_context(world['hyd']) is None


# ---------------------------------------------------------------------------
# The quote / preview and the booking
# ---------------------------------------------------------------------------

class TestQuoteAndBooking:
    def test_the_preview_shows_tax_on_the_discounted_fare(self, us825):
        launch_promo()
        trip = us_trip(us825)
        result = apply_promo('FIVE', trip.user_id, D('22.15'), zone=trip.zone)
        assert result.ok
        assert (result.discount_amount, result.final_fare) == (D('5.00'), D('16.74'))
        assert (result.tax_amount, result.tax_relief) == (D('1.24'), D('0.41'))
        assert result.to_dict()['tax_amount'] == '1.24'

    def test_the_booking_matches_the_preview_and_lowers_the_trips_fare(self, us825):
        _promo, campaign = launch_promo()
        trip = us_trip(us825)
        result = redeem_promo('FIVE', trip.user_id, trip, D('22.15'), zone=trip.zone)
        assert result.ok and result.final_fare == D('16.74') and result.tax_relief == D('0.41')
        trip.refresh_from_db()
        assert trip.estimated_fare_minor == 2_174                       # 22.15 - 0.41
        assert rider_promos.discount_minor(trip) == 500
        assert rider_promos.payable_minor(trip) == 1_674                # what the rider is charged
        assert PromoRedemption.objects.get(trip=trip).discount_amount_minor == 500
        campaign.refresh_from_db()
        assert campaign.committed_minor == 500

    def test_the_quote_passed_in_by_the_booking_is_used_as_given(self, us825):
        launch_promo()
        trip = us_trip(us825)
        result = redeem_promo('FIVE', trip.user_id, trip, D('22.15'), zone=trip.zone,
                              tax=(D('8.25'), D('0.50')))
        assert result.final_fare == D('16.74')

    def test_book_applies_the_rule_for_a_us_quote_and_leaves_an_inclusive_quote_alone(self, us825):
        launch_promo()
        trip = us_trip(us825)
        result = rider_promos.book(trip.user_id, trip, D('22.15'), trip.zone, 'FIVE',
                                   tax=(D('8.25'), D('0.50')))
        assert result.final_fare == D('16.74')
        trip.refresh_from_db()
        assert trip.estimated_fare_minor == 2_174
        # an inclusive quote (tax=False) is never adjusted, whatever the zone says
        other = us_trip(us825)
        again = rider_promos.book(other.user_id, other, D('22.15'), other.zone, 'FIVE', tax=False)
        assert again.final_fare == D('17.15') and again.tax_relief is None
        other.refresh_from_db()
        assert other.estimated_fare_minor == 2_215

    def test_a_percent_promo_is_computed_on_the_quoted_total_as_before(self, us825):
        now = timezone.now()
        promo = PromoCode.objects.create(
            code='TEN', discount_type='percent', discount_percent=D('10.00'), currency='USD',
            max_per_user_redemptions=5, valid_from=now - timedelta(days=1),
            valid_to=now + timedelta(days=5))
        Campaign.objects.create(
            kind='rider_promo', name='TEN', state='live', country='US', currency='USD',
            budget_minor=100_000, starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=5),
            promo_code=promo, config={'code': 'TEN', 'discount_type': 'percent'})
        trip = us_trip(us825)
        result = apply_promo('TEN', trip.user_id, D('22.15'), zone=trip.zone)
        # module 12's discount: 10% of the quoted 22.15 = 2.215 -> 2.22 (unchanged); the tax is then
        # on (20.00 - 2.22) = 17.78 x 8.25% = 1.467 -> 1.47, so 0.18 less than the 1.65
        assert result.discount_amount == D('2.22')
        assert (result.tax_amount, result.tax_relief) == (D('1.47'), D('0.18'))
        assert result.final_fare == D('17.78') + D('1.47') + D('0.50') == D('19.75')

    def test_an_inclusive_zone_is_unchanged_golden_india(self, world):  # noqa: F811
        """India: the same numbers with and without this change (a 200.00 fare, a 50.00 flat promo)."""
        from servers.promotions.rider_promos import payable_minor

        now = timezone.now()
        promo = PromoCode.objects.create(
            code='FIFTY', discount_type='flat', discount_flat_minor=5_000, currency='INR',
            max_per_user_redemptions=5, valid_from=now - timedelta(days=1),
            valid_to=now + timedelta(days=5))
        Campaign.objects.create(
            kind='rider_promo', name='FIFTY', state='live', country='IN', currency='INR',
            budget_minor=100_000, starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=5),
            promo_code=promo, config={'code': 'FIFTY', 'discount_type': 'flat'})
        trip = world['ghost'](status='in_progress', method='online')
        Trip.objects.filter(pk=trip.pk).update(estimated_fare_minor=20_000, final_fare_minor=None)
        trip.refresh_from_db()
        preview = apply_promo('FIFTY', trip.user_id, D('200.00'), zone=trip.zone)
        assert (preview.discount_amount, preview.final_fare) == (D('50.00'), D('150.00'))
        assert 'tax_amount' not in preview.to_dict()
        result = redeem_promo('FIFTY', trip.user_id, trip, D('200.00'), zone=trip.zone)
        assert (result.discount_amount, result.final_fare) == (D('50.00'), D('150.00'))
        assert result.tax_amount is None and result.tax_relief is None
        trip.refresh_from_db()
        assert trip.estimated_fare_minor == 20_000 and payable_minor(trip) == 15_000


# ---------------------------------------------------------------------------
# Settlement and the ledger (a US promo trip, cash and online)
# ---------------------------------------------------------------------------

class TestSettlementBalances:
    def book(self, us825, method):
        _promo, campaign = launch_promo()
        trip = us_trip(us825, method=method)
        assert redeem_promo('FIVE', trip.user_id, trip, D('22.15'), zone=trip.zone).ok
        trip.refresh_from_db()
        return trip, campaign

    def test_online_the_driver_is_owed_the_net_of_the_lowered_fare(self, us825):
        trip, campaign = self.book(us825, 'online')
        driver = us825['world']['aus_driver']
        credit_driver_wallet(trip)
        s = TripSettlement.objects.get(trip=trip)
        assert (s.gross_fare_minor, s.commission_amount_minor, s.driver_net_minor) == (
            2_174, 544, 1_630)
        assert driver_wallet(driver).balance_minor == 1_630
        # the platform collected the rider's 16.74, owes the driver 16.30 and keeps 5.44 commission;
        # it funded the 5.00 discount: 16.74 + 5.00 = 21.74 = 16.30 + 5.44
        assert rider_promos.payable_minor(trip) + 500 == s.driver_net_minor + s.commission_amount_minor
        entry = LedgerEntry.objects.get(kind=DISCOUNT_FUNDED, trip=trip)
        assert (entry.amount_minor, entry.currency) == (500, 'USD')          # the discount, unchanged
        assert not WalletTransaction.objects.filter(purpose='promo_discount_platform_funded').exists()
        campaign.refresh_from_db()
        assert (campaign.committed_minor, campaign.spent_minor) == (0, 500)

    def test_cash_the_driver_holds_the_charge_and_is_credited_the_discount(self, us825):
        trip, _campaign = self.book(us825, 'cash')
        driver = us825['world']['aus_driver']
        credit_driver_wallet(trip)
        s = TripSettlement.objects.get(trip=trip)
        assert (s.gross_fare_minor, s.commission_amount_minor, s.driver_net_minor) == (
            2_174, 544, 1_630)
        cash_held = rider_promos.payable_minor(trip)
        assert cash_held == 1_674
        credit = WalletTransaction.objects.get(purpose='promo_discount_platform_funded')
        assert (credit.amount_minor, credit.txn_type) == (500, 'credit')     # the discount, unchanged
        # cash in hand + wallet (credit - commission owed) == the driver's net: the ledger balances
        assert cash_held + driver_wallet(driver).balance_minor == s.driver_net_minor
        assert driver_wallet(driver).balance_minor == 500 - 544

    @pytest.mark.parametrize('method', ['online', 'cash'])
    def test_settling_twice_changes_nothing(self, us825, method):
        trip, _campaign = self.book(us825, method)
        driver = us825['world']['aus_driver']
        credit_driver_wallet(trip)
        before = driver_wallet(driver).balance_minor
        credit_driver_wallet(trip)
        assert driver_wallet(driver).balance_minor == before
        assert TripSettlement.objects.filter(trip=trip).count() == 1
        assert LedgerEntry.objects.filter(kind=DISCOUNT_FUNDED, trip=trip).count() == 1

    @pytest.mark.parametrize('method', ['online', 'cash'])
    def test_a_us_trip_without_a_promo_is_unchanged(self, us825, method):
        trip = us_trip(us825, method=method)
        credit_driver_wallet(trip)
        s = TripSettlement.objects.get(trip=trip)
        # 22.15 gross as before (tax on the full fare), 25% = 5.5375 -> 5.54, net 16.61
        assert (s.gross_fare_minor, s.commission_amount_minor, s.driver_net_minor) == (
            2_215, 554, 1_661)
        assert rider_promos.payable_minor(trip) == 2_215
        assert not LedgerEntry.objects.filter(kind=DISCOUNT_FUNDED).exists()


# ---------------------------------------------------------------------------
# The receipt
# ---------------------------------------------------------------------------

class TestReceipt:
    def test_the_tax_line_is_on_what_the_rider_was_charged(self, us825):
        launch_promo()
        trip = us_trip(us825)
        redeem_promo('FIVE', trip.user_id, trip, D('22.15'), zone=trip.zone)
        trip.refresh_from_db()
        total = receipts._total_fare(trip)
        assert total == D('21.74')
        basis = receipts._tax_basis(trip, total)
        assert basis == D('16.74')
        tax = receipts._receipt_tax(trip, basis, D('8.25'))
        assert tax['amount'] == D('1.24') and tax['mode'] == 'additive'
        (label, discount, charged), = receipts.promo_lines(trip)
        assert (discount, charged) == (D('5.00'), D('16.74'))

    def test_a_us_trip_without_a_promo_taxes_the_total_as_before(self, us825):
        trip = us_trip(us825)
        total = receipts._total_fare(trip)
        assert receipts._tax_basis(trip, total) == total == D('22.15')
        assert receipts._receipt_tax(trip, total, D('8.25'))['amount'] == D('1.65')

    def test_india_receipt_tax_is_unchanged(self, world):  # noqa: F811
        trip = world['ghost'](status='completed', method='online')
        Trip.objects.filter(pk=trip.pk).update(estimated_fare_minor=20_000)
        trip.refresh_from_db()
        total = receipts._total_fare(trip)
        assert receipts._tax_basis(trip, total) == total
        assert receipts._receipt_tax(trip, total, D('5.00'))['amount'] == D('9.52')


# ---------------------------------------------------------------------------
# The operator-end path with a promo (standard rule)
# ---------------------------------------------------------------------------

class TestOperatorEndWithPromo:
    def test_the_standard_end_taxes_the_discounted_fare(self, us825):
        _promo, _campaign = launch_promo(flat_minor=200)
        trip = us_started(us825, km=2, method='online')
        Trip.objects.filter(pk=trip.pk).update(commission_percent_snapshot=D('25.00'))
        trip.refresh_from_db()
        assert redeem_promo('FIVE', trip.user_id, trip, D(trip.estimated_fare), zone=trip.zone).ok
        data = release(us825['client'], trip, 'other').json()['data']
        # metered fare 10.50, tax 8.25% = 0.87 (0.866), fee 0.50 -> 11.87 charged before the promo
        assert data['capped_fare_minor'] == 1_187
        assert data['promo_discount_minor'] == 200
        # tax on (10.50 - 2.00) = 8.50 x 8.25% = 0.70125 -> 0.70, so 0.17 less than before
        assert data['tax_minor'] == 70 and data['tax_relief_minor'] == 17
        assert data['pretax_fare_minor'] == 1_050 and data['per_trip_fee_minor'] == 50
        assert data['rider_pays_minor'] == 1_170                      # 10.50 + 0.70 + 0.50
        assert data['rider_pays_after_promo_minor'] == 970            # (10.50 - 2.00) + 0.70 + 0.50
        assert data['commission_minor'] == 293                        # 25% of 11.70 = 2.925
        assert data['driver_earnings_minor'] == 1_170 - 293
        trip.refresh_from_db()
        assert trip.final_fare_minor == 1_170
        assert rider_promos.payable_minor(trip) == 970
        assert PromoRedemption.objects.get(trip=trip).discount_amount_minor == 200
        snap = row(trip)
        assert snap.pretax_fare_minor + snap.tax_minor + snap.per_trip_fee_minor == snap.gross_fare_minor
        assert (snap.gross_fare_minor, snap.discount_amount_minor, snap.rider_payable_minor) == (
            1_170, 200, 970)

    def test_a_standard_end_without_a_promo_is_unchanged(self, us825):
        trip = us_started(us825, km=2, method='online')
        data = release(us825['client'], trip, 'other').json()['data']
        assert data['tax_relief_minor'] == 0
        assert data['capped_fare_minor'] == data['rider_pays_minor'] == 1_187
        assert (data['pretax_fare_minor'], data['tax_minor'], data['per_trip_fee_minor']) == (
            1_050, 87, 50)


# ---------------------------------------------------------------------------
# Ruling 3: the platform-paid early end
# ---------------------------------------------------------------------------

def ledger_credits(trip):
    return WalletTransaction.objects.filter(
        user_id=trip.driver_id.user_id, purpose='platform_funded_trip_end')


class TestPlatformPaidEnd:
    def test_us_earnings_are_the_pretax_fare_less_commission_no_tax_no_fee(self, us825):
        trip = us_started(us825, km=2, fare=2_500)
        Trip.objects.filter(pk=trip.pk).update(commission_percent_snapshot=D('25.00'))
        trip.refresh_from_db()
        data = release(us825['client'], trip, 'platform_fault').json()['data']
        # the metered pre-tax fare for 2 km in 3 minutes is 10.50; 25% commission = 2.625 -> 2.63
        assert data['driver_earnings_base_minor'] == 1_050
        assert data['commission_minor'] == 263
        assert data['driver_earnings_minor'] == 1_050 - 263 == 787
        assert data['rider_pays_minor'] == 0
        credits = ledger_credits(trip)
        assert credits.count() == 1
        assert credits.get().amount_minor == 787 and credits.get().txn_type == 'credit'
        trip.refresh_from_db()
        assert trip.platform_funded_minor == 787

    def test_it_is_what_a_normal_trip_of_that_pretax_fare_nets(self, us825):
        trip = us_started(us825, km=2, fare=2_500)
        Trip.objects.filter(pk=trip.pk).update(commission_percent_snapshot=D('25.00'))
        trip.refresh_from_db()
        data = release(us825['client'], trip, 'platform_fault').json()['data']
        base = data['driver_earnings_base_minor']
        commission = to_minor(D(base) / 100 * D('25') / 100, 'USD')
        assert data['driver_earnings_minor'] == base - commission

    def test_the_pretax_cap_binds_below_the_metered_fare(self, us825):
        # an upfront total of 10.00 is a pre-tax fare below the metered 10.50
        trip = us_started(us825, km=2, fare=1_000)
        Trip.objects.filter(pk=trip.pk).update(commission_percent_snapshot=D('25.00'))
        trip.refresh_from_db()
        pretax_cap, tax, fee = promo_tax.split_additive(D('10.00'), D('8.25'), D('0.50'), 'USD')
        assert pretax_cap + tax + fee == D('10.00')
        data = release(us825['client'], trip, 'platform_fault').json()['data']
        base = to_minor(pretax_cap, 'USD')
        assert data['driver_earnings_base_minor'] == base < 1_050
        commission = to_minor(D(base) / 100 * D('25') / 100, 'USD')
        assert data['driver_earnings_minor'] == base - commission

    def test_a_booked_quote_split_is_the_cap(self, us825):
        """The booking stores the quote's pre-tax fare; the platform-paid cap uses it (a promo that
        lowered the trip's fare must not change how the cap is read)."""
        trip = us_started(us825, km=2, fare=2_174)
        Trip.objects.filter(pk=trip.pk).update(commission_percent_snapshot=D('25.00'))
        trip.refresh_from_db()
        FarePricing.objects.create(
            trip_id=trip, base_fare=D('5.00'), distance_fare=D('10.00'), time_fare=D('5.00'),
            surge_multiplier=D('1.00'), total_fare=D('21.74'),
            pretax_fare_minor=900, tax_minor=165, per_trip_fee_minor=50)
        plan = operator_end.build_plan(trip, 'platform_fault')
        assert plan.earnings_base_minor == 900            # the stored 9.00, below the metered 10.50

    def test_the_credit_is_idempotent(self, us825):
        trip = us_started(us825, km=2, fare=2_500)
        release(us825['client'], trip, 'platform_fault')
        plan = operator_end.build_plan(trip, 'platform_fault')
        first = operator_end._credit_driver(trip, plan)
        second = operator_end._credit_driver(trip, plan)
        assert first.pk == second.pk and ledger_credits(trip).count() == 1

    def test_the_regulatory_fee_is_still_the_platforms_expense(self, us825):
        trip = us_started(us825, km=2, fare=2_500)
        data = release(us825['client'], trip, 'safety_incident').json()['data']
        assert data['regulatory_fee_expense_minor'] == 50
        expense = PlatformExpense.objects.get()
        assert (expense.trip_id, expense.amount_minor, expense.currency) == (trip.pk, 50, 'USD')
        assert not WalletTransaction.objects.filter(purpose__icontains='regulatory').exists()

    def test_india_is_unchanged_fare_less_commission(self, geo, world, card):  # noqa: F811
        client, _ = operator('city_ops_manager', geo)
        trip = started(world, card, km=2)
        data = release(client, trip, 'platform_fault').json()['data']
        assert data['capped_fare_minor'] == 4_900
        assert data['driver_earnings_base_minor'] == 4_900       # the whole (GST-inclusive) fare
        commission = to_minor(D('49.00') * D('20.00') / 100, 'INR')
        assert data['driver_earnings_minor'] == 4_900 - commission == 3_920
        assert (data['pretax_fare_minor'], data['tax_minor']) == (None, None)
        assert data['tax_relief_minor'] == 0
