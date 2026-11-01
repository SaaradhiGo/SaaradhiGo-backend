"""US sales tax and regulatory fees are PASS-THROUGH (owner ruling, AUDIT 27.62 item 1).

    "US tax and regulatory fees are pass-through: collected from the rider, remitted, never part of
    driver earnings; commission is calculated on the fare before tax and fees."

TEST-ONLY NUMBERS (go-live blocker #15, US accountant to confirm): the 8.25 percent tax and the 0.50
fee are the brief's worked example; no rate is decided in the code.

THE WORKED EXAMPLE (fare 20.00, tax 8.25%, fee 0.50, commission 25%)
  rider pays      20.00 + 1.65 + 0.50 = 22.15
  driver          gross 20.00, commission 5.00, net 15.00
  pass-through    tax 1.65 + fee 0.50, owed by the platform, in nobody's earnings
  cash            wallet -5.00 -1.65 -0.50 = -7.15 ; cash in hand 22.15 ; 22.15 - 7.15 = 15.00 net
WITH A 5.00 PLATFORM-FUNDED PROMO
  rider pays      (20.00 - 5.00) + 1.24 + 0.50 = 16.74 ; driver gross 20.00, commission 5.00, net 15.00
  cash            wallet +5.00 -5.00 -1.24 -0.50 = -1.74 ; cash in hand 16.74 ; 16.74 - 1.74 = 15.00
EVERY TRIP     rider paid = driver net + commission + tax + fee - platform-funded promo
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from servers.driver.earnings import settlement_rows, summarise
from servers.driver.utils import credit_driver_wallet
from servers.payments import ledger, reconciliation
from servers.payments.models import TripSettlement
from servers.pricing import promo_tax
from servers.pricing.models import RateCard
from servers.promotions import rider_promos
from servers.promotions.models import DISCOUNT_FUNDED, LedgerEntry
from servers.reports import compute
from servers.reports.models import DailyCityMetrics
from servers.ride.models import FarePricing, Trip
from servers.ride.promos import redeem_promo
from servers.rider.models import Wallet, WalletTransaction
from test_operator_end_us_tax import row, us, us_started  # noqa: F401 - fixtures and helpers
from test_rulings_2759 import (  # noqa: F401 - fixtures and helpers
    driver_wallet, launch_promo, us825, us_trip,
)
from test_sos_support_zones_matrix import geo, operator  # noqa: F401 - fixtures
from test_stale_started_release import card, release, started  # noqa: F401
from test_stale_trip_actions import world  # noqa: F401 - fixture

pytestmark = pytest.mark.django_db

D = Decimal
TAX_LABEL = 'Sales tax collected (remitted by SaaradhiGo)'
FEE_LABEL = 'Regulatory fee collected (remitted by SaaradhiGo)'


def settle(us825, *, method, promo=False, fare=2215):
    """A US trip as booking leaves it (22.15 = 20.00 + 1.65 + 0.50), optionally with the 5.00 promo."""
    if promo:
        launch_promo()
    trip = us_trip(us825, fare=fare, method=method)
    if promo:
        assert redeem_promo('FIVE', trip.user_id, trip, D('22.15'), zone=trip.zone).ok
        trip.refresh_from_db()
    credit_driver_wallet(trip)
    return trip, TripSettlement.objects.get(trip=trip)


def lines(trip):
    """The driver-wallet ledger rows of one trip, `{purpose: signed minor}`."""
    out = {}
    for t in WalletTransaction.objects.filter(reference_id=f'TRIP_{trip.id}'):
        out[t.purpose] = out.get(t.purpose, 0) + (t.amount_minor if t.txn_type == 'credit'
                                                  else -t.amount_minor)
    return out


class TestTheWorkedExample:
    def test_online_the_rider_pays_22_15_and_the_driver_nets_15_00(self, us825):
        trip, s = settle(us825, method='online')
        assert (s.gross_fare_minor, s.commission_amount_minor, s.driver_net_minor) == (
            2_000, 500, 1_500)
        assert (s.tax_collected_minor, s.regulatory_fee_collected_minor) == (165, 50)
        assert rider_promos.payable_minor(trip) == 2_215 == 2_000 + 165 + 50
        # tax and fee never touch the driver's wallet on an online trip: only the net is credited
        assert lines(trip) == {'trip_earnings': 1_500}
        assert driver_wallet(us825['world']['aus_driver']).balance_minor == 1_500

    def test_cash_the_wallet_owes_commission_tax_and_fee_as_three_lines(self, us825):
        trip, s = settle(us825, method='cash')
        assert (s.gross_fare_minor, s.commission_amount_minor, s.driver_net_minor) == (
            2_000, 500, 1_500)
        assert lines(trip) == {'trip_commission': -500, 'tax_collected': -165,
                               'regulatory_fee_collected': -50}
        wallet = driver_wallet(us825['world']['aus_driver']).balance_minor
        assert wallet == -500 - 165 - 50 == -715
        cash_in_hand = 2_215
        assert cash_in_hand + wallet == s.driver_net_minor == 1_500

    def test_with_a_5_00_platform_funded_promo_online(self, us825):
        trip, s = settle(us825, method='online', promo=True)
        assert (s.gross_fare_minor, s.commission_amount_minor, s.driver_net_minor) == (
            2_000, 500, 1_500)
        assert (s.tax_collected_minor, s.regulatory_fee_collected_minor) == (124, 50)
        assert rider_promos.payable_minor(trip) == 1_674 == 1_500 + 124 + 50
        entry = LedgerEntry.objects.get(kind=DISCOUNT_FUNDED, trip=trip)
        assert entry.amount_minor == 500                       # the promo cost
        assert lines(trip) == {'trip_earnings': 1_500}

    def test_with_a_5_00_platform_funded_promo_cash(self, us825):
        trip, s = settle(us825, method='cash', promo=True)
        assert (s.tax_collected_minor, s.regulatory_fee_collected_minor) == (124, 50)
        assert lines(trip) == {'promo_discount_platform_funded': 500, 'trip_commission': -500,
                               'tax_collected': -124, 'regulatory_fee_collected': -50}
        wallet = driver_wallet(us825['world']['aus_driver']).balance_minor
        assert wallet == 500 - 500 - 124 - 50 == -174
        cash_in_hand = rider_promos.payable_minor(trip)
        assert cash_in_hand == 1_674
        assert cash_in_hand + wallet == s.driver_net_minor == 1_500
        assert LedgerEntry.objects.get(kind=DISCOUNT_FUNDED, trip=trip).amount_minor == 500

    @pytest.mark.parametrize('method', ['online', 'cash'])
    def test_settling_twice_writes_the_pass_through_once(self, us825, method):
        trip, _ = settle(us825, method=method)
        before = driver_wallet(us825['world']['aus_driver']).balance_minor
        credit_driver_wallet(trip)
        assert driver_wallet(us825['world']['aus_driver']).balance_minor == before
        assert TripSettlement.objects.filter(trip=trip).count() == 1
        assert WalletTransaction.objects.filter(purpose='tax_collected').count() == (
            1 if method == 'cash' else 0)

    def test_a_booked_quote_split_is_what_settles(self, us825):
        """Booking stores the quote's split; settlement uses its pre-tax fare and fee as recorded."""
        trip = us_trip(us825, fare=2_215)
        FarePricing.objects.create(
            trip_id=trip, base_fare=D('5.00'), distance_fare=D('10.00'), time_fare=D('5.00'),
            surge_multiplier=D('1.00'), total_fare=D('22.15'),
            pretax_fare_minor=2_000, tax_minor=165, per_trip_fee_minor=50)
        credit_driver_wallet(trip)
        s = TripSettlement.objects.get(trip=trip)
        assert (s.gross_fare_minor, s.tax_collected_minor, s.regulatory_fee_collected_minor) == (
            2_000, 165, 50)


class TestTheSplit:
    def test_the_split_always_adds_back_to_the_charge(self, us825):
        for fare in (1_000, 1_315, 2_215, 2_899, 5_555):
            trip = us_trip(us825, fare=fare)
            split = promo_tax.settlement_split(trip, fare, 'USD')
            assert split['pretax'] + split['tax'] + split['fee'] == fare

    def test_an_inclusive_zone_has_no_split(self, world):  # noqa: F811
        trip = world['ghost'](status='in_progress', method='online')
        assert promo_tax.settlement_split(trip, 20_000, 'INR') is None

    def test_a_promo_that_took_the_taxable_amount_to_zero_leaves_the_fee_only(self, us825):
        launch_promo(flat_minor=2_215)
        trip = us_trip(us825)
        redeem_promo('FIVE', trip.user_id, trip, D('22.15'), zone=trip.zone)
        trip.refresh_from_db()
        credit_driver_wallet(trip)
        s = TripSettlement.objects.get(trip=trip)
        # the rider paid 0, the driver still earns on the 20.00 fare, no tax was charged
        assert rider_promos.payable_minor(trip) == 0
        assert (s.gross_fare_minor, s.tax_collected_minor, s.regulatory_fee_collected_minor) == (
            2_000, 0, 50)
        assert s.driver_net_minor == 1_500


@pytest.mark.parametrize('method', ['online', 'cash'])
@pytest.mark.parametrize('promo', [False, True])
def test_the_ledger_balances_on_every_trip(us825, method, promo):
    """rider paid = driver net + commission + tax + fee - platform-funded promo."""
    trip, s = settle(us825, method=method, promo=promo)
    promo_cost = sum(LedgerEntry.objects.filter(kind=DISCOUNT_FUNDED, trip=trip)
                     .values_list('amount_minor', flat=True))
    rider_paid = rider_promos.payable_minor(trip)
    assert rider_paid == (s.driver_net_minor + s.commission_amount_minor + s.tax_collected_minor
                          + s.regulatory_fee_collected_minor - promo_cost)
    assert s.gross_fare_minor == s.driver_net_minor + s.commission_amount_minor
    # and the driver's side: cash in hand plus the wallet is exactly the net
    if method == 'cash':
        wallet = driver_wallet(us825['world']['aus_driver']).balance_minor
        assert rider_paid + wallet == s.driver_net_minor


class TestPlatformPaidEndEqualsANormalTrip:
    def card_12(self, us825):
        # 5.00 + 2 km x 2.75 + 3 min x 0.50 = 12.00 before tax (rate cards are immutable: a new one)
        aus = us825['aus']
        RateCard.objects.filter(zone=aus).delete()
        RateCard.objects.create(
            zone=aus, vehicle_type=us825['sedan'], version=1, is_active=True,
            effective_from=timezone.now() - timedelta(days=30), base_fare=D('5.00'),
            per_km_fare=D('2.75'), per_min_fare=D('0.50'), min_fare=D('3.00'), gst_percent=D('8.25'))

    def test_a_12_00_distance_fare_nets_9_00_the_same_as_a_normal_12_00_trip(self, us825):
        self.card_12(us825)
        pf = us_started(us825, km=2, fare=2_500)
        Trip.objects.filter(pk=pf.pk).update(commission_percent_snapshot=D('25.00'))
        pf.refresh_from_db()
        data = release(us825['client'], pf, 'platform_fault').json()['data']
        assert data['driver_earnings_base_minor'] == 1_200
        assert data['commission_minor'] == 300
        assert data['driver_earnings_minor'] == 900
        # a normal completed US trip of the same 12.00 fare: 12.00 + 0.99 + 0.50 = 13.49 charged
        normal, s = settle(us825, method='online', fare=1_349)
        assert (s.gross_fare_minor, s.tax_collected_minor, s.regulatory_fee_collected_minor) == (
            1_200, 99, 50)
        assert s.driver_net_minor == data['driver_earnings_minor'] == 900

    def test_the_platform_pays_no_tax_or_fee_to_the_driver(self, us825):
        self.card_12(us825)
        pf = us_started(us825, km=2, fare=2_500)
        Trip.objects.filter(pk=pf.pk).update(commission_percent_snapshot=D('25.00'))
        pf.refresh_from_db()
        release(us825['client'], pf, 'platform_fault')
        credits = WalletTransaction.objects.filter(reference_id=f'TRIP_{pf.id}')
        assert [(c.purpose, c.amount_minor) for c in credits] == [('platform_funded_trip_end', 900)]
        assert not TripSettlement.objects.filter(trip=pf).exists()      # no settlement by design


class TestStandardOperatorEnd:
    def test_the_standard_end_settles_like_a_normal_trip_of_the_same_pretax_fare(self, us825):
        trip = us_started(us825, km=2, method='cash')
        Trip.objects.filter(pk=trip.pk).update(commission_percent_snapshot=D('25.00'))
        trip.refresh_from_db()
        shown = release.__globals__['preview'](us825['client'], trip, 'other').json()['data']
        done = release(us825['client'], trip, 'other').json()['data']
        assert shown['driver_earnings_minor'] == done['driver_earnings_minor']
        s = TripSettlement.objects.get(trip=trip)
        # 10.50 before tax, 0.87 tax, 0.50 fee -> 11.87 charged; commission 25% of 10.50 = 2.63
        assert (s.gross_fare_minor, s.tax_collected_minor, s.regulatory_fee_collected_minor) == (
            1_050, 87, 50)
        assert (s.commission_amount_minor, s.driver_net_minor) == (263, 787)
        assert done['driver_earnings_minor'] == s.driver_net_minor
        assert done['rider_pays_minor'] == 1_187 == s.gross_fare_minor + 87 + 50
        # a normal trip of the same fare (11.87 charged) nets the same
        again, s2 = settle(us825, method='online', fare=1_187)
        assert s2.driver_net_minor == 787

    def test_the_reconciliation_does_not_flag_a_pass_through_settlement(self, us825):
        trip = us_started(us825, km=2, method='cash')
        release(us825['client'], trip, 'other')
        s = TripSettlement.objects.get(trip=trip)
        trip.refresh_from_db()
        assert s.gross_fare_minor != trip.final_fare_minor          # pre-tax versus full charge
        from servers.payments.models import ReconciliationItem

        class Run:
            now = timezone.now()
            checks = {}
            raised = []

            def raise_item(self, kind, *a, **k):
                self.raised.append(kind)

        r = Run()
        reconciliation._check_settlements(
            r, 'US', 'USD', timezone.now() - timedelta(days=1), timezone.now() + timedelta(days=1))
        assert 'settlement_gross_mismatch' not in r.raised and not ReconciliationItem.objects.exists()


class TestIndiaIsUnchanged:
    @pytest.mark.parametrize('method', ['online', 'cash'])
    def test_golden_settlement(self, world, method):  # noqa: F811
        trip = world['ghost'](status='in_progress', method=method)
        Trip.objects.filter(pk=trip.pk).update(
            estimated_fare_minor=20_000, final_fare_minor=None,
            commission_percent_snapshot=D('20.00'))
        trip.refresh_from_db()
        credit_driver_wallet(trip)
        s = TripSettlement.objects.get(trip=trip)
        assert (s.gross_fare_minor, s.commission_amount_minor, s.driver_net_minor) == (
            20_000, 4_000, 16_000)
        assert (s.tax_collected_minor, s.regulatory_fee_collected_minor) == (None, None)
        want = {'trip_earnings': 16_000} if method == 'online' else {'trip_commission': -4_000}
        assert lines(trip) == want

    def test_golden_settlement_with_a_promo(self, world):  # noqa: F811
        from servers.promotions.models import Campaign
        from servers.ride.models import PromoCode

        now = timezone.now()
        promo = PromoCode.objects.create(
            code='FIFTY', discount_type='flat', discount_flat_minor=5_000, currency='INR',
            max_per_user_redemptions=5, valid_from=now - timedelta(days=1),
            valid_to=now + timedelta(days=5))
        Campaign.objects.create(
            kind='rider_promo', name='FIFTY', state='live', country='IN', currency='INR',
            budget_minor=100_000, starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=5),
            promo_code=promo, config={'code': 'FIFTY', 'discount_type': 'flat'})
        trip = world['ghost'](status='in_progress', method='cash')
        Trip.objects.filter(pk=trip.pk).update(
            estimated_fare_minor=20_000, final_fare_minor=None,
            commission_percent_snapshot=D('20.00'))
        trip.refresh_from_db()
        assert redeem_promo('FIFTY', trip.user_id, trip, D('200.00'), zone=trip.zone).ok
        credit_driver_wallet(trip)
        s = TripSettlement.objects.get(trip=trip)
        assert (s.gross_fare_minor, s.commission_amount_minor, s.driver_net_minor) == (
            20_000, 4_000, 16_000)
        assert s.tax_collected_minor is None
        assert lines(trip) == {'trip_commission': -4_000, 'promo_discount_platform_funded': 5_000}

    def test_golden_operator_end(self, geo, world, card):  # noqa: F811
        client, _ = operator('city_ops_manager', geo)
        trip = started(world, card, km=2, method='cash')
        data = release(client, trip, 'other').json()['data']
        assert data['capped_fare_minor'] == data['rider_pays_minor'] == 4_900
        assert data['commission_minor'] == 980 and data['driver_earnings_minor'] == 3_920
        assert data['driver_earnings_base_minor'] is None
        s = TripSettlement.objects.get(trip=trip)
        assert (s.gross_fare_minor, s.driver_net_minor, s.tax_collected_minor) == (4_900, 3_920, None)


# ---------------------------------------------------------------------------
# What the driver is shown
# ---------------------------------------------------------------------------

class TestDriverEarnings:
    def rows(self, us825):
        return settlement_rows(us825['world']['aus_driver'])

    def test_tax_and_fee_are_never_in_the_earnings(self, us825):
        settle(us825, method='online')
        (r,) = self.rows(us825)
        api = r.to_api()
        assert (api['amount'], api['commission'], api['net_amount']) == ('20.00', '5.00', '15.00')
        assert (api['tax_collected'], api['regulatory_fee_collected']) == ('1.65', '0.50')
        assert api['deductions'] == []                          # online: nothing touches the driver
        totals = summarise([r])
        assert (totals['total_gross'], totals['total_commission'], totals['total_net']) == (
            D('20.00'), D('5.00'), D('15.00'))
        assert (totals['tax_collected'], totals['regulatory_fees_collected']) == (D('1.65'), D('0.50'))
        assert totals['cash_collected'] == D('0.00')

    def test_a_cash_trip_shows_the_collected_and_owed_lines_as_deductions(self, us825):
        settle(us825, method='cash')
        (r,) = self.rows(us825)
        api = r.to_api()
        assert (api['amount'], api['commission'], api['net_amount']) == ('20.00', '5.00', '15.00')
        assert api['cash_in_hand'] is True and api['cash_to_collect'] == '22.15'
        assert api['deductions'] == [
            {'kind': 'tax_collected', 'label': TAX_LABEL, 'amount': '1.65'},
            {'kind': 'regulatory_fee_collected',
             'label': 'Regulatory fee collected (remitted by SaaradhiGo)', 'amount': '0.50'}]
        assert summarise([r])['cash_collected'] == D('22.15')

    def test_the_fee_line_uses_the_zones_own_fee_name(self, us825):
        zone = us825['aus']
        zone.per_trip_fee_label = 'Airport fee'
        zone.save()
        settle(us825, method='cash')
        (r,) = self.rows(us825)
        assert r.to_api()['deductions'][1]['label'] == 'Airport fee collected (remitted by SaaradhiGo)'

    def test_the_endpoints_never_put_tax_in_earned(self, us825):
        from rest_framework.test import APIClient

        settle(us825, method='cash', promo=True)
        driver = us825['world']['aus_driver']
        api = APIClient()
        api.force_authenticate(user=driver.user_id)
        summary = api.get('/api/v1/driver/earnings/summary/').json()['data']
        assert (summary['total_earned'], summary['total_commission'], summary['total_net']) == (
            '20.00', '5.00', '15.00')
        assert (summary['tax_collected'], summary['regulatory_fees_collected']) == ('1.24', '0.50')
        assert summary['cash_collected'] == '16.74'
        listing = api.get('/api/v1/driver/earnings/').json()['data']['results']
        assert listing[0]['amount'] == '20.00' and listing[0]['net_amount'] == '15.00'

    def test_an_old_row_reads_as_it_did(self, us825):
        """A settlement written before the rule (the new fields NULL) is not re-settled: its gross is
        what it was."""
        trip, s = settle(us825, method='online')
        TripSettlement.objects.filter(pk=s.pk).update(
            gross_fare_minor=2_215, commission_amount_minor=554, driver_net_minor=1_661,
            tax_collected_minor=None, regulatory_fee_collected_minor=None)
        (r,) = self.rows(us825)
        api = r.to_api()
        assert api['tax_collected'] is None and api['deductions'] == []
        assert api['amount'] == '22.15'                         # the trip's fare, as the old reading


class TestTheStatement:
    def test_a_cash_trip_has_labelled_lines_that_close_on_the_balance(self, us825):
        trip, s = settle(us825, method='cash')
        driver = us825['world']['aus_driver']
        Wallet.objects.filter(user_id=driver.user_id, scope=Wallet.SCOPE_DRIVER).update(currency='USD')
        start = timezone.now() - timedelta(days=1)
        end = timezone.now() + timedelta(days=1)
        statement = ledger.build_statement(driver, start, end)
        assert statement['unexplained_minor'] == 0
        assert statement['closing_minor'] == statement['balance_minor'] == -715
        entries = ledger.entry_dicts(driver, statement['rows'], statement['opening_minor'], 0, 50)
        by_purpose = {e['purpose']: e for e in entries}
        assert by_purpose['tax_collected']['label'] == TAX_LABEL
        assert by_purpose['tax_collected']['amount_minor'] == -165
        assert by_purpose['regulatory_fee_collected']['label'] == (
            'Regulatory fee collected (remitted by SaaradhiGo)')
        assert by_purpose['regulatory_fee_collected']['kind'] == 'regulatory_fee_collected'
        settlement = by_purpose['trip_commission']['settlement']
        assert settlement['gross_fare_minor'] == 2_000 and settlement['tax_collected_minor'] == 165
        assert entries[-1]['running_balance_minor'] == -715

    def test_the_new_purposes_belong_to_the_drivers_ledger(self):
        from servers.payments.scope import DRIVER_LEDGER_PURPOSES

        assert {'tax_collected', 'regulatory_fee_collected'} <= set(DRIVER_LEDGER_PURPOSES)


# ---------------------------------------------------------------------------
# Finance overview and reports
# ---------------------------------------------------------------------------

class TestFinanceOverview:
    def test_the_pass_through_section_is_per_currency_and_by_kind(self, us825, geo):
        settle(us825, method='online')
        settle(us825, method='cash', promo=True)
        client, _ = operator('super_admin', geo)
        today = timezone.now().date()
        resp = client.get('/api/v1/payments/admin/overview/'
                          f'?from={today - timedelta(days=1)}&to={today + timedelta(days=1)}')
        assert resp.status_code == 200, resp.content[:400]
        section = resp.json()['data']['tax_and_fees_collected']
        assert section['title'] == 'Tax and fees collected (pass-through)'
        (usd,) = section['by_currency']
        assert usd['currency'] == 'USD'
        kinds = {k['kind']: k['amount_minor'] for k in usd['by_kind']}
        assert kinds == {'tax_collected': 165 + 124, 'regulatory_fee_collected': 100}
        assert usd['collected_minor'] == 165 + 124 + 100
        # and nothing of it is in the gross, commission or driver-net columns
        totals = {t['currency']: t for t in resp.json()['data']['totals']}
        assert totals['USD']['gross_minor'] == 4_000
        assert totals['USD']['commission_minor'] == 1_000 and totals['USD']['driver_net_minor'] == 3_000

    def test_the_settlements_list_carries_the_pass_through(self, us825, geo):
        settle(us825, method='online')
        client, _ = operator('super_admin', geo)
        today = timezone.now().date()
        resp = client.get('/api/v1/payments/admin/settlements/'
                          f'?from={today - timedelta(days=1)}&to={today + timedelta(days=1)}')
        body = resp.json()['data']
        assert body['results'][0]['tax_collected_minor'] == 165
        assert body['totals'][0]['tax_collected_minor'] == 165


class TestReportsRollups:
    def test_the_rollup_totals_equal_the_settlement_rows(self, us825, geo):
        from servers.ride.models import TripStatus

        done, _ = TripStatus.objects.get_or_create(status_code='completed')
        settled = []
        for kwargs in ({'method': 'online'}, {'method': 'cash', 'promo': True}):
            trip, s = settle(us825, **kwargs)
            Trip.objects.filter(pk=trip.pk).update(status_id=done, completed_at=timezone.now())
            settled.append(s)
        city = geo['US-TX-AUS']
        today = timezone.now().date()
        for d in (today - timedelta(days=1), today, today + timedelta(days=1)):
            compute.rebuild_day(city, d)
        rows = DailyCityMetrics.objects.filter(city=city, currency='USD')
        total = lambda f: sum(getattr(r, f) for r in rows)  # noqa: E731
        assert total('gmv_minor') == sum(s.gross_fare_minor for s in settled) == 4_000
        assert total('commission_minor') == sum(s.commission_amount_minor for s in settled) == 1_000
        assert total('driver_net_minor') == sum(s.driver_net_minor for s in settled) == 3_000
        # `rider_paid` is the trip's full charge (`final_fare or estimated_fare`, as ever: the promo is
        # a separate platform-funded line), so it is the GMV plus the tax and fees collected.
        passthrough = sum(s.tax_collected_minor + s.regulatory_fee_collected_minor for s in settled)
        assert total('rider_paid_minor') == total('gmv_minor') + passthrough
        assert total('promo_cost_minor') == 500


class TestMigration:
    def test_the_new_columns_are_nullable_and_additive(self):
        fields = {f.name: f for f in TripSettlement._meta.get_fields()}
        for name in ('tax_collected_minor', 'regulatory_fee_collected_minor'):
            assert fields[name].null is True

    def test_wallets_are_untouched_by_an_inclusive_trip(self, world):  # noqa: F811
        before = Wallet.objects.count()
        trip = world['ghost'](status='in_progress', method='cash')
        Trip.objects.filter(pk=trip.pk).update(
            estimated_fare_minor=10_000, final_fare_minor=None,
            commission_percent_snapshot=D('20.00'))
        trip.refresh_from_db()
        credit_driver_wallet(trip)
        assert not WalletTransaction.objects.filter(
            purpose__in=('tax_collected', 'regulatory_fee_collected')).exists()
        assert Wallet.objects.count() >= before
