"""Reports API (module 14): who sees which report, scope, currencies, export, limits.

Everything runs through the real endpoints over rollups built by the real `rebuild_day`.
"""

import csv
import re
import io
import json
from collections import defaultdict
from datetime import timedelta

import pytest
from django.core.cache import cache

from finance_world import operator
from reports_world import build_month
from servers.admin_access.masking import RAW_PII_PATTERNS
from servers.admin_access.models import GeoNode
from servers.admin_audit.models import AdminAuditLog
from servers.reports import compute
from servers.reports.catalog import MAX_RANGE_DAYS, REPORTS

pytestmark = pytest.mark.django_db

BASE = '/api/v1/reports/admin'
RANGE = {'from': '2026-10-01', 'to': '2026-10-31'}

OPS_REPORTS = ('trips', 'supply', 'riders', 'rider_retention', 'support', 'safety', 'fraud')
MONEY_REPORTS = ('revenue', 'payouts')
PROMO_REPORT = 'promotions_cost'

#: role -> reports it may open. The door is `report.read`; money needs finance.read or
#: finance.aggregate; promotion cost also opens with promo.read (Marketing).
MATRIX = {
    'super_admin': set(REPORTS),
    'city_ops_manager': set(REPORTS),
    'finance': set(REPORTS),
    'analyst': set(REPORTS),
    'marketing': set(OPS_REPORTS) | {PROMO_REPORT},
    'support_agent': set(),
    'safety_officer': set(),
    'kyc_verifier': set(),
}


@pytest.fixture(autouse=True)
def _fresh_buckets():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def geo(db):
    from conftest import seed_admin_access

    seed_admin_access()
    return {n.code: n for n in GeoNode.objects.all()}


@pytest.fixture
def world(geo):
    w = build_month(geo)
    for code in ('IN-TG-HYD', 'US-TX-AUS'):
        for d in range(1, 32):
            compute.rebuild_day(geo[code], w.start.replace(day=d))
        compute.rebuild_cohorts(geo[code], weeks=6, today=w.start.replace(day=31))
    return w


def data(response):
    assert response.status_code == 200, response.content[:400]
    return response.json()['data']


def get(client, key, **params):
    return client.get(f'{BASE}/{key}/', {**RANGE, **params})


# ---------------------------------------------------------------------------
# Capability matrix
# ---------------------------------------------------------------------------

class TestCapabilityMatrix:
    @pytest.mark.parametrize('role', sorted(MATRIX))
    def test_each_role_opens_exactly_its_reports(self, geo, world, role):
        client, _ = operator(role, geo, 'IN-TG-HYD')
        for key in REPORTS:
            expected = 200 if key in MATRIX[role] else 403
            assert get(client, key).status_code == expected, (role, key)

    @pytest.mark.parametrize('role', sorted(MATRIX))
    def test_the_catalogue_lists_what_the_role_may_open(self, geo, world, role):
        client, _ = operator(role, geo, 'IN-TG-HYD')
        response = client.get(f'{BASE}/catalogue/')
        if not MATRIX[role]:
            assert response.status_code == 403
            return
        listed = {r['key'] for area in data(response)['areas'] for r in area['reports']}
        assert listed == MATRIX[role]

    def test_marketing_sees_promotion_cost_with_gmv_and_trip_volume(self, geo, world):
        """AUDIT 27.59 ruling 1: GMV and trip volume for promo ROI; no take rate, commission,
        platform revenue or payouts."""
        client, _ = operator('marketing', geo, 'IN-TG-HYD')
        body = data(get(client, 'promotions_cost'))
        total = body['totals'][0]
        assert total['promo_cost_minor'] == 5000
        finance, _ = operator('finance', geo, 'IN-TG-HYD')
        assert total['gmv_minor'] == data(get(finance, 'revenue'))['totals'][0]['gmv_minor'] > 0
        assert total['trips_completed'] > 0
        assert total['promo_cost_percent_of_gmv'] == pytest.approx(
            100 * total['promo_cost_minor'] / total['gmv_minor'], abs=0.06)
        for forbidden in ('commission_minor', 'take_rate', 'net_take_minor', 'rider_paid_minor',
                          'driver_net_minor', 'platform_cost_minor', 'paid_minor'):
            assert forbidden not in total
            assert all(forbidden not in r for r in body['rows'])
        assert get(client, 'revenue').status_code == 403
        assert get(client, 'payouts').status_code == 403
        # trip volume (the trips report) is open to Marketing through the report door
        assert data(get(client, 'trips'))['totals'][0]['trips_completed'] > 0

    def test_marketing_promotion_csv_has_gmv_but_no_commission_or_take_rate(self, geo, world):
        client, _ = operator('marketing', geo, 'IN-TG-HYD')
        response = client.get(f'{BASE}/promotions_cost/export/', RANGE)
        assert response.status_code == 200
        header = response.content.decode().splitlines()[0].split(',')
        assert 'gmv' in header and 'trips_completed' in header
        assert not [h for h in header if re.search(r'commission|take_rate|platform|revenue|net_take|payout', h)]
        assert client.get(f'{BASE}/revenue/export/', RANGE).status_code == 403
        assert client.get(f'{BASE}/payouts/export/', RANGE).status_code == 403

    def test_an_unknown_report_is_404(self, geo, world):
        client, _ = operator('analyst', geo, 'IN-TG-HYD')
        assert client.get(f'{BASE}/nope/').status_code == 404

    def test_anonymous_is_refused(self, geo, world):
        from rest_framework.test import APIClient

        assert APIClient().get(f'{BASE}/trips/').status_code in (401, 403)


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

class TestScope:
    def test_city_caller_sees_only_their_city(self, geo, world):
        client, _ = operator('finance', geo, 'IN-TG-HYD')
        body = data(get(client, 'revenue'))
        assert [c['code'] for c in body['scope']['cities']] == ['IN-TG-HYD']
        assert body['currencies'] == ['INR']
        assert all(r['currency'] == 'INR' for r in body['rows'])

    def test_an_explicit_out_of_scope_city_is_404(self, geo, world):
        client, _ = operator('finance', geo, 'IN-TG-HYD')
        assert get(client, 'revenue', city='US-TX-AUS').status_code == 404
        assert get(client, 'trips', city='US-TX-AUS').status_code == 404
        # A place above their scope, and one that does not exist, are the same answer.
        assert get(client, 'revenue', city='IN').status_code == 404
        assert get(client, 'revenue', city='NOWHERE').status_code == 404

    def test_a_country_caller_sees_the_country_and_a_per_city_breakdown(self, geo, world):
        client, _ = operator('analyst', geo, 'IN')
        body = data(get(client, 'trips', group_by='city'))
        assert {c['code'] for c in body['scope']['cities']} >= {'IN-TG-HYD'}
        assert 'US-TX-AUS' not in {c['code'] for c in body['scope']['cities']}
        assert {r['city'] for r in body['rows']} == {'IN-TG-HYD'}
        # The country code itself is theirs to ask for.
        assert get(client, 'trips', city='IN').status_code == 200
        assert get(client, 'trips', city='US-TX-AUS').status_code == 404

    def test_super_admin_sees_both_cities(self, geo, world):
        client, _ = operator('super_admin', geo)
        body = data(get(client, 'trips', group_by='city'))
        assert {r['city'] for r in body['rows']} == {'IN-TG-HYD', 'US-TX-AUS'}

    def test_money_follows_the_place_the_money_capability_is_held(self, geo, world):
        """Finance in Hyderabad, Support in Austin: the money of Hyderabad only."""
        from servers.admin_access.models import AdminAssignment, AdminRole

        client, user = operator('finance', geo, 'IN-TG-HYD')
        AdminAssignment.objects.create(admin=user, role=AdminRole.objects.get(code='support_agent'),
                                       scope=geo['US-TX-AUS'])
        body = data(get(client, 'revenue'))
        assert body['currencies'] == ['INR']
        assert get(client, 'revenue', city='US-TX-AUS').status_code == 404


# ---------------------------------------------------------------------------
# Currencies, totals and parity
# ---------------------------------------------------------------------------

class TestCurrencies:
    def test_currencies_are_never_summed(self, geo, world):
        client, _ = operator('super_admin', geo)
        body = data(get(client, 'revenue', group_by='none', granularity='month'))
        assert body['currencies'] == ['INR', 'USD']
        totals = {t['currency']: t for t in body['totals']}
        assert set(totals) == {'INR', 'USD'}
        # Each total equals that currency's rows and nothing else.
        for cur, total in totals.items():
            rows = [r for r in body['rows'] if r['currency'] == cur]
            assert total['gmv_minor'] == sum(r['gmv_minor'] for r in rows)
        # USD GMV is the three Austin fares (3000 + 1800 + 4500), never added to rupees.
        assert totals['USD']['gmv_minor'] == 9300
        assert all(r['currency'] in ('INR', 'USD') for r in body['rows'])
        assert not any('currency' not in r for r in body['rows'])

    def test_revenue_by_vehicle_and_month_matches_the_trips(self, geo, world):
        """Parity with the frozen executive_revenue: rider-paid by vehicle class and month."""
        from servers.ride.models import Trip

        client, _ = operator('finance', geo, 'IN-TG-HYD')
        body = data(get(client, 'revenue', group_by='vehicle', granularity='month'))
        expected = defaultdict(int)
        for t in Trip.objects.filter(status_id__status_code='completed',
                                     zone__geo_node__code='IN-TG-HYD'):
            expected[t.requested_vehicle_type.type] += t.final_fare_minor if t.final_fare_minor is not None else t.estimated_fare_minor
        got = {r['vehicle']: r['rider_paid_minor'] for r in body['rows'] if r['rider_paid_minor']}
        assert got == dict(expected)
        assert {r['period'] for r in body['rows']} == {'2026-10-01'}
        total = body['totals'][0]
        assert total['rider_paid_minor'] == sum(expected.values())
        assert total['take_rate'] == pytest.approx(
            100 * total['commission_minor'] / total['gmv_minor'], abs=0.06)

    def test_platform_cost_and_net_take_use_every_ledger_kind(self, geo, world):
        client, _ = operator('finance', geo, 'IN-TG-HYD')
        total = data(get(client, 'revenue', group_by='none'))['totals'][0]
        assert total['promo_cost_minor'] == 5000
        assert total['platform_funded_trip_end_minor'] == 10000
        assert total['incentive_rewards_minor'] == 30000
        assert total['referral_rewards_minor'] == 17500
        assert total['reward_recovery_minor'] == 4000
        assert total['refunds_minor'] == 2500
        assert total['platform_cost_minor'] == 5000 + 10000 + 0 + 30000 + 17500 - 4000
        assert total['net_take_minor'] == total['commission_minor'] - total['platform_cost_minor']

    def test_the_austin_regulatory_fee_is_in_usd_only(self, geo, world):
        client, _ = operator('super_admin', geo)
        totals = {t['currency']: t for t in data(get(client, 'revenue', group_by='none'))['totals']}
        assert totals['USD']['regulatory_fee_expense_minor'] == 150
        assert totals['INR']['regulatory_fee_expense_minor'] == 0

    def test_granularities_agree_on_the_total(self, geo, world):
        client, _ = operator('finance', geo, 'IN-TG-HYD')
        seen = set()
        for granularity in ('day', 'week', 'month'):
            body = data(get(client, 'revenue', group_by='none', granularity=granularity))
            seen.add(body['totals'][0]['gmv_minor'])
            assert sum(r['gmv_minor'] for r in body['rows']) == body['totals'][0]['gmv_minor']
        assert len(seen) == 1
        weekly = data(get(client, 'revenue', group_by='none', granularity='week'))
        assert all(__import__('datetime').date.fromisoformat(r['period']).weekday() == 0
                   for r in weekly['rows'])

    def test_a_vehicle_filter_narrows_every_figure(self, geo, world):
        client, _ = operator('finance', geo, 'IN-TG-HYD')
        auto = data(get(client, 'revenue', group_by='none', vehicle='auto'))['totals'][0]
        # Rewards are not tied to a vehicle class, so a class filter leaves them out.
        assert auto['incentive_rewards_minor'] == 0
        assert auto['gmv_minor'] == 30000 + 12000 + 25000

    def test_trips_report_shows_the_funnel_and_cancellations(self, geo, world):
        client, _ = operator('city_ops_manager', geo, 'IN-TG-HYD')
        body = data(get(client, 'trips'))
        total = body['totals'][0]
        assert (total['cancelled_by_rider'], total['cancelled_by_driver'],
                total['cancelled_by_ops'], total['no_driver_found']) == (2, 1, 2, 2)
        stages = {s['stage']: s['count'] for s in body['extra']['funnel']}
        assert stages['requested'] == total['trips_requested']
        assert stages['completed'] == 8
        assert total['avg_eta_seconds'] == 240

    def test_supply_report_has_online_hours(self, geo, world):
        client, _ = operator('analyst', geo, 'IN-TG-HYD')
        total = data(get(client, 'supply'))['totals'][0]
        assert total['online_hours'] == 9.0     # 4 + 5 hours of sessions
        assert total['active_drivers'] >= 1

    def test_retention_table(self, geo, world):
        client, _ = operator('analyst', geo, 'IN-TG-HYD')
        body = data(get(client, 'rider_retention', **{'from': '2026-09-28', 'to': '2026-10-31'}))
        assert body['cohorts']
        first = body['cohorts'][0]
        assert first['percent'][0] == 100.0
        assert all(0 <= p <= 100 for c in body['cohorts'] for p in c['percent'] if p is not None)


# ---------------------------------------------------------------------------
# Safety history for the Analyst
# ---------------------------------------------------------------------------

class TestSosHistoryForTheAnalyst:
    def test_counts_and_response_times_only(self, geo, world):
        client, _ = operator('analyst', geo, 'IN-TG-HYD')
        response = get(client, 'safety', granularity='day')
        body = data(response)
        total = body['totals'][0]
        assert total['sos_events'] == 1 and total['sos_responded'] == 1
        assert total['sos_avg_response_seconds'] == 180
        text = response.content.decode()
        # No trip id, coordinate, person or event id anywhere in the body.
        for needle in ('trip_id', 'latitude', 'longitude', '17.43', '78.40', 'user_label', 'note',
                       '"id"'):
            assert needle not in text, needle
        assert not set(body['rows'][0]) & {'trip', 'trip_id', 'lat', 'lng', 'event_id'}

    def test_the_median_is_shown_for_a_single_rollup_row_only(self, geo, world):
        from reports_world import IST, at
        from servers.sos.models import SOSEvent, SOSEventUpdate

        client, _ = operator('analyst', geo, 'IN-TG-HYD')
        day = data(get(client, 'safety', granularity='day'))
        rows = [r for r in day['rows'] if r['sos_events']]
        assert rows[0]['sos_median_response_seconds'] == 180
        # A second SOS day: a median of the month cannot be made from two daily medians.
        second = SOSEvent.objects.create(user=world.in_riders[1], initiated_by='rider', trip=world.trips.hyd[1],
                                         event_type='medical')
        SOSEvent.objects.filter(pk=second.pk).update(created_at=at(IST, 10, 20, 9))
        SOSEventUpdate.objects.create(event=second, new_status='acknowledged')
        SOSEventUpdate.objects.filter(event=second).update(created_at=at(IST, 10, 20, 9, 1))
        compute.rebuild_day(geo['IN-TG-HYD'], world.start.replace(day=20))
        month = data(get(client, 'safety', granularity='month'))
        assert month['rows'][0]['sos_events'] == 2
        assert month['rows'][0]['sos_median_response_seconds'] is None
        assert month['rows'][0]['sos_avg_response_seconds'] == 120     # (180 + 60) / 2
        assert month['totals'][0]['sos_median_response_seconds'] is None


# ---------------------------------------------------------------------------
# Parameters and limits
# ---------------------------------------------------------------------------

class TestRangesAndParameters:
    def client(self, geo):
        return operator('analyst', geo, 'IN-TG-HYD')[0]

    def test_the_range_limit_is_400_days(self, geo, world):
        client = self.client(geo)
        ok = client.get(f'{BASE}/trips/', {'from': '2025-09-27', 'to': '2026-10-31'})
        assert (__import__('datetime').date(2026, 10, 31) - __import__('datetime').date(2025, 9, 27)).days + 1 == MAX_RANGE_DAYS
        assert ok.status_code == 200
        too_long = client.get(f'{BASE}/trips/', {'from': '2025-09-26', 'to': '2026-10-31'})
        assert too_long.status_code == 400
        assert too_long.json()['error']['details']['field'] == 'to'

    @pytest.mark.parametrize('params', [
        {'from': '2026-10-10', 'to': '2026-10-01'},
        {'from': 'yesterday', 'to': '2026-10-01'},
        {'granularity': 'year'},
        {'group_by': 'planet'},
        {'group_by': 'vehicle'},          # trips allows it; supply does not (see below)
    ])
    def test_bad_parameters_are_400(self, geo, world, params):
        client = self.client(geo)
        key = 'supply' if params.get('group_by') == 'vehicle' else 'trips'
        response = get(client, key, **params)
        assert response.status_code == 400, response.content[:200]

    def test_defaults_are_the_last_thirty_days(self, geo, world):
        client = self.client(geo)
        body = data(client.get(f'{BASE}/trips/'))
        d_from, d_to = body['params']['from'], body['params']['to']
        import datetime as dt

        assert (dt.date.fromisoformat(d_to) - dt.date.fromisoformat(d_from)).days == 29

    def test_data_as_of_is_reported(self, geo, world):
        client = self.client(geo)
        assert data(get(client, 'trips'))['as_of']
        assert data(client.get(f'{BASE}/catalogue/'))['as_of']
        empty = data(client.get(f'{BASE}/trips/', {'from': '2020-01-01', 'to': '2020-01-31'}))
        assert empty['as_of'] is None and empty['rows'] == []
        assert empty['totals'][0]['trips_requested'] == 0


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

class TestExport:
    def csv_of(self, response):
        assert response.status_code == 200, response.content[:300]
        return list(csv.reader(io.StringIO(response.content.decode())))

    def test_the_csv_is_the_table_the_screen_shows(self, geo, world):
        client, _ = operator('finance', geo, 'IN-TG-HYD')
        params = {**RANGE, 'granularity': 'month', 'group_by': 'vehicle'}
        shown = data(client.get(f'{BASE}/revenue/', params))
        response = client.get(f'{BASE}/revenue/export/', params)
        rows = self.csv_of(response)
        header = rows[0]
        assert header[:3] == ['period', 'vehicle', 'currency']
        assert len(rows) - 1 == len(shown['rows'])
        sedan = next(r for r in rows[1:] if r[1] == 'sedan')
        gmv = header.index('gmv')
        expected = next(r for r in shown['rows'] if r['vehicle'] == 'sedan')['gmv_minor']
        assert sedan[gmv] == f'{expected / 100:.2f}'      # major units, per its currency column
        assert sedan[2] == 'INR'

    def test_filename_has_scope_and_range(self, geo, world):
        client, _ = operator('finance', geo, 'IN-TG-HYD')
        response = client.get(f'{BASE}/revenue/export/', RANGE)
        disposition = response['Content-Disposition']
        assert 'report-revenue-IN-TG-HYD-20261001-20261031.csv' in disposition
        assert response['Cache-Control'] == 'no-store'
        everything, _ = operator('super_admin', geo)
        assert 'report-trips-all-' in everything.get(f'{BASE}/trips/export/', RANGE)['Content-Disposition']

    def test_every_export_is_audited_with_params_and_row_count(self, geo, world):
        client, user = operator('analyst', geo, 'IN-TG-HYD')
        response = client.get(f'{BASE}/trips/export/', {**RANGE, 'granularity': 'week'})
        rows = self.csv_of(response)
        audit = AdminAuditLog.objects.filter(action='report_exported').get()
        assert audit.actor_id == user.id and audit.target_id == 'trips'
        assert audit.after['row_count'] == len(rows) - 1
        assert audit.after['params']['granularity'] == 'week'
        assert audit.after['scope'] == 'IN-TG-HYD'

    def test_an_export_without_the_capability_is_403_and_leaves_no_audit_row(self, geo, world):
        client, _ = operator('marketing', geo, 'IN-TG-HYD')
        assert client.get(f'{BASE}/revenue/export/', RANGE).status_code == 403
        assert not AdminAuditLog.objects.filter(action='report_exported').exists()
        out_of_scope, _ = operator('finance', geo, 'IN-TG-HYD')
        assert out_of_scope.get(f'{BASE}/revenue/export/',
                                {**RANGE, 'city': 'US-TX-AUS'}).status_code == 404

    def test_no_export_without_an_audit_row(self, geo, world, monkeypatch):
        from servers.reports import views

        monkeypatch.setattr(views, 'record_admin_action', lambda *a, **k: None)
        client, _ = operator('analyst', geo, 'IN-TG-HYD')
        assert client.get(f'{BASE}/trips/export/', RANGE).status_code == 503

    def test_exports_carry_no_personal_columns_and_no_raw_pii(self, geo, world):
        client, _ = operator('super_admin', geo)
        import re

        sensitive = re.compile(r'phone|email|address|upi|ifsc|account|licen[cs]e|aadhaar|ssn|dob|'
                               r'contact|name|trip_id|user|(^|_)(lat|lng|long|latitude|longitude)($|_)',
                               re.I)
        for key in REPORTS:
            text = client.get(f'{BASE}/{key}/export/', RANGE).content.decode()
            header = text.splitlines()[0].split(',')
            assert not [h for h in header if sensitive.search(h)], (key, header)
            for name, rx in RAW_PII_PATTERNS.items():
                assert not rx.search(text), (key, name)

    def test_export_is_rate_limited_per_operator(self, geo, world, monkeypatch):
        from servers.reports.views import ReportExportThrottle

        monkeypatch.setattr(ReportExportThrottle, 'THROTTLE_RATES', {'report_export': '2/min'})
        a, _ = operator('analyst', geo, 'IN-TG-HYD')
        b, _ = operator('analyst', geo, 'IN-TG-HYD')
        for _ in range(2):
            assert a.get(f'{BASE}/trips/export/', RANGE).status_code == 200
        assert a.get(f'{BASE}/trips/export/', RANGE).status_code == 429
        assert b.get(f'{BASE}/trips/export/', RANGE).status_code == 200
        # The screen itself is not the export bucket.
        assert a.get(f'{BASE}/trips/', RANGE).status_code == 200

    def test_the_retention_export_is_the_cohort_table(self, geo, world):
        client, _ = operator('analyst', geo, 'IN-TG-HYD')
        rows = self.csv_of(client.get(f'{BASE}/rider_retention/export/',
                                      {'from': '2026-09-28', 'to': '2026-10-31'}))
        assert rows[0][:2] == ['cohort_week', 'riders'] and len(rows) > 1


class TestNothingWrites:
    def test_reading_and_exporting_change_no_business_row(self, geo, world):
        from servers.payments.models import TripSettlement
        from servers.ride.models import Trip
        from servers.rider.models import WalletTransaction

        counts = (Trip.objects.count(), TripSettlement.objects.count(),
                  WalletTransaction.objects.count())
        client, _ = operator('super_admin', geo)
        for key in REPORTS:
            get(client, key)
            client.get(f'{BASE}/{key}/export/', RANGE)
        assert counts == (Trip.objects.count(), TripSettlement.objects.count(),
                          WalletTransaction.objects.count())
        # Only the export audit rows were added.
        assert set(AdminAuditLog.objects.values_list('action', flat=True)) >= {'report_exported'}

    def test_the_api_has_only_get_routes(self, geo, world):
        client, _ = operator('super_admin', geo)
        for method in ('post', 'put', 'patch', 'delete'):
            assert getattr(client, method)(f'{BASE}/trips/', {}).status_code == 405
