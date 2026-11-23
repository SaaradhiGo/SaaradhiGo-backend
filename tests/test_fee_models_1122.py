"""Driver fee models (Srini's ruling, 22 Nov): three plan types in one engine, set per place and
class with inheritance, the driver protections, notice before an increase, and the words drivers and
the console read.

Fares are whole rupees x 100 (minor units). India's legal share: a plan never takes more than 20%
of a day's fares (MVAG, `regulation.commission_cap`).
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from servers.pricing import fee_plans
from servers.pricing.fee_plan_models import DriverFeePlan
from servers.pricing.fee_plan_views import resolved_rows
from test_driver_fee_plans import BASE, india, plan  # noqa: F401 - fixtures and helpers
from test_finance_actions import approve_request, body_of, checker, post
from test_pricing_module import geo, world  # noqa: F401

pytestmark = [pytest.mark.django_db, pytest.mark.no_default_commission]
DAY = date(2026, 11, 20)


def daily(scope, amounts, **kw):
    kw.setdefault('earnings_floor_minor', 0)
    kw.setdefault('daily_cap_minor', None)
    return plan(scope=scope, plan_type='daily_pass', amounts=amounts, **kw)


class TestInheritancePerClass:
    def test_a_class_the_city_does_not_set_comes_from_the_state(self, india):
        DriverFeePlan.objects.all().delete()
        daily('IN-TG', {'auto': {'daily_pass_minor': 2000}})
        daily('IN-TG-HYD', {'sedan': {'daily_pass_minor': 4900}}, version=1)
        scopes = fee_plans.scope_codes_of_zone(india.hyd)

        auto, auto_from = fee_plans.resolve_with_source('IN', scopes, DAY, 'auto')
        sedan, sedan_from = fee_plans.resolve_with_source('IN', scopes, DAY, 'sedan')

        assert auto_from == 'IN-TG' and auto.amounts_for('auto')['daily_pass_minor'] == 2000
        assert sedan_from == 'IN-TG-HYD'

    def test_the_resolved_view_says_where_each_class_comes_from_and_marks_overrides(self, india):
        DriverFeePlan.objects.all().delete()
        daily('IN-TG', {'auto': {'daily_pass_minor': 2000}, 'sedan': {'daily_pass_minor': 3900}})
        daily('IN-TG-HYD', {'sedan': {'daily_pass_minor': 4900}}, version=1)

        rows = {(r['code'], r['vehicle_type']): r for r in resolved_rows('IN', DAY)['rows']}

        hyd_auto = rows[('IN-TG-HYD', 'auto')]
        assert hyd_auto['words'] == '₹20 a day, only on a day with a trip'
        assert hyd_auto['from_code'] == 'IN-TG' and hyd_auto['override'] is False
        hyd_sedan = rows[('IN-TG-HYD', 'sedan')]
        assert hyd_sedan['words'].startswith('₹49 a day') and hyd_sedan['override'] is True

    def test_a_zone_can_set_its_own(self, india):
        DriverFeePlan.objects.all().delete()
        from servers.pricing.models import ServiceZone
        ServiceZone.objects.filter(pk=india.hyd.pk).update(code='IN-TG-HYD-AIRPORT')
        india.hyd.refresh_from_db()
        daily('IN-TG-HYD', {'sedan': {'daily_pass_minor': 4900}})
        daily('IN-TG-HYD-AIRPORT', {'sedan': {'daily_pass_minor': 5900}}, version=1)

        got, source = fee_plans.resolve_with_source('IN', fee_plans.scope_codes_of_zone(india.hyd), DAY, 'sedan')

        assert source == 'IN-TG-HYD-AIRPORT' and got.amounts_for('sedan')['daily_pass_minor'] == 5900


class TestDriverProtections:
    def _plan(self):
        return DriverFeePlan(country='IN', currency='INR', plan_type='daily_pass', earnings_floor_minor=0,
                             amounts={'sedan': {'daily_pass_minor': 4900}})

    def test_no_trip_no_daily_fee(self):
        assert fee_plans.day_fee(self._plan(), 'sedan', [], day=DAY).fee_minor == 0

    def test_a_day_with_a_trip_pays_the_daily_fee(self):
        assert fee_plans.day_fee(self._plan(), 'sedan', [30000, 30000], day=DAY).fee_minor == 4900

    def test_never_more_than_20_percent_of_the_days_fares(self):
        result = fee_plans.day_fee(self._plan(), 'sedan', [15000], day=DAY)
        assert result.fee_minor == 3000 and result.reduced_from_minor == 4900

    def test_a_commission_above_20_percent_is_refused(self, india):
        maker, _ = india.super
        r = post(maker, f'{BASE}/fee-plans/', {
            'country': 'IN', 'scope_code': 'IN-TG-HYD', 'plan_type': 'percent_commission',
            'amounts': {'sedan': {'commission_percent': 25}}, 'reason': 'test'}, expect=201)
        r = post(maker, f"{BASE}/fee-plans/{body_of(r)['id']}/submit/", {'reason': 'try'}, expect=400)
        assert r.json()['error']['code'] in ('ABOVE_COMMISSION_CAP', 'BREAKS_LEGAL_SHARE')

    def test_a_commission_plan_snapshots_its_own_capped_percentage(self, india):
        DriverFeePlan.objects.all().delete()
        plan(plan_type='percent_commission', amounts={'sedan': {'commission_percent': 12}})
        snap = fee_plans.snapshot_for_booking(india.hyd, 'sedan')
        assert snap['commission_percent_snapshot'] == Decimal('12.00')


class TestNoticeBeforeAnIncrease:
    def _activate(self, india, amounts):
        maker, _ = india.super
        r = post(maker, f'{BASE}/fee-plans/', {
            'country': 'IN', 'scope_code': 'IN-TG-HYD', 'plan_type': 'daily_pass', 'amounts': amounts,
            'earnings_floor_minor': 0, 'reason': 'Daily fee'}, expect=201)
        pid = body_of(r)['id']
        r = post(maker, f'{BASE}/fee-plans/{pid}/submit/', {'reason': 'ruling'}, expect=202)
        chk, _ = checker()
        assert approve_request(chk, body_of(r)['approval_request']['id']).status_code == 200
        return DriverFeePlan.objects.get(pk=pid)

    def test_an_increase_starts_after_seven_days(self, india):
        new = self._activate(india, {'sedan': {'daily_pass_minor': 4900}})
        assert new.effective_from == fee_plans.local_day('IN') + timedelta(days=7)

    def test_a_cut_starts_the_next_day(self, india):
        DriverFeePlan.objects.all().delete()
        daily('IN-TG-HYD', {'sedan': {'daily_pass_minor': 9900}})
        new = self._activate(india, {'sedan': {'daily_pass_minor': 4900}})
        assert new.effective_from == fee_plans.local_day('IN') + timedelta(days=1)


class TestWords:
    def test_the_driver_line_for_a_daily_fee(self):
        p = DriverFeePlan(country='IN', currency='INR', plan_type='daily_pass',
                          amounts={'sedan': {'daily_pass_minor': 4900}})
        assert fee_plans._line_words(p, 'sedan') == '₹49 daily fee, charged only if you complete a trip'

    def test_plan_words_for_each_type(self):
        per_trip = DriverFeePlan(country='IN', currency='INR', plan_type='fair_share', daily_cap_minor=14900,
                                 amounts={'sedan': {'per_trip_fee_minor': 1000}}, waiver_first_days=60)
        commission = DriverFeePlan(country='IN', currency='INR', plan_type='percent_commission',
                                   amounts={'*': {'commission_percent': 10}})
        undecided = DriverFeePlan(country='IN', currency='INR', plan_type='daily_pass', amounts={'auto': {}})
        assert fee_plans.plan_words(per_trip, 'sedan') == '₹10 a trip, at most ₹149 a day; first 60 days free'
        assert fee_plans.plan_words(commission, 'suv') == '10% of each fare'
        assert fee_plans.plan_words(undecided, 'auto') == 'To be decided'


class TestRidersSeeAClassOnlyWithAFee:
    def test_auto_comes_back_once_its_fee_is_set(self, india):
        DriverFeePlan.objects.all().delete()
        daily('IN-TG-HYD', {'sedan': {'daily_pass_minor': 4900}, 'auto': {}})
        assert fee_plans.class_offered(india.hyd, 'auto') is False
        DriverFeePlan.objects.update(amounts={'sedan': {'daily_pass_minor': 4900}, 'auto': {'daily_pass_minor': 2000}})
        assert fee_plans.class_offered(india.hyd, 'auto') is True


class TestSimulator:
    def test_take_home_and_revenue_at_the_three_presets(self, india):
        maker, _ = india.super
        r = post(maker, f'{BASE}/fee-plans/simulate/', {'country': 'IN', 'vehicle_type': 'sedan',
                                                         'average_fare_minor': 20000}, expect=200)
        rows = body_of(r)['rows']
        assert {x['plan'] for x in rows} >= {'percent_commission', 'daily_pass', 'fair_share'}
        assert {x['trips_per_day'] for x in rows} >= {5, 12, 20}
        for x in rows:
            assert x['driver_takes_home_day_minor'] == x['fares_per_day_minor'] - x['driver_pays_day_minor']
        assert DriverFeePlan.objects.filter(status='active').count() == 1  # nothing activated


class TestReviewWords:
    def test_a_zero_fee_reads_as_no_platform_fee(self):
        p = DriverFeePlan(country='US', currency='USD', plan_type='fair_share', amounts={'*': {'per_trip_fee_minor': 0}})
        assert fee_plans.plan_words(p, 'standard') == 'No platform fee'
        assert fee_plans._line_words(p, 'standard') == 'No platform fee'

    def test_a_class_without_a_fee_says_it_is_not_offered(self, india):
        rows = resolved_rows('IN', DAY)['rows']
        auto = [r for r in rows if r['vehicle_type'] == 'auto']
        if not auto:
            pytest.skip('no auto rate card in this test database')
        assert auto[0]['words'] == 'Not offered to riders: no driver fee set'

    def test_a_plan_carries_its_place_by_name(self, india):
        from servers.pricing.fee_plan_workflow import plan_dict
        assert plan_dict(india.plan)['scope_name'] == 'India'
