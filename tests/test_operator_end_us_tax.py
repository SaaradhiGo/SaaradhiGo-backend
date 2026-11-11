"""Ruling (e), AUDIT 27.56: US early-ended trips (an operator ends a STARTED trip).

THE RULE: tax and the per-trip fee are calculated on the amount actually charged, within the upfront
cap; the pre-tax capped fare is worked out first. When the rider is charged 0 (safety incident or
platform fault) NO tax or fee is charged to the rider, and the per-trip regulatory fee the platform
owes is booked as a PLATFORM EXPENSE, on no wallet. India (inclusive tax) is unchanged.

TEST-ONLY NUMBERS: the 10.00 percent tax and 1.50 fee below are fixture values chosen for round
arithmetic. No tax rate is decided anywhere in the code (go-live blocker #15, US accountant to
confirm); the code reads the zone's and the rate card's own configured values.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from servers.driver.models import VehicleType
from servers.payments.models import PlatformExpense
from servers.pricing.models import RateCard
from servers.ride import operator_end
from servers.ride.models import FarePricing
from servers.rider.models import WalletTransaction
from test_sos_support_zones_matrix import geo, operator  # noqa: F401 - fixtures
from test_stale_started_release import card, preview, release, started, trail  # noqa: F401
from test_stale_trip_actions import world  # noqa: F401 - fixture
from test_trips_module import _trip

pytestmark = pytest.mark.django_db

D = Decimal
TAX = D('10.00')          # fixture only: not a real rate
FEE = D('1.50')           # fixture only: not a real fee


@pytest.fixture
def us(world, geo):  # noqa: F811
    """Austin: an additive-tax zone with a per-trip fee, and a rate card carrying a tax percent."""
    aus = world['aus']
    aus.tax_mode, aus.per_trip_fee, aus.per_trip_fee_label = 'additive', FEE, 'Regulatory fee'
    aus.save()
    sedan, _ = VehicleType.objects.get_or_create(type='sedan')
    RateCard.objects.create(
        zone=aus, vehicle_type=sedan, version=1, is_active=True,
        effective_from=timezone.now() - timedelta(days=30), base_fare=D('5.00'),
        per_km_fare=D('2.00'), per_min_fare=D('0.50'), min_fare=D('3.00'), gst_percent=TAX)
    client, _ = operator('city_ops_manager', geo, scope_code='US-TX-AUS')
    return {'world': world, 'client': client, 'sedan': sedan, 'aus': aus}


def us_started(us, *, km=2, fare=2_500, method='online', minutes=33, **extra):
    """A started Austin trip flagged stale; the driver was last seen 3 minutes after it began."""
    world = us['world']
    now = world['now']
    trip = _trip(world['aus_rider'], world['aus'], status='in_progress',
                 driver=world['aus_driver'], fare=fare, method=method, vehicle_type=us['sedan'],
                 requested_at=now - timedelta(minutes=45), accepted_at=now - timedelta(minutes=40),
                 reached_at=now - timedelta(minutes=35), started_at=now - timedelta(minutes=33),
                 last_driver_activity_at=now - timedelta(minutes=30),
                 stale_flagged_at=now - timedelta(minutes=5), stale_reason='driver_silent_long',
                 commission_percent_snapshot=D('20.00'), **extra)
    trail(trip, km)
    return trip


def row(trip):
    return FarePricing.objects.get(trip_id=trip, snapshot_type=FarePricing.SNAPSHOT_OPERATOR_END)


class TestStandardRuleUnderTheCap:
    def test_tax_and_fee_are_on_the_amount_charged(self, us):
        trip = us_started(us, km=2)                       # 5.00 + 2 km * 2.00 + 3 min * 0.50 = 10.50
        data = release(us['client'], trip, 'driver_unresponsive').json()['data']
        # base 10.50, tax 10 percent = 1.05, fee 1.50: 13.05 charged, within the 25.00 quote.
        assert (data['pretax_fare_minor'], data['tax_minor'], data['per_trip_fee_minor']) == (
            1_050, 105, 150)
        assert data['capped_fare_minor'] == 1_305 == data['rider_pays_minor']
        assert data['cap_applied'] is False and data['regulatory_fee_expense_minor'] == 0
        assert 'blocker #15' in data['tax_note']
        trip.refresh_from_db()
        assert trip.final_fare_minor == 1_305
        snap = row(trip)
        assert (snap.pretax_fare_minor, snap.tax_minor, snap.per_trip_fee_minor) == (1_050, 105, 150)
        assert snap.pretax_fare_minor + snap.tax_minor + snap.per_trip_fee_minor == snap.gross_fare_minor
        assert snap.rider_payable_minor == 1_305
        assert not PlatformExpense.objects.exists()

    def test_the_preview_shows_exactly_what_the_release_applies(self, us):
        trip = us_started(us, km=2)
        shown = preview(us['client'], trip, 'other').json()['data']
        done = release(us['client'], trip, 'other').json()['data']
        for key in ('pretax_fare_minor', 'tax_minor', 'per_trip_fee_minor', 'rider_pays_minor',
                    'capped_fare_minor'):
            assert shown[key] == done[key], key

    def test_the_online_payment_is_for_the_charged_total(self, us):
        from servers.payments.models import Payment

        trip = us_started(us, km=2)
        release(us['client'], trip, 'other')
        assert Payment.objects.get(trip_id=trip).amount_minor == 1_305   # tax and fee inside it


class TestCapBinding:
    def test_the_total_is_held_at_the_upfront_price_and_the_base_backed_out(self, us):
        trip = us_started(us, km=2, fare=1_200)           # the quote is 12.00; 13.05 would pass it
        data = release(us['client'], trip, 'other').json()['data']
        assert data['uncapped_fare_minor'] == 1_305 and data['capped_fare_minor'] == 1_200
        assert data['cap_applied'] is True and data['rider_pays_minor'] == 1_200
        # base' = (12.00 - 1.50) / 1.10 = 9.545..., rounded to 9.55; tax takes the rounding cent.
        assert (data['pretax_fare_minor'], data['tax_minor'], data['per_trip_fee_minor']) == (
            955, 95, 150)
        assert data['pretax_fare_minor'] + data['tax_minor'] + data['per_trip_fee_minor'] == 1_200
        snap = row(us_trip_of(data))
        assert snap.gross_fare_minor == 1_200 and snap.total_fare_minor == 1_305

    def test_the_formula_in_isolation(self):
        for cap in (1_200, 1_000, 999, 301, 151):
            base, tax, fee, total = operator_end.tax_on_charged(
                D('10.50'), TAX, FEE, D(cap) / 100, 'USD')
            assert total == D(cap) / 100 and base + tax + fee == total
            assert base >= 0 and tax >= 0 and fee <= total

    def test_a_cap_below_the_fee_charges_only_the_cap(self):
        base, tax, fee, total = operator_end.tax_on_charged(D('10.50'), TAX, FEE, D('1.00'), 'USD')
        assert (base, tax, fee, total) == (D('0.00'), D('0.00'), D('1.00'), D('1.00'))

    def test_under_the_cap_nothing_is_backed_out(self):
        base, tax, fee, total = operator_end.tax_on_charged(D('10.50'), TAX, FEE, D('99.00'), 'USD')
        assert (base, tax, fee, total) == (D('10.50'), D('1.05'), D('1.50'), D('13.05'))


def us_trip_of(data):
    from servers.ride.models import Trip

    return Trip.objects.get(pk=data['trip_id'])


class TestPromo:
    def test_the_promo_discount_lowers_the_taxable_amount(self, us):
        from servers.promotions.models import Campaign
        from servers.ride.models import PromoCode
        from servers.ride.promos import redeem_promo

        now = timezone.now()
        promo = PromoCode.objects.create(
            code='USD2', discount_type='flat', discount_flat_minor=200, currency='USD',
            max_per_user_redemptions=5, valid_from=now - timedelta(days=1),
            valid_to=now + timedelta(days=5))
        Campaign.objects.create(
            kind='rider_promo', name='USD2', state='live', country='US', currency='USD',
            budget_minor=100_000, starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=5),
            promo_code=promo, config={'code': 'USD2', 'discount_type': 'flat'})
        trip = us_started(us, km=2, method='online')
        assert redeem_promo('USD2', trip.user_id, trip, D(trip.estimated_fare),
                            zone=trip.zone).ok
        data = release(us['client'], trip, 'other').json()['data']
        # AUDIT 27.59 ruling 2 (was: tax on the fare before the promo, 1.05): the discount is the
        # module 12 discount on the capped fare (2.00), and the tax is now on what the rider is
        # charged: 10% of (10.50 - 2.00) = 0.85, so 0.20 less. The fee (1.50) is unchanged.
        assert data['capped_fare_minor'] == 1_305
        assert (data['pretax_fare_minor'], data['tax_minor'], data['per_trip_fee_minor']) == (
            1_050, 85, 150)
        assert data['tax_relief_minor'] == 20
        assert data['promo_discount_minor'] == 200
        assert data['rider_pays_minor'] == 1_285                  # 10.50 + 0.85 + 1.50
        assert data['rider_pays_after_promo_minor'] == 1_085      # (10.50 - 2.00) + 0.85 + 1.50


class TestPlatformFunded:
    @pytest.mark.parametrize('code', ['safety_incident', 'platform_fault'])
    def test_the_rider_pays_nothing_no_tax_no_fee_and_the_fee_is_the_platforms(self, us, code):
        trip = us_started(us, km=2)
        data = release(us['client'], trip, code).json()['data']
        assert data['rider_pays_minor'] == 0 and data['funded_by'] == 'platform'
        assert (data['pretax_fare_minor'], data['tax_minor'], data['per_trip_fee_minor']) == (0, 0, 0)
        assert data['regulatory_fee_expense_minor'] == 150
        trip.refresh_from_db()
        assert trip.final_fare_minor == 0 and trip.payment_status == 'waived'
        snap = row(trip)
        assert (snap.tax_minor, snap.per_trip_fee_minor, snap.rider_payable_minor) == (0, 0, 0)
        expense = PlatformExpense.objects.get()
        assert expense.kind == 'platform_regulatory_fee_expense' == PlatformExpense.REGULATORY_FEE
        assert (expense.trip_id, expense.amount_minor, expense.currency) == (trip.pk, 150, 'USD')
        assert 'blocker 15' in expense.note

    def test_the_expense_is_on_no_driver_and_no_rider_wallet(self, us):
        world = us['world']
        trip = us_started(us, km=2)
        release(us['client'], trip, 'platform_fault')
        assert not WalletTransaction.objects.filter(
            user_id__in=[world['aus_rider'], world['aus_driver'].user_id],
            idempotency_key__contains='REGULATORY').exists()
        assert not WalletTransaction.objects.filter(purpose__icontains='regulatory').exists()
        # The driver is credited the platform-funded earnings as before, and nothing else.
        purposes = set(WalletTransaction.objects.filter(
            user_id=world['aus_driver'].user_id).values_list('purpose', flat=True))
        assert purposes == {'platform_funded_trip_end'}

    def test_booking_the_fee_twice_is_one_row(self, us):
        trip = us_started(us, km=2)
        release(us['client'], trip, 'platform_fault')
        plan = operator_end.build_plan(trip, 'platform_fault')
        assert operator_end._book_regulatory_fee(trip, plan) == PlatformExpense.objects.get().pk
        assert PlatformExpense.objects.count() == 1

    def test_the_finance_overview_lists_it_in_the_platform_funded_section(self, geo, us):
        trip = us_started(us, km=2)
        release(us['client'], trip, 'platform_fault')
        finance, _ = operator('finance', geo, scope_code='US-TX-AUS')
        today = timezone.now().date().isoformat()
        r = finance.get(f'/api/v1/payments/admin/overview/?from={today}&to={today}')
        assert r.status_code == 200, r.content
        block = r.json()['data']['platform_funded_trip_ends']
        fees = block['regulatory_fee_expenses']
        assert fees['kind'] == 'platform_regulatory_fee_expense'
        assert fees['totals'] == [{'currency': 'USD', 'count': 1, 'amount_minor': 150}]
        assert fees['rows'] == [{'trip_id': trip.pk, 'amount_minor': 150, 'currency': 'USD'}]
        assert 'blocker 15' in fees['note']

    def test_a_zone_with_no_fee_books_no_expense(self, us):
        us['aus'].per_trip_fee = D('0.00')
        us['aus'].save()
        trip = us_started(us, km=2)
        data = release(us['client'], trip, 'platform_fault').json()['data']
        assert data['regulatory_fee_expense_minor'] == 0 and not PlatformExpense.objects.exists()


class TestIndiaIsUnchanged:
    def test_inclusive_tax_has_no_split_no_expense_and_the_same_totals(
            self, geo, world, card):  # noqa: F811
        client, _ = operator('city_ops_manager', geo)
        trip = started(world, card, km=2)
        data = release(client, trip, 'driver_unresponsive').json()['data']
        assert data['uncapped_fare_minor'] == 4_900 and data['capped_fare_minor'] == 4_900
        assert data['rider_pays_minor'] == 4_900
        assert (data['pretax_fare_minor'], data['tax_minor'], data['per_trip_fee_minor']) == (
            None, None, None)
        assert data['tax_note'] is None and data['regulatory_fee_expense_minor'] == 0
        snap = row(trip)
        assert (snap.pretax_fare_minor, snap.tax_minor, snap.per_trip_fee_minor) == (
            None, None, None)
        assert snap.gross_fare_minor == 4_900 and snap.rider_payable_minor == 4_900

    def test_india_platform_funded_books_no_expense(self, geo, world, card):  # noqa: F811
        client, _ = operator('city_ops_manager', geo)
        trip = started(world, card, km=2)
        data = release(client, trip, 'platform_fault').json()['data']
        assert data['rider_pays_minor'] == 0 and data['driver_earnings_minor'] == 3_920
        assert not PlatformExpense.objects.exists()
        assert (row(trip).tax_minor, row(trip).per_trip_fee_minor) == (None, None)

    def test_the_standard_us_total_matches_the_old_arithmetic(self, us):
        """No tax rate or fee changed: the charged total is exactly what the old code charged."""
        trip = us_started(us, km=2)
        data = release(us['client'], trip, 'other').json()['data']
        assert data['uncapped_fare_minor'] == data['capped_fare_minor'] == 1_305
