"""Driver fee plans, "Fair Share" (owner brief, 9 Nov): resolution, the end-of-day fee, the legal
guard, plan switches, the simulator and the console's maker-checker.

Fixed example days at a Rs 150 fare under India's car plan (Rs 10 a trip, cap Rs 149, floor
Rs 1,000): 0 trips 0; 3 trips (Rs 450) 0, under the floor; 5 trips (Rs 750) 0; 12 trips Rs 120;
20 trips Rs 149 (cap); 40 trips Rs 149.
"""
from datetime import date, datetime, timedelta, timezone as dt_tz
from decimal import Decimal

import pytest

from finance_world import make_driver, make_rider, make_trip, settle
from servers.admin_audit.models import AdminAuditLog
from servers.driver.models import VehicleType
from servers.pricing import fee_plans
from servers.pricing.fee_plan_models import DriverDailyFee, DriverFeePlan
from servers.ride.models import Trip
from servers.rider.models import Wallet, WalletTransaction
from test_finance_actions import approve_request, body_of, checker, post
from test_pricing_module import BASE, geo, make_rule, world  # noqa: F401

pytestmark = [pytest.mark.django_db, pytest.mark.no_default_commission]

DAY = date(2026, 11, 20)
CARS = {'per_trip_fee_minor': 1000}


def plan(country='IN', scope='', plan_type='fair_share', amounts=None, version=1, **kw):
    kw.setdefault('earnings_floor_minor', 100000)
    kw.setdefault('daily_cap_minor', 14900)
    return DriverFeePlan.objects.create(
        country=country, scope_code=scope, plan_type=plan_type, version=version, status='active',
        effective_from=date(2026, 1, 1), amounts=amounts if amounts is not None else {'sedan': CARS}, **kw)


@pytest.fixture
def india(world):
    DriverFeePlan.objects.all().delete()
    make_rule('IN', '15.00', node=world.geo['IN'])
    world.plan = plan()
    world.driver = make_driver(world.geo, 'IN-TG-HYD', 'f', currency='INR', calling='91')
    world.rider = make_rider('f', '91')
    world.sedan = VehicleType.objects.get(type='sedan')
    return world


def trips_on(world, day, n, fare='150', method='online'):
    out = []
    # 10:00 IST on `day`, a minute apart.
    base = datetime(day.year, day.month, day.day, 4, 30, tzinfo=dt_tz.utc)
    for i in range(n):
        t = make_trip(world.hyd, world.rider, world.driver, fare=fare, method=method)
        snap = fee_plans.snapshot_for_booking(world.hyd, 'sedan', when=base)
        Trip.objects.filter(pk=t.id).update(completed_at=base + timedelta(minutes=i),
                                            requested_vehicle_type=world.sedan, **snap)
        t.refresh_from_db()
        settle(t)
        out.append(t)
    return out


class TestResolution:
    @pytest.mark.parametrize('plan_type', ['percent_commission', 'per_trip_fee', 'daily_pass', 'fair_share'])
    def test_city_then_state_then_country(self, world, plan_type):
        DriverFeePlan.objects.all().delete()
        country = plan(plan_type=plan_type)
        assert fee_plans.resolve('IN', ['IN-TG-HYD', 'IN-TG'], DAY) == country
        state = plan(scope='IN-TG', plan_type=plan_type)
        assert fee_plans.resolve('IN', ['IN-TG-HYD', 'IN-TG'], DAY) == state
        city = plan(scope='IN-TG-HYD', plan_type=plan_type)
        assert fee_plans.resolve('IN', ['IN-TG-HYD', 'IN-TG'], DAY) == city
        assert fee_plans.resolve('IN', ['IN-AP-VJA', 'IN-AP'], DAY) == country

    def test_a_class_the_plan_does_not_name_keeps_the_commission(self, india):
        assert fee_plans.snapshot_for_booking(india.hyd, 'auto') == {}
        snap = fee_plans.snapshot_for_booking(india.hyd, 'sedan')
        assert snap['fee_plan_type'] == 'fair_share' and snap['commission_percent_snapshot'] == 0


class TestEndOfDay:
    @pytest.mark.parametrize('n,fee,reason', [(0, 0, None), (3, 0, 'under_floor'), (5, 0, 'under_floor'),
                                              (12, 12000, 'charged'), (20, 14900, 'charged'),
                                              (40, 14900, 'charged')])
    def test_fixed_example_days(self, india, n, fee, reason):
        trips_on(india, DAY, n)
        row = fee_plans.charge_day(india.driver, 'IN', DAY)
        if n == 0:
            assert row is None
            return
        assert (row.fee_minor, row.reason, row.trips) == (fee, reason, n)
        # Settlement kept the whole fare for the driver: no commission on these trips.
        earned = WalletTransaction.objects.filter(user_id=india.driver.user_id, purpose='trip_earnings')
        assert sum(e.amount_minor for e in earned) == n * 15000
        assert fee_plans.charge_day(india.driver, 'IN', DAY) == row        # twice: one row
        assert DriverDailyFee.objects.count() == 1
        rows = WalletTransaction.objects.filter(purpose='platform_fee')
        assert rows.count() == (1 if fee else 0) and sum(r.amount_minor for r in rows) == fee
        if n == 12:
            assert row.explanation == 'Today: 12 trips × ₹10.00 = ₹120.00 (cap ₹149.00)'
        if n == 3:
            assert row.explanation == 'No fee today: earnings under ₹1,000.00.'

    def test_the_hourly_job_charges_yesterday_once(self, india):
        trips_on(india, DAY, 12)
        now = datetime(2026, 11, 21, 3, 0, tzinfo=dt_tz.utc)              # 08:30 IST next day
        assert fee_plans.charge_yesterday(now)['IN'] == {'day': '2026-11-20', 'drivers': 1}
        fee_plans.charge_yesterday(now)
        assert WalletTransaction.objects.filter(purpose='platform_fee').count() == 1

    def test_cash_driver_owes_it_from_the_next_earnings(self, india):
        trips_on(india, DAY, 12, method='cash')
        fee_plans.charge_day(india.driver, 'IN', DAY)
        wallet = Wallet.objects.get(user_id=india.driver.user_id, scope='driver')
        assert wallet.balance_minor == -12000          # no commission on cash trips; the fee is owed

    def test_the_launch_waiver(self, india):
        india.plan.waiver_first_days = 60
        india.plan.save()
        trips_on(india, DAY, 12)
        row = fee_plans.charge_day(india.driver, 'IN', DAY)
        assert row.fee_minor == 0 and row.reason == 'waiver'


class TestLegalGuard:
    @pytest.mark.parametrize('plan_type,amounts', [
        ('per_trip_fee', {'sedan': {'per_trip_fee_minor': 1000}}),
        ('fair_share', {'sedan': {'per_trip_fee_minor': 1000}}),
        ('daily_pass', {'sedan': {'daily_pass_minor': 19900}}),
    ])
    def test_a_40_rupee_trip_leaves_the_driver_80_percent(self, india, plan_type, amounts):
        DriverFeePlan.objects.all().delete()
        india.plan = plan(plan_type=plan_type, amounts=amounts, earnings_floor_minor=0, daily_cap_minor=None)
        trips_on(india, DAY, 1, fare='40')
        row = fee_plans.charge_day(india.driver, 'IN', DAY)
        assert row.fee_minor == 800 and row.guard_reduced_from_minor in (1000, 19900)

    def test_percent_commission_is_held_by_the_commission_cap(self, india):
        bad = DriverFeePlan(country='IN', plan_type='percent_commission',
                            amounts={'sedan': {'commission_percent': 25}})
        assert fee_plans.simulate(bad, 'sedan', 5, 15000)['breaks_legal_share']

    def test_activation_is_refused_when_a_normal_trip_breaks_it(self, india):
        from servers.pricing.models import RateCard
        from test_pricing_module import LONG_AGO
        RateCard.objects.create(zone=india.hyd, vehicle_type=india.sedan, effective_from=LONG_AGO,
                                base_fare=Decimal('30'), per_km_fare=Decimal('10'), per_min_fare=Decimal('1'),
                                min_fare=Decimal('50'))                  # 5 km, 15 min: Rs 95
        greedy = DriverFeePlan(country='IN', plan_type='per_trip_fee',
                               amounts={'sedan': {'per_trip_fee_minor': 2500}})
        assert fee_plans.breaks_legal_share_on_normal_trip(greedy) == ['sedan']


class TestSwitching:
    def test_trips_before_the_switch_keep_their_plan(self, india):
        DriverFeePlan.objects.all().delete()
        commission = plan(plan_type='percent_commission', amounts={'sedan': {'commission_percent': 15}})
        before = trips_on(india, DAY, 2)
        from servers.payments.models import TripSettlement
        assert all(t.fee_plan_type == 'percent_commission' for t in before)
        assert {s.commission_percent for s in TripSettlement.objects.filter(trip__in=before)} == {Decimal('15.00')}
        commission.status, commission.effective_to = 'archived', DAY + timedelta(days=1)
        commission.save()
        fair = plan(version=2)
        fair.effective_from = DAY + timedelta(days=1)
        fair.save()
        after = trips_on(india, DAY + timedelta(days=1), 2)
        for t in before:
            t.refresh_from_db()
            assert t.fee_plan_type == 'percent_commission' and t.commission_percent_snapshot is None
        assert all(t.fee_plan_type == 'fair_share' and t.commission_percent_snapshot == 0 for t in after)
        assert fee_plans.charge_day(india.driver, 'IN', DAY) is None      # commission trips: no fee row


class TestSimulator:
    @pytest.mark.parametrize('n', [5, 12, 20])
    def test_simulator_equals_the_end_of_day_job(self, india, n):
        trips_on(india, DAY, n)
        row = fee_plans.charge_day(india.driver, 'IN', DAY)
        sim = fee_plans.simulate(india.plan, 'sedan', n, 15000)
        assert sim['driver_pays_day_minor'] == row.fee_minor

    def test_the_console_compares_plans_at_the_presets(self, india):
        maker, _ = india.super
        r = post(maker, f'{BASE}/fee-plans/simulate/', {
            'country': 'IN', 'vehicle_type': 'sedan', 'average_fare_minor': 25000,
            'commission_percent': 10, 'daily_pass_minor': 19900}, expect=200)
        rows = {(x['plan'], x['preset']): x for x in body_of(r)['rows']}
        assert rows[('fair_share', 'medium')]['driver_pays_day_minor'] == 12000
        assert rows[('percent_commission', 'medium')]['driver_pays_day_minor'] == 12 * 2500
        assert rows[('daily_pass', 'basic')]['driver_pays_day_minor'] == 19900


class TestConsoleMakerChecker:
    def test_draft_submit_approve_activate_in_the_audit_log(self, india):
        maker, _ = india.super
        r = post(maker, f'{BASE}/fee-plans/', {
            'country': 'IN', 'plan_type': 'fair_share', 'amounts': {'sedan': {'per_trip_fee_minor': 900}},
            'earnings_floor_minor': 100000, 'daily_cap_minor': 13900, 'reason': 'Lower the fee'}, expect=201)
        plan_id = body_of(r)['id']
        r = post(maker, f'{BASE}/fee-plans/{plan_id}/submit/', {'reason': 'Q1 review'}, expect=202)
        assert DriverFeePlan.objects.get(pk=plan_id).status == 'pending_approval'
        chk, _ = checker()
        assert approve_request(chk, body_of(r)['approval_request']['id']).status_code == 200
        new = DriverFeePlan.objects.get(pk=plan_id)
        assert new.status == 'active' and new.effective_from == fee_plans.local_day('IN') + timedelta(days=1)
        india.plan.refresh_from_db()
        assert india.plan.status == 'archived' and india.plan.effective_to == new.effective_from
        actions = set(AdminAuditLog.objects.values_list('action', flat=True))
        assert {'driver_fee_plan_drafted', 'driver_fee_plan_activated'} <= actions
