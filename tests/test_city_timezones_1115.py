"""Daily jobs close each city's day at midnight in that city's own time zone, and payout times are
city-local (US round 5, 15 Nov). Includes the switch out of daylight saving time (US: Nov 1 2026)."""
from datetime import date, datetime, time, timedelta, timezone as dt_tz
from types import SimpleNamespace

import pytest

from finance_world import make_trip, settle
from servers.payments.payout_scope import due_cycle, zone_of
from servers.pricing import fee_plans
from servers.pricing.fee_plan_models import DriverDailyFee, DriverFeePlan
from servers.ride.models import Trip
from test_driver_fee_plans import india  # noqa: F401
from test_pricing_module import BASE, geo, make_rule, world  # noqa: F401

pytestmark = [pytest.mark.django_db, pytest.mark.no_default_commission]


def utc(*a):
    return datetime(*a, tzinfo=dt_tz.utc)


def trip_at(w, when):
    t = make_trip(w.hyd, w.rider, w.driver, fare='150', method='online')
    snap = fee_plans.snapshot_for_booking(w.hyd, 'sedan', when=when)
    Trip.objects.filter(pk=t.id).update(completed_at=when, requested_vehicle_type=w.sedan, **snap)
    t.refresh_from_db()
    settle(t)
    return t


@pytest.fixture
def mountain(india):
    """The test city on Mountain time (a city whose zone is not its country's default), no floor."""
    india.hyd.timezone_name = 'America/Boise'
    india.hyd.save(update_fields=['timezone_name'])
    DriverFeePlan.objects.filter(pk=india.plan.pk).update(earnings_floor_minor=0)
    return india


def days_charged(w):
    return list(DriverDailyFee.objects.filter(driver=w.driver).values_list('day', flat=True))


class TestFeeDayClosesAtCityMidnight:
    def test_mountain_city_closes_at_mountain_midnight_not_the_countrys(self, mountain):
        trip_at(mountain, utc(2026, 11, 21, 6, 30))          # Nov 20, 23:30 MST (Nov 21 in India)
        fee_plans.charge_yesterday(utc(2026, 11, 21, 6, 50))  # 23:50 MST: Nov 20 is not over
        assert days_charged(mountain) == []
        out = fee_plans.charge_yesterday(utc(2026, 11, 21, 7, 5))  # 00:05 MST Nov 21
        assert days_charged(mountain) == [date(2026, 11, 20)]
        assert out['IN']['days'] == {'America/Boise': '2026-11-20'}

    def test_india_closes_at_india_midnight(self, india):
        DriverFeePlan.objects.filter(pk=india.plan.pk).update(earnings_floor_minor=0)
        trip_at(india, utc(2026, 11, 20, 18, 0))              # Nov 20, 23:30 IST
        fee_plans.charge_yesterday(utc(2026, 11, 20, 18, 25))  # 23:55 IST
        assert days_charged(india) == []
        fee_plans.charge_yesterday(utc(2026, 11, 20, 18, 35))  # 00:05 IST Nov 21
        assert days_charged(india) == [date(2026, 11, 20)]

    def test_the_day_daylight_saving_ends_is_25_hours(self, mountain):
        hours = lambda s, e: e.astimezone(dt_tz.utc) - s.astimezone(dt_tz.utc)  # noqa: E731 - real elapsed time
        start, end = fee_plans._day_bounds('US', date(2026, 11, 1), 'America/Boise')
        assert (start, hours(start, end)) == (utc(2026, 11, 1, 6, 0), timedelta(hours=25))
        assert hours(*fee_plans._day_bounds('US', date(2026, 3, 8), 'America/Boise')) == timedelta(hours=23)
        early = trip_at(mountain, utc(2026, 11, 1, 6, 30))     # 00:30 MDT Nov 1
        late = trip_at(mountain, utc(2026, 11, 2, 6, 30))      # 23:30 MST Nov 1
        trip_at(mountain, utc(2026, 11, 2, 7, 30))             # 00:30 MST Nov 2
        assert [t.id for t in fee_plans.day_trips(mountain.driver, 'IN', date(2026, 11, 1))] == [early.id, late.id]
        fee_plans.charge_yesterday(utc(2026, 11, 2, 7, 5))     # 00:05 MST Nov 2
        row = DriverDailyFee.objects.get(driver=mountain.driver)
        assert (row.day, row.trips) == (date(2026, 11, 1), 2)
        assert row.explanation.startswith('1 Nov: 2 trips')    # dated, never "Today"


def weekly(country, scope=''):
    return SimpleNamespace(country=country, schedule_scope=scope, payout_schedule='weekly', payout_weekday=0,
                           payout_time_local=time(10, 0), payout_interval_days=None, payout_anchor_date=None)


class TestPayoutTimeIsLocal:
    def test_us_monday_10_mountain_standard_time(self):
        assert due_cycle(weekly('US'), utc(2026, 11, 16, 16, 59)) is None   # 09:59 MST
        assert due_cycle(weekly('US'), utc(2026, 11, 16, 17, 0)) == date(2026, 11, 16)

    def test_us_monday_10_mountain_daylight_time_before_the_switch(self):
        assert due_cycle(weekly('US'), utc(2026, 10, 26, 15, 59)) is None   # 09:59 MDT
        assert due_cycle(weekly('US'), utc(2026, 10, 26, 16, 0)) == date(2026, 10, 26)

    def test_india_monday_10_ist(self):
        assert due_cycle(weekly('IN'), utc(2026, 11, 16, 4, 29)) is None
        assert due_cycle(weekly('IN'), utc(2026, 11, 16, 4, 30)) == date(2026, 11, 16)

    def test_a_city_schedule_runs_on_the_citys_clock(self, mountain):
        assert str(zone_of(weekly('IN', 'IN-TG-HYD'))) == 'America/Boise'
        assert str(zone_of(weekly('IN'))) == 'Asia/Kolkata'
        assert due_cycle(weekly('IN', 'IN-TG-HYD'), utc(2026, 11, 16, 16, 59)) is None
        assert due_cycle(weekly('IN', 'IN-TG-HYD'), utc(2026, 11, 16, 17, 0)) == date(2026, 11, 16)
