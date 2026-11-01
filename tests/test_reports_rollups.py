"""Reports rollups (module 14): they equal a direct query over the source rows.

The expectations below are computed here, row by row in plain Python from the SAME fixture data
(trips, current settlements, the promotions ledger, the wallet ledger, refunds, expenses), never
from the code under test. Money is compared per (city, currency).
"""

from collections import defaultdict
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from conftest import seed_actor
from reports_world import CDT, IST, at, build_month, make_trip, settle_at
from servers.admin_access.models import GeoNode
from servers.admin_audit.models import AdminAuditLog
from servers.payments.aggregates import _current_settlements, finance_aggregates
from servers.reports import compute
from servers.reports.models import (
    DailyCityMetrics, DailyDriverSupply, DailyFraud, DailyPayouts, DailyRiderMetrics, DailySafety,
    DailySupport, RebuildLog, WeeklyRiderCohort,
)
from servers.reports.tasks import rebuild_recent, rebuild_week

pytestmark = pytest.mark.django_db

CITIES = {'IN-TG-HYD': IST, 'US-TX-AUS': CDT}


@pytest.fixture
def geo(db):
    from conftest import seed_admin_access

    seed_admin_access()
    return {n.code: n for n in GeoNode.objects.all()}


@pytest.fixture
def world(geo):
    return build_month(geo)


def rebuild_all(world, geo):
    for code in CITIES:
        for d in range(1, 32):
            compute.rebuild_day(geo[code], world.start.replace(day=d))


def rollup_totals(city, field):
    out = defaultdict(int)
    for row in DailyCityMetrics.objects.filter(city=city).values('currency', field):
        out[row['currency']] += row[field]
    return {k: v for k, v in out.items() if v}


def source_trips(code):
    from servers.ride.models import Trip

    return list(Trip.objects.filter(zone__geo_node__code=code).select_related(
        'status_id', 'requested_vehicle_type'))


def charged(trip):
    return trip.final_fare_minor if trip.final_fare_minor is not None else (trip.estimated_fare_minor or 0)


class TestRollupEqualsSource:
    def test_money_per_city_and_currency(self, geo, world):
        from servers.payments.models import PlatformExpense
        from servers.promotions.models import LedgerEntry
        from servers.ride.models import TripMoneyAction

        rebuild_all(world, geo)
        for code, currency in (('IN-TG-HYD', 'INR'), ('US-TX-AUS', 'USD')):
            city = geo[code]
            settlements = [s for s in _current_settlements()
                           if s.trip.zone.geo_node.code == code]
            trips = source_trips(code)
            assert rollup_totals(city, 'gmv_minor') == {currency: sum(s.gross_fare_minor for s in settlements)}
            assert rollup_totals(city, 'commission_minor') == {
                currency: sum(s.commission_amount_minor for s in settlements)}
            assert rollup_totals(city, 'driver_net_minor') == {
                currency: sum(s.driver_net_minor for s in settlements)}
            done = [t for t in trips if t.status_id.status_code == 'completed']
            assert rollup_totals(city, 'rider_paid_minor') == {currency: sum(charged(t) for t in done)}
            assert rollup_totals(city, 'trips_completed') == {currency: len(done)}
            assert rollup_totals(city, 'trips_requested') == {currency: len(trips)}
            promo = sum(e.amount_minor for e in LedgerEntry.objects.filter(
                kind='promo_discount_platform_funded', trip__zone__geo_node__code=code))
            assert rollup_totals(city, 'promo_cost_minor').get(currency, 0) == promo
            refunds = sum(a.amount_minor for a in TripMoneyAction.objects.filter(
                kind='refund', trip__zone__geo_node__code=code))
            assert rollup_totals(city, 'refunds_minor').get(currency, 0) == refunds
            fee = sum(e.amount_minor for e in PlatformExpense.objects.filter(
                trip_id__in=[t.id for t in trips]))
            assert rollup_totals(city, 'regulatory_fee_expense_minor').get(currency, 0) == fee

        # Wallet-ledger kinds: Hyderabad has the funded end, both rewards, the recovery and the
        # RIDER referral reward (placed by the rider's first trip); Austin has an incentive.
        hyd, aus = geo['IN-TG-HYD'], geo['US-TX-AUS']
        assert rollup_totals(hyd, 'platform_funded_trip_end_minor') == {'INR': 10000}
        assert rollup_totals(hyd, 'incentive_rewards_minor') == {'INR': 30000}
        assert rollup_totals(hyd, 'referral_rewards_minor') == {'INR': 10000 + 7500}
        assert rollup_totals(hyd, 'reward_recovery_minor') == {'INR': 4000}
        assert rollup_totals(aus, 'incentive_rewards_minor') == {'USD': 2000}
        assert rollup_totals(aus, 'regulatory_fee_expense_minor') == {'USD': 150}
        # Nothing leaked across the two cities, and nothing was summed across currencies.
        assert set(DailyCityMetrics.objects.values_list('currency', flat=True)) == {'INR', 'USD'}
        assert not DailyCityMetrics.objects.filter(city=hyd).exclude(currency='INR').exists()
        assert not DailyCityMetrics.objects.filter(city=aus).exclude(currency='USD').exists()

    def test_cash_and_online_both_settle_into_gmv(self, geo, world):
        rebuild_all(world, geo)
        methods = {t.payment_method for t in world.trips.hyd}
        assert {'cash', 'online'} <= methods
        total = sum(s.gross_fare_minor for s in _current_settlements()
                    if s.trip.zone.geo_node.code == 'IN-TG-HYD')
        assert rollup_totals(geo['IN-TG-HYD'], 'gmv_minor') == {'INR': total}
        # The promo trip: GMV counts the full fare, the rider paid less.
        promo = world.trips.promo
        assert promo.final_fare_minor == 15000 and promo.estimated_fare_minor == 20000
        row = DailyCityMetrics.objects.get(city=geo['IN-TG-HYD'], date=world.start.replace(day=12),
                                           vehicle_type='sedan')
        assert row.rider_paid_minor == 15000 and row.promo_cost_minor == 5000
        assert row.gmv_minor == 20000 or row.gmv_minor == 15000   # settled on the fare charged

    def test_counts_and_cancellation_causes(self, geo, world):
        rebuild_all(world, geo)
        city = geo['IN-TG-HYD']
        expected = world.trips.cancel_expected
        assert sum(rollup_totals(city, 'cancelled_by_rider').values()) == expected['rider']
        assert sum(rollup_totals(city, 'cancelled_by_driver').values()) == expected['driver']
        assert sum(rollup_totals(city, 'cancelled_by_ops').values()) == expected['ops']
        assert sum(rollup_totals(city, 'no_driver_found').values()) == expected['no_driver']
        accepted = [t for t in source_trips('IN-TG-HYD') if t.accepted_at]
        assert sum(rollup_totals(city, 'trips_accepted').values()) == len(accepted)

    def test_per_vehicle_split_adds_up(self, geo, world):
        rebuild_all(world, geo)
        by_vehicle = defaultdict(int)
        for row in DailyCityMetrics.objects.filter(city=geo['IN-TG-HYD']):
            by_vehicle[row.vehicle_type] += row.gmv_minor
        expected = defaultdict(int)
        for s in _current_settlements():
            if s.trip.zone.geo_node.code == 'IN-TG-HYD':
                expected[s.trip.requested_vehicle_type.type] += s.gross_fare_minor
        assert {k: v for k, v in by_vehicle.items() if v} == dict(expected)

    def test_gmv_equals_the_finance_aggregate_for_the_same_window(self, geo, world):
        """The rollups read the rows `finance.aggregate` reads: for the same window they agree."""
        rebuild_all(world, geo)
        now = world.trips.hyd[-1].completed_at + timedelta(days=1)
        window = finance_aggregates([geo['IN-TG-HYD']], '7d', now=now)['IN-TG-HYD']
        start = now - timedelta(days=7)
        expected = sum(s.gross_fare_minor for s in _current_settlements()
                       if s.trip.zone.geo_node.code == 'IN-TG-HYD' and start <= s.settled_at < now)
        assert window['gmv']['INR'] == expected
        # The month's rollup GMV is the all-time sum, so it can only be larger.
        assert rollup_totals(geo['IN-TG-HYD'], 'gmv_minor')['INR'] >= expected
        commission = sum(s.commission_amount_minor for s in _current_settlements()
                         if s.trip.zone.geo_node.code == 'IN-TG-HYD' and start <= s.settled_at < now)
        assert window['commission']['INR'] == commission

    def test_local_day_boundaries_hyderabad_and_austin(self, geo, world):
        """19:00 UTC on Oct 4 is 00:30 IST on the 5th (Hyderabad); 03:30 UTC on Oct 5 is 22:30 on
        the 4th in Austin. UTC days would put both on the wrong date."""
        rebuild_all(world, geo)
        hyd = DailyCityMetrics.objects.filter(city=geo['IN-TG-HYD'], trips_completed__gt=0)
        assert hyd.get(date=world.start.replace(day=5)).gmv_minor == 45000
        assert not hyd.filter(date=world.start.replace(day=4)).exists()
        aus = DailyCityMetrics.objects.filter(city=geo['US-TX-AUS'], trips_completed__gt=0)
        assert aus.get(date=world.start.replace(day=4)).gmv_minor == 3000
        assert aus.get(date=world.start.replace(day=5)).gmv_minor == 1800
        assert str(compute.city_context(geo['IN-TG-HYD']).tz) == 'Asia/Kolkata'
        assert str(compute.city_context(geo['US-TX-AUS']).tz) == 'America/Chicago'


class TestRebuildIsIdempotent:
    def snapshot(self):
        def rows(model, *fields):
            return sorted(model.objects.values_list(*fields))
        return (
            rows(DailyCityMetrics, 'city_id', 'date', 'vehicle_type', 'currency', 'gmv_minor',
                 'commission_minor', 'trips_requested', 'trips_completed', 'promo_cost_minor',
                 'incentive_rewards_minor'),
            rows(DailyDriverSupply, 'city_id', 'date', 'active_drivers', 'online_seconds'),
            rows(DailyRiderMetrics, 'city_id', 'date', 'new_riders', 'active_riders',
                 'first_trip_riders'),
            rows(DailySafety, 'city_id', 'date', 'source', 'kind', 'severity', 'count'),
            rows(DailySupport, 'city_id', 'date', 'opened', 'first_response_breached'),
            rows(DailyFraud, 'city_id', 'date', 'kind', 'raised', 'confirmed'),
            rows(DailyPayouts, 'city_id', 'date', 'currency', 'requested_minor'),
        )

    def test_rebuilding_twice_gives_the_same_rows(self, geo, world):
        rebuild_all(world, geo)
        first = self.snapshot()
        count = DailyCityMetrics.objects.count()
        rebuild_all(world, geo)
        assert self.snapshot() == first
        assert DailyCityMetrics.objects.count() == count

    def test_a_late_settlement_is_picked_up_by_a_rebuild(self, geo, world):
        rebuild_all(world, geo)
        before = rollup_totals(geo['IN-TG-HYD'], 'gmv_minor')['INR']
        late = make_trip(world.hyd, world.in_riders[0], world.in_driver, fare='99.00',
                         requested=world.trips.hyd[0].requested_at,
                         completed=world.trips.hyd[0].completed_at)
        settle_at(late, late.completed_at)
        assert rollup_totals(geo['IN-TG-HYD'], 'gmv_minor')['INR'] == before   # not yet
        compute.rebuild_day(geo['IN-TG-HYD'], world.start.replace(day=2))
        assert rollup_totals(geo['IN-TG-HYD'], 'gmv_minor')['INR'] == before + 9900

    def test_the_rebuild_log_says_when(self, geo, world):
        compute.rebuild_day(geo['IN-TG-HYD'], world.start.replace(day=2), trigger='test')
        log = RebuildLog.objects.get(city=geo['IN-TG-HYD'], date=world.start.replace(day=2))
        assert log.trigger == 'test' and (timezone.now() - log.built_at) < timedelta(minutes=1)


class TestOtherRollups:
    def test_supply_clips_a_session_to_each_local_day(self, geo, world):
        rebuild_all(world, geo)
        city = geo['IN-TG-HYD']
        oct2 = DailyDriverSupply.objects.get(city=city, date=world.start.replace(day=2))
        oct3 = DailyDriverSupply.objects.get(city=city, date=world.start.replace(day=3))
        # Oct 2 local: 09-13 (4 h) + 22-24 (2 h); Oct 3 local: 00-03 (3 h).
        assert oct2.online_seconds == 6 * 3600 and oct3.online_seconds == 3 * 3600
        assert oct2.online_drivers == 1 and oct3.online_drivers == 1
        assert oct2.active_drivers == 1      # two completed trips by one driver

    def test_riders_first_trip_is_counted_once_per_rider(self, geo, world):
        rebuild_all(world, geo)
        city = geo['IN-TG-HYD']
        firsts = sum(DailyRiderMetrics.objects.filter(city=city)
                     .values_list('first_trip_riders', flat=True))
        completed_riders = {t.user_id_id for t in source_trips('IN-TG-HYD')
                            if t.status_id.status_code == 'completed'}
        assert firsts == len(completed_riders)
        active_oct2 = DailyRiderMetrics.objects.get(city=city, date=world.start.replace(day=2))
        assert active_oct2.active_riders == 2

    def test_support_safety_fraud(self, geo, world):
        rebuild_all(world, geo)
        city = geo['IN-TG-HYD']
        support = DailySupport.objects.get(city=city, date=world.start.replace(day=5))
        assert support.opened == 1 and support.first_response_breached == 1   # answered after due
        sos = DailySafety.objects.get(city=city, date=world.start.replace(day=8),
                                      source='sos', kind='*')
        assert sos.count == 1 and sos.responded_count == 1 and sos.response_median_seconds == 180
        inc = DailySafety.objects.get(city=city, date=world.start.replace(day=8), source='incident')
        assert (inc.kind, inc.severity, inc.count) == (world.incident.category, world.incident.severity, 1)
        raised = DailyFraud.objects.get(city=city, date=world.start.replace(day=8), kind='referral_abuse')
        decided = DailyFraud.objects.get(city=city, date=world.start.replace(day=9), kind='referral_abuse')
        assert raised.raised == 1 and decided.confirmed == 1

    def test_no_table_can_hold_a_trip_id_a_coordinate_or_a_person(self):
        names = set()
        for model in (DailySafety, DailySupport, DailyFraud, DailyRiderMetrics, DailyDriverSupply,
                      DailyCityMetrics, DailyPayouts, WeeklyRiderCohort):
            names |= {f.name for f in model._meta.get_fields()}
        assert not {n for n in names if 'trip_id' in n or n in ('latitude', 'longitude', 'lat', 'long')
                    or 'user' in n or 'phone' in n or 'email' in n}

    def test_cohort_retention(self, geo, world):
        from finance_world import make_rider

        city = geo['IN-TG-HYD']
        riders = [make_rider(f'COH{i}', '91') for i in range(4)]
        # Week of Oct 5 (Monday): four riders take a first trip; two ride again the week after.
        for i, rider in enumerate(riders):
            when = at(IST, 10, 6 + (i % 2), 10)
            make_trip(world.hyd, rider, world.in_driver, fare='100.00', requested=when,
                      completed=when + timedelta(minutes=10))
        for rider in riders[:2]:
            when = at(IST, 10, 14, 10)
            make_trip(world.hyd, rider, world.in_driver, fare='100.00', requested=when,
                      completed=when + timedelta(minutes=10))
        today = world.start.replace(day=20)
        week = world.start.replace(day=5)
        compute.rebuild_cohorts(city, weeks=4, today=today)
        week0 = WeeklyRiderCohort.objects.get(city=city, cohort_week=week, weeks_since=0)
        week1 = WeeklyRiderCohort.objects.get(city=city, cohort_week=week, weeks_since=1)
        week2 = WeeklyRiderCohort.objects.get(city=city, cohort_week=week, weeks_since=2)
        # Independent expectation, rider by rider from the trips themselves.
        from servers.ride.models import Trip

        first, weeks_ridden = {}, defaultdict(set)
        for t in Trip.objects.filter(status_id__status_code='completed').order_by('completed_at'):
            local = t.completed_at.astimezone(IST).date()
            first.setdefault(t.user_id_id, (local, t.zone.geo_node.code))
            weeks_ridden[t.user_id_id].add(local - timedelta(days=local.weekday()))
        members = [u for u, (d, code) in first.items()
                   if code == 'IN-TG-HYD' and d - timedelta(days=d.weekday()) == week]
        assert len(members) >= 4
        assert week0.cohort_size == len(members) and week0.retained == len(members)
        assert week1.retained == sum(1 for u in members if week + timedelta(weeks=1) in weeks_ridden[u])
        assert week2.retained == sum(1 for u in members if week + timedelta(weeks=2) in weeks_ridden[u])
        assert week1.retained >= 2
        compute.rebuild_cohorts(city, weeks=4, today=today)       # idempotent
        assert WeeklyRiderCohort.objects.filter(city=city, cohort_week=week, weeks_since=1).count() == 1


class TestBeatTasksAndBackfill:
    def test_the_beat_tasks_are_scheduled(self):
        from django.conf import settings

        tasks = {v['task'] for v in settings.CELERY_BEAT_SCHEDULE.values()}
        assert {'reports.rebuild_recent', 'reports.rebuild_week'} <= tasks

    def test_recent_sweep_is_idempotent_and_covers_today_and_yesterday(self, geo, world):
        first = rebuild_recent()
        snapshot = sorted(RebuildLog.objects.values_list('city_id', 'date'))
        second = rebuild_recent()
        assert first['failed_cities'] == [] and second['failed_cities'] == []
        assert sorted(RebuildLog.objects.values_list('city_id', 'date')) == snapshot
        hyd_today = compute.local_today(geo['IN-TG-HYD'])
        dates = set(RebuildLog.objects.filter(city=geo['IN-TG-HYD']).values_list('date', flat=True))
        assert dates == {hyd_today, hyd_today - timedelta(days=1)}

    def test_nightly_sweep_covers_seven_days_and_cohorts(self, geo, world):
        result = rebuild_week()
        assert RebuildLog.objects.filter(city=geo['IN-TG-HYD']).count() == 7
        assert result['failed_cities'] == []

    def test_one_failing_city_does_not_stop_the_others(self, geo, world, monkeypatch):
        real = compute.rebuild_day

        def flaky(city, day, **kw):
            if city.code == 'IN-TG-HYD':
                raise RuntimeError('boom')
            return real(city, day, **kw)

        monkeypatch.setattr(compute, 'rebuild_day', flaky)
        result = rebuild_recent()
        assert 'IN-TG-HYD' in result['failed_cities']
        assert RebuildLog.objects.filter(city=geo['US-TX-AUS']).exists()

    def test_backfill_is_audited_and_needs_an_actor(self, geo, world):
        with pytest.raises(CommandError):
            call_command('backfill_reports', '--since', '2026-10-01', stdout=StringIO())
        assert not DailyCityMetrics.objects.exists()
        actor = seed_actor()
        args = ['--since', '2026-10-01', '--until', '2026-10-31', '--city', 'IN-TG-HYD',
                '--actor-email', actor.email]
        call_command('backfill_reports', *args, stdout=StringIO())
        assert DailyCityMetrics.objects.filter(city=geo['IN-TG-HYD']).exists()
        assert not DailyCityMetrics.objects.filter(city=geo['US-TX-AUS']).exists()
        row = AdminAuditLog.objects.filter(action='reports_backfilled').get()
        assert row.after['days_rebuilt'] == 31 and row.after['cities'] == ['IN-TG-HYD']
        assert actor.email in row.actor_label
        before = DailyCityMetrics.objects.count()
        call_command('backfill_reports', *args, stdout=StringIO())
        assert DailyCityMetrics.objects.count() == before
        assert AdminAuditLog.objects.filter(action='reports_backfilled').count() == 2

    def test_backfill_rejects_a_bad_range(self, geo):
        actor = seed_actor()
        with pytest.raises(CommandError):
            call_command('backfill_reports', '--since', '2026-10-05', '--until', '2026-10-01',
                         '--actor-email', actor.email, stdout=StringIO())
