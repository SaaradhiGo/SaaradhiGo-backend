"""Command Centre (module 1): per-city overview, alert thresholds, the sweep.

WHAT THESE PROVE
----------------
  * the overview's numbers against a seeded two-city fixture (Hyderabad in
    INR, Austin in USD): requested / accepted / completed / cancelled /
    unfulfilled, the three rates, money per currency never summed across,
    the queues, and every windowed figure's same-window-last-week value;
  * rates with a zero denominator are None, not 0;
  * scope: a Hyderabad City Ops Manager sees one row and cannot ask for
    Austin; a Super Admin sees both, and not the paused cities with no data;
  * money is withheld from anyone without `finance.read`;
  * "today" is the city's own day;
  * the 30 s cache is per (scope, node, window);
  * thresholds: the seed is idempotent and never overwrites; a change is a
    maker-checker request that needs a DIFFERENT `platform.settings` holder
    over the city, and writes version 2 only on approval, audited;
  * the beat task raises alerts when a line is crossed, raises nothing
    twice, honours the minimum sample, and clears the alert when the number
    recovers -- with an audit row either way;
  * acknowledging follows the kind's own permission: City Ops takes a rate
    alert, Finance a payout-age alert, Safety an SOS alert, Analyst nothing,
    and nobody sees another city's alert.
"""

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from io import StringIO
from zoneinfo import ZoneInfo

import pytest
from django.conf import settings as django_settings
from django.core.management import call_command
from django.utils import timezone

from conftest import seed_actor
from servers.admin_access.models import GeoNode
from servers.admin_audit.models import AdminAuditLog
from servers.approvals.models import EXECUTED, REQUESTED, ApprovalRequest
from servers.command_centre import overview, thresholds
from servers.command_centre.metrics import METRICS
from servers.command_centre.models import ACK_PERMISSION, AlertThreshold, OperatorAlert
from servers.command_centre.services import raise_alert
from servers.command_centre.tasks import evaluate_alert_thresholds
from servers.driver.models import DriverSession, WithdrawalRequest
from servers.ride.models import Trip, TripStatus
from servers.sos.models import SOSEvent
from servers.support.models import SupportTicket
from test_sos_support_zones_matrix import (  # noqa: F401 - fixtures
    _client_for, _driver, _rider, _zone, geo, operator,
)

pytestmark = pytest.mark.django_db

OVERVIEW = '/api/v1/command-centre/admin/overview/'
ALERTS = '/api/v1/command-centre/admin/alerts/'
THRESHOLDS = '/api/v1/command-centre/admin/thresholds/'
APPROVALS = '/api/v1/approvals/admin/requests/'

AUSTIN_POLYGON = {
    'type': 'Polygon',
    'coordinates': [[[-97.85, 30.15], [-97.60, 30.15], [-97.60, 30.40],
                     [-97.85, 30.40], [-97.85, 30.15]]],
}
HOUR = timedelta(hours=1)
WEEK = timedelta(days=7)


@pytest.fixture(autouse=True)
def _no_cache_no_presence(settings, monkeypatch):
    """Numbers are asserted fresh, and the online count comes from the
    status column unless a test says otherwise (Redis presence is not part
    of the fixture)."""
    settings.COMMAND_CENTRE_OVERVIEW_CACHE_SECONDS = 0
    monkeypatch.setattr(overview, 'online_driver_ids', lambda: None)


def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


def _trip(rider, zone, requested_at, *, driver=None, status='completed', accept_after=None,
          reach_after=None, complete_after=None, cancel_after=None, fare_minor=10_000):
    """A trip whose clock is set explicitly; `requested_at` is auto_now_add
    so it is written with an UPDATE after the insert."""
    trip = Trip.objects.create(
        user_id=rider, driver_id=driver, status_id=_status(status), zone=zone,
        pickup_lat=Decimal('17.4'), pickup_long=Decimal('78.4'),
        destination_lat=Decimal('17.5'), destination_long=Decimal('78.5'),
        estimated_fare_minor=fare_minor, currency=zone.currency,
    )
    fields = {'requested_at': requested_at}
    if accept_after is not None:
        fields['accepted_at'] = requested_at + accept_after
    if reach_after is not None:
        fields['reached_at'] = requested_at + reach_after
    if complete_after is not None:
        fields['completed_at'] = requested_at + complete_after
    if cancel_after is not None:
        fields['cancelled_at'] = requested_at + cancel_after
    Trip.objects.filter(pk=trip.pk).update(**fields)
    trip.refresh_from_db()
    return trip


def _completed(rider, zone, at, driver, eta_minutes, fare_minor):
    return _trip(rider, zone, at, driver=driver, status='completed',
                 accept_after=timedelta(minutes=1),
                 reach_after=timedelta(minutes=1 + eta_minutes),
                 complete_after=timedelta(minutes=20 + eta_minutes), fare_minor=fare_minor)


def _unfulfilled(rider, zone, at):
    return _trip(rider, zone, at, status='cancelled', cancel_after=timedelta(minutes=3), fare_minor=0)


@pytest.fixture
def world(geo):
    """Hyderabad and Austin, with a known day in each and the same day a
    week earlier.

    Hyderabad, last 24 h: 10 requests -- 6 completed (ETAs 5,5,10,10,15,15
    -> mean 10.0 min; fares 100..600 INR = 2,100.00 INR), 2 cancelled before
    any driver accepted (unfulfilled), 1 accepted then cancelled, 1 still
    searching. Last week: 5 requests -- 4 completed (100 INR each), 1
    unfulfilled.

    Austin, last 24 h: 4 requests -- 2 completed (15.00 + 25.00 USD), 2
    unfulfilled. Last week: nothing at all.
    """
    now = timezone.now()
    hyd_zone = _zone('IN-TG-HYD', geo['IN-TG-HYD'])
    aus_zone = _zone('US-TX-AUS', geo['US-TX-AUS'], country='US', state_code='TX',
                     timezone_name='America/Chicago', polygon_geojson=AUSTIN_POLYGON)
    rider = _rider('+919876700001', 'Overview Rider')
    rider2 = _rider('+919876700002', 'Second Rider')
    aus_rider = _rider('+15125570001', 'Austin Rider')
    hyd_driver = _driver('+919876700003', 'Hyderabad Driver', geo['IN-TG-HYD'])
    hyd_driver.status = 'online'
    hyd_driver.approved = True
    hyd_driver.save(update_fields=['status', 'approved'])
    aus_driver = _driver('+15125570002', 'Austin Driver', geo['US-TX-AUS'])

    recent = now - 2 * HOUR
    for eta, fare in zip((5, 5, 10, 10, 15, 15), (10_000, 20_000, 30_000, 40_000, 50_000, 60_000), strict=True):
        _completed(rider, hyd_zone, recent, hyd_driver, eta, fare)
    _unfulfilled(rider, hyd_zone, recent)
    _unfulfilled(rider2, hyd_zone, recent)
    _trip(rider2, hyd_zone, recent, driver=hyd_driver, status='cancelled',
          accept_after=timedelta(minutes=1), cancel_after=timedelta(minutes=4))
    searching = _trip(rider, hyd_zone, now - timedelta(minutes=5), status='requested')

    last_week = recent - WEEK
    for _ in range(4):
        _completed(rider, hyd_zone, last_week, hyd_driver, 6, 10_000)
    _unfulfilled(rider, hyd_zone, last_week)

    _completed(aus_rider, aus_zone, recent, aus_driver, 7, 1_500)
    _completed(aus_rider, aus_zone, recent, aus_driver, 9, 2_500)
    _unfulfilled(aus_rider, aus_zone, recent)
    _unfulfilled(aus_rider, aus_zone, recent)

    hyd_trip = Trip.objects.filter(zone=hyd_zone, status_id__status_code='completed').first()
    sos = SOSEvent.objects.create(
        user=rider, user_label='Overview Rider', initiated_by='rider', trip=hyd_trip,
        event_type='panic', status='open',
    )
    SOSEvent.objects.filter(pk=sos.pk).update(created_at=now - timedelta(minutes=10))
    SupportTicket.objects.create(user_id=rider, trip_id=hyd_trip, issue_type='other',
                                 description='help')
    hyd_wd = WithdrawalRequest.objects.create(driver=hyd_driver, amount_minor=100_000,
                                              currency='INR', status='pending')
    WithdrawalRequest.objects.filter(pk=hyd_wd.pk).update(requested_at=now - 60 * HOUR)
    aus_wd = WithdrawalRequest.objects.create(driver=aus_driver, amount_minor=5_000,
                                              currency='USD', status='pending')
    WithdrawalRequest.objects.filter(pk=aus_wd.pk).update(requested_at=now - HOUR)
    DriverSession.objects.create(driver=hyd_driver, started_at=now - HOUR)

    return {
        'now': now, 'hyd_zone': hyd_zone, 'aus_zone': aus_zone, 'rider': rider,
        'hyd_driver': hyd_driver, 'aus_driver': aus_driver, 'sos': sos,
        'hyd_trip': hyd_trip, 'searching': searching,
    }


def _row(payload, code):
    return next(c for c in payload['cities'] if c['code'] == code)


def _get(client, **params):
    query = '&'.join(f'{k}={v}' for k, v in params.items())
    response = client.get(f'{OVERVIEW}?{query}' if query else OVERVIEW)
    return response, (response.json().get('data') if response.status_code == 200 else response.json())


# ===========================================================================
# The numbers
# ===========================================================================

class TestOverviewNumbers:
    def test_hyderabad_row_and_its_last_week(self, geo, world):
        client, _ = operator('super_admin', geo)
        response, data = _get(client, window='24h')
        assert response.status_code == 200, response.content
        hyd = _row(data, 'IN-TG-HYD')

        assert hyd['trips']['requested'] == {'now': 10, 'last_week': 5}
        assert hyd['trips']['accepted'] == {'now': 7, 'last_week': 4}
        assert hyd['trips']['completed'] == {'now': 6, 'last_week': 4}
        assert hyd['trips']['cancelled'] == {'now': 3, 'last_week': 1}
        assert hyd['trips']['unfulfilled'] == {'now': 2, 'last_week': 1}
        assert hyd['rates']['unfulfilled_rate'] == {'now': 20.0, 'last_week': 20.0}
        assert hyd['rates']['acceptance_rate'] == {'now': 70.0, 'last_week': 80.0}
        assert hyd['rates']['avg_eta_minutes'] == {'now': 10.0, 'last_week': 6.0}
        assert hyd['money']['collected'] == {'INR': {'now_minor': 210_000, 'last_week_minor': 40_000}}
        assert hyd['currency'] == 'INR' and hyd['timezone'] == 'Asia/Kolkata'
        assert hyd['drivers']['online_now'] == {'now': 1, 'last_week': None}
        assert hyd['drivers']['online_source'] == 'db'
        assert hyd['drivers']['active']['now'] == 1
        assert hyd['riders']['active_24h'] == {'now': 2, 'last_week': 1}
        q = hyd['queues']
        assert q['sos_open']['now'] == 1 and 9.5 <= q['sos_oldest_open_minutes'] <= 10.5
        assert q['sos_raised'] == {'now': 1, 'last_week': 0}
        assert q['tickets_open']['now'] == 1 and q['tickets_raised']['now'] == 1
        assert q['withdrawals_pending']['now'] == 1
        assert 59.9 <= q['withdrawals_oldest_pending_hours'] <= 60.1
        assert hyd['money']['withdrawals_pending_sum'] == {'INR': {'now_minor': 100_000, 'last_week_minor': None}}
        assert q['kyc_awaiting']['now'] == 0
        assert q['alerts_open']['now'] == 0
        assert data['definitions']['acceptance_rate'].startswith('Trips accepted ÷ trips requested')

    def test_austin_row_and_zero_denominators(self, geo, world):
        client, _ = operator('super_admin', geo)
        _, data = _get(client, window='24h')
        aus = _row(data, 'US-TX-AUS')

        assert aus['trips']['requested'] == {'now': 4, 'last_week': 0}
        assert aus['rates']['unfulfilled_rate'] == {'now': 50.0, 'last_week': None}
        assert aus['rates']['acceptance_rate'] == {'now': 50.0, 'last_week': None}
        assert aus['rates']['avg_eta_minutes'] == {'now': 8.0, 'last_week': None}
        assert aus['money']['collected'] == {'USD': {'now_minor': 4_000, 'last_week_minor': 0}}
        assert aus['currency'] == 'USD' and aus['timezone'] == 'America/Chicago'
        assert aus['queues']['kyc_awaiting']['now'] == 1
        assert aus['queues']['sos_open']['now'] == 0 and aus['queues']['sos_oldest_open_minutes'] == 0.0
        assert aus['money']['withdrawals_pending_sum'] == {'USD': {'now_minor': 5_000, 'last_week_minor': None}}

    def test_money_is_per_currency_and_never_summed(self, geo, world):
        client, _ = operator('super_admin', geo)
        _, data = _get(client, window='24h')
        for row in data['cities']:
            assert set(row['money']['collected']) <= {row['currency']}, row['code']
            assert 'total' not in row['money'] and 'collected_total' not in row['money']
        assert 'INR' not in _row(data, 'US-TX-AUS')['money']['collected']
        assert 'USD' not in _row(data, 'IN-TG-HYD')['money']['collected']

    def test_the_7d_window_includes_last_weeks_day_on_its_edge(self, geo, world):
        """Same rows, wider window: the 24h figures are a subset of the 7d ones."""
        client, _ = operator('super_admin', geo)
        _, day = _get(client, window='24h')
        _, week = _get(client, window='7d')
        assert _row(week, 'IN-TG-HYD')['trips']['requested']['now'] >= _row(day, 'IN-TG-HYD')['trips']['requested']['now']
        assert week['window'] == '7d'

    def test_an_unknown_window_is_refused(self, geo, world):
        client, _ = operator('super_admin', geo)
        response = client.get(f'{OVERVIEW}?window=1h')
        assert response.status_code == 400
        assert response.json()['error']['code'] == 'INVALID_WINDOW'

    def test_presence_is_used_when_redis_answers(self, geo, world, monkeypatch):
        monkeypatch.setattr(overview, 'online_driver_ids', lambda: {world['aus_driver'].id})
        client, _ = operator('super_admin', geo)
        _, data = _get(client, window='24h')
        assert _row(data, 'IN-TG-HYD')['drivers']['online_now']['now'] == 0
        assert _row(data, 'US-TX-AUS')['drivers']['online_now']['now'] == 1
        assert _row(data, 'US-TX-AUS')['drivers']['online_source'] == 'presence'


class TestTodayIsTheCitysDay:
    def test_window_bounds_follow_the_city_clock(self):
        now = datetime(2026, 10, 28, 1, 30, tzinfo=dt_timezone.utc)
        ist_start, ist_end = overview.window_bounds('today', now, ZoneInfo('Asia/Kolkata'))
        cdt_start, cdt_end = overview.window_bounds('today', now, ZoneInfo('America/Chicago'))
        # 01:30Z is 07:00 IST on the 28th: the day began at 18:30Z on the 27th.
        assert ist_start == datetime(2026, 10, 27, 18, 30, tzinfo=dt_timezone.utc)
        # ...and 20:30 CDT on the 27th: the day began at 05:00Z on the 27th.
        assert cdt_start == datetime(2026, 10, 27, 5, 0, tzinfo=dt_timezone.utc)
        assert ist_end == cdt_end == now
        assert overview.last_week((ist_start, ist_end)) == (ist_start - WEEK, now - WEEK)

    def test_the_rolling_windows_are_the_same_everywhere(self):
        now = timezone.now()
        assert overview.window_bounds('24h', now, ZoneInfo('Asia/Kolkata')) == (now - timedelta(days=1), now)
        assert overview.window_bounds('7d', now, ZoneInfo('America/Chicago')) == (now - WEEK, now)

    def test_the_endpoint_reports_each_citys_window(self, geo, world):
        # Austin is paused and its fixture trips are two hours old, so in its
        # first two local hours "today" is empty and the row would drop out.
        # An online driver keeps the row whatever the clock says.
        world['aus_driver'].status = 'online'
        world['aus_driver'].approved = True
        world['aus_driver'].save(update_fields=['status', 'approved'])
        client, _ = operator('super_admin', geo)
        _, data = _get(client, window='today')
        hyd, aus = _row(data, 'IN-TG-HYD'), _row(data, 'US-TX-AUS')
        hyd_start = datetime.fromisoformat(hyd['window']['start']).astimezone(ZoneInfo('Asia/Kolkata'))
        aus_start = datetime.fromisoformat(aus['window']['start']).astimezone(ZoneInfo('America/Chicago'))
        assert (hyd_start.hour, hyd_start.minute) == (0, 0)
        assert (aus_start.hour, aus_start.minute) == (0, 0)


# ===========================================================================
# Who sees what
# ===========================================================================

class TestScope:
    def test_a_hyderabad_operator_sees_only_hyderabad(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        response, data = _get(client, window='24h')
        assert response.status_code == 200
        assert [c['code'] for c in data['cities']] == ['IN-TG-HYD']

    def test_a_hyderabad_operator_cannot_ask_for_austin(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        response = client.get(f'{OVERVIEW}?window=24h&scope=US-TX-AUS')
        assert response.status_code == 403
        assert response.json()['error']['code'] == 'SCOPE_NOT_HELD'

    def test_super_admin_sees_both_and_not_the_empty_paused_cities(self, geo, world):
        client, _ = operator('super_admin', geo)
        _, data = _get(client, window='24h')
        codes = [c['code'] for c in data['cities']]
        assert 'IN-TG-HYD' in codes and 'US-TX-AUS' in codes
        # Draft (never launched), no trips, no drivers: not a row.
        assert 'IN-TG-WGL' not in codes and 'IN-AP-VJA' not in codes
        assert _row(data, 'US-TX-AUS')['status'] == 'draft'

    def test_super_admin_can_narrow_to_one_city(self, geo, world):
        client, _ = operator('super_admin', geo)
        _, data = _get(client, window='24h', scope='US-TX-AUS')
        assert [c['code'] for c in data['cities']] == ['US-TX-AUS']
        assert data['scope'] == 'US-TX-AUS'

    def test_money_is_withheld_without_finance_read(self, geo, world):
        ops, _ = operator('city_ops_manager', geo)
        _, data = _get(ops, window='24h')
        assert data['money_visible'] is False
        hyd = _row(data, 'IN-TG-HYD')
        assert 'money' not in hyd
        assert 'withdrawals_pending' not in hyd['queues']
        assert hyd['trips']['requested']['now'] == 10  # the shared picture stays

        finance, _ = operator('finance', geo)
        _, data = _get(finance, window='24h')
        assert data['money_visible'] is True
        assert _row(data, 'IN-TG-HYD')['money']['collected']['INR']['now_minor'] == 210_000

    def test_an_operator_without_report_read_is_refused(self, geo, world):
        support, _ = operator('support_agent', geo)
        assert support.get(OVERVIEW).status_code == 403

    def test_the_route_is_gated_and_scoped(self):
        from servers.command_centre import views

        gates = {p.permission for p in views.admin_overview.cls.permission_classes
                 if hasattr(p, 'permission')}
        assert gates == {'report.read'}


class TestCache:
    def test_cached_per_scope_node_and_window(self, geo, world, settings):
        settings.COMMAND_CENTRE_OVERVIEW_CACHE_SECONDS = 30
        sa, _ = operator('super_admin', geo)
        first = _get(sa, window='24h')[1]
        second = _get(sa, window='24h')[1]
        assert first['cached'] is False and second['cached'] is True
        assert _get(sa, window='7d')[1]['cached'] is False
        assert _get(sa, window='24h', scope='IN-TG-HYD')[1]['cached'] is False
        # A different scope is a different board, even for the same window.
        ops, _ = operator('city_ops_manager', geo)
        assert _get(ops, window='24h')[1]['cached'] is False


# ===========================================================================
# Thresholds
# ===========================================================================

def _seed_thresholds():
    out = StringIO()
    call_command('seed_alert_thresholds', actor_email=seed_actor().email, stdout=out)
    return out.getvalue()


class TestThresholdSeed:
    def test_seeds_every_city_once_and_never_overwrites(self, geo):
        cities = GeoNode.objects.filter(kind='city').count()
        assert cities >= 2
        _seed_thresholds()
        assert AlertThreshold.objects.filter(is_active=True).count() == cities * len(METRICS)
        rows = AdminAuditLog.objects.filter(action='alert_thresholds_seeded').count()
        assert rows == 1

        hyd = geo['IN-TG-HYD']
        unf = thresholds.active_threshold(hyd, 'unfulfilled_rate')
        assert (unf.warn_value, unf.critical_value, unf.window) == (Decimal('15'), Decimal('30'), '24h')
        acc = thresholds.active_threshold(hyd, 'acceptance_rate')
        assert (acc.warn_value, acc.critical_value) == (Decimal('70'), Decimal('50'))
        eta = thresholds.active_threshold(hyd, 'avg_eta_minutes')
        assert (eta.warn_value, eta.critical_value) == (Decimal('8'), Decimal('12'))
        sos = thresholds.active_threshold(hyd, 'sos_open')
        assert (sos.warn_value, sos.critical_value, sos.window) == (None, Decimal('0'), 'now')
        unack = thresholds.active_threshold(hyd, 'sos_unacknowledged')
        assert (unack.warn_value, unack.critical_value) == (None, Decimal('5'))
        wd = thresholds.active_threshold(hyd, 'withdrawal_pending')
        assert (wd.warn_value, wd.critical_value) == (Decimal('48'), None)
        assert unf.created_by_email == seed_actor().email and unf.version == 1

        # A hand-set line survives a re-run.
        thresholds.write_threshold_version(city=hyd, metric='sos_unacknowledged', warn_value=None,
                                           critical_value=3, window='now', created_by=seed_actor(),
                                           reason='tighter')
        before = AlertThreshold.objects.count()
        _seed_thresholds()
        assert AlertThreshold.objects.count() == before
        assert thresholds.active_threshold(hyd, 'sos_unacknowledged').critical_value == Decimal('3')
        assert AdminAuditLog.objects.filter(action='alert_thresholds_seeded').count() == 1

    def test_the_seed_needs_an_actor(self, geo):
        from django.core.management.base import CommandError

        with pytest.raises(CommandError, match='--actor-email'):
            call_command('seed_alert_thresholds', stdout=StringIO())
        assert not AlertThreshold.objects.exists()

    def test_the_catalogue_is_listed_with_the_rows(self, geo, world):
        _seed_thresholds()
        ops, _ = operator('city_ops_manager', geo)
        response = ops.get(THRESHOLDS)
        assert response.status_code == 200
        data = response.json()['data']
        assert {m['key'] for m in data['metrics']} == set(METRICS)
        assert {r['city'] for r in data['results']} == {'IN-TG-HYD'}
        assert data['count'] == len(METRICS)


class TestThresholdChange:
    def _propose(self, client, **body):
        payload = {'city': 'IN-TG-HYD', 'metric': 'unfulfilled_rate', 'warn_value': 10,
                   'critical_value': 25, 'window': '24h', 'reason': 'festival weekend'}
        payload.update(body)
        return client.post(f'{THRESHOLDS}change/', payload, format='json')

    def test_a_change_waits_for_a_different_checker(self, geo, world):
        _seed_thresholds()
        maker_client, maker = operator('super_admin', geo)
        response = self._propose(maker_client)
        assert response.status_code == 202, response.content
        req_id = response.json()['data']['approval_request']['id']
        row = ApprovalRequest.objects.get(pk=req_id)
        assert row.status == REQUESTED and row.action_key == 'command_centre.threshold_change'
        assert row.scope_node.code == 'IN-TG-HYD' and row.country == 'IN'
        # Nothing written yet.
        assert thresholds.active_threshold(geo['IN-TG-HYD'], 'unfulfilled_rate').version == 1

        # The maker cannot be the checker.
        refused = maker_client.post(f'{APPROVALS}{req_id}/approve/', {'reason': 'ok'}, format='json')
        assert refused.status_code == 403
        assert refused.json()['error']['code'] == 'SELF_APPROVAL'

        checker_client, checker = operator('super_admin', geo)
        approved = checker_client.post(f'{APPROVALS}{req_id}/approve/', {'reason': 'agreed'}, format='json')
        assert approved.status_code == 200, approved.content
        row.refresh_from_db()
        assert row.status == EXECUTED, row.error
        current = thresholds.active_threshold(geo['IN-TG-HYD'], 'unfulfilled_rate')
        assert current.version == 2
        assert (current.warn_value, current.critical_value) == (Decimal('10'), Decimal('25'))
        assert current.changed_via_id == row.id and current.created_by_id == maker.id
        superseded = AlertThreshold.objects.get(city=geo['IN-TG-HYD'], metric='unfulfilled_rate', version=1)
        assert superseded.is_active is False and superseded.valid_to is not None
        audit = AdminAuditLog.objects.filter(action='alert_threshold_changed').first()
        assert audit is not None and audit.actor_id == checker.id
        assert audit.after['version'] == 2 and audit.before['version'] == 1

    def test_the_same_proposal_twice_is_one_request(self, geo, world):
        _seed_thresholds()
        client, _ = operator('super_admin', geo)
        first = self._propose(client).json()['data']
        second = self._propose(client).json()['data']
        assert first['approval_request']['id'] == second['approval_request']['id']
        assert second['duplicate'] is True

    def test_needs_platform_settings_over_the_city(self, geo, world):
        _seed_thresholds()
        ops, _ = operator('city_ops_manager', geo)   # no platform.settings
        assert self._propose(ops).status_code == 403
        assert ApprovalRequest.objects.count() == 0

    def test_validation(self, geo, world):
        _seed_thresholds()
        client, _ = operator('super_admin', geo)
        assert self._propose(client, warn_value=40, critical_value=30).json()['error']['code'] == 'LINES_INVERTED'
        assert self._propose(client, metric='accptance').json()['error']['code'] == 'UNKNOWN_METRIC'
        assert self._propose(client, reason='').json()['error']['code'] == 'REASON_REQUIRED'
        assert self._propose(client, warn_value=None, critical_value=None).json()['error']['code'] == 'NO_LINE'
        assert self._propose(client, metric='sos_open', window='24h').json()['error']['code'] == 'INVALID_WINDOW'
        assert self._propose(client, city='IN-TG').status_code == 404
        assert ApprovalRequest.objects.count() == 0


# ===========================================================================
# The sweep
# ===========================================================================

def _open(kind, city_code):
    return OperatorAlert.objects.filter(kind=kind, city__code=city_code, acknowledged_at__isnull=True)


class TestSweep:
    def test_raises_once_honours_the_sample_and_clears(self, geo, world):
        _seed_thresholds()

        result = evaluate_alert_thresholds()
        assert result['raised'] >= 1

        # Hyderabad: an open SOS (critical), unacknowledged for 10 min (> 5,
        # critical), a payout waiting 60 h (> 48, warning), 20 % unfulfilled
        # over 10 requests (> 15, warning), ETA 10 min (> 8, warning);
        # acceptance 70 % is on the line, not under it.
        assert _open('threshold_sos_open', 'IN-TG-HYD').get().severity == 'critical'
        unack = _open('threshold_sos_unacknowledged', 'IN-TG-HYD').get()
        assert unack.severity == 'critical' and unack.payload['threshold'] == '5'
        assert 9 <= unack.payload['value'] <= 11
        wd = _open('threshold_withdrawal_pending', 'IN-TG-HYD').get()
        assert wd.severity == 'warning' and wd.payload['city'] == 'IN-TG-HYD'
        unf = _open('threshold_unfulfilled_rate', 'IN-TG-HYD').get()
        assert unf.severity == 'warning' and unf.payload['value'] == 20.0 and unf.payload['threshold'] == '15'
        assert _open('threshold_avg_eta_minutes', 'IN-TG-HYD').get().severity == 'warning'
        assert not _open('threshold_acceptance_rate', 'IN-TG-HYD').exists()
        # Austin: 50 % unfulfilled, but on 4 requests -- under the minimum
        # sample of 5, so no alert; nothing else crosses a line.
        assert not OperatorAlert.objects.filter(city__code='US-TX-AUS').exists()

        # Run again: nothing doubles.
        count = OperatorAlert.objects.count()
        again = evaluate_alert_thresholds()
        assert again['raised'] == 0 and OperatorAlert.objects.count() == count

        # The SOS is resolved: both SOS alerts clear, by nobody, on the record.
        SOSEvent.objects.filter(pk=world['sos'].pk).update(status='resolved')
        third = evaluate_alert_thresholds()
        assert third['cleared'] == 2
        for kind in ('threshold_sos_open', 'threshold_sos_unacknowledged'):
            assert not _open(kind, 'IN-TG-HYD').exists()
            closed = OperatorAlert.objects.get(kind=kind, city__code='IN-TG-HYD')
            assert closed.acknowledged_by_id is None
            assert closed.acknowledged_note.startswith('cleared:')
        cleared_rows = AdminAuditLog.objects.filter(action='operator_alert_cleared')
        assert cleared_rows.count() == 2
        assert all(r.actor_label == 'system:alert_thresholds' for r in cleared_rows)
        # The payout alert is still open: nothing changed there.
        assert _open('threshold_withdrawal_pending', 'IN-TG-HYD').exists()

    def test_escalates_in_place(self, geo, world):
        _seed_thresholds()
        evaluate_alert_thresholds()
        alert = _open('threshold_unfulfilled_rate', 'IN-TG-HYD').get()
        assert alert.severity == 'warning'
        # Four more unfulfilled requests: 6 of 14 = 42.9 %, over the 30 % line.
        for _ in range(4):
            _unfulfilled(world['rider'], world['hyd_zone'], world['now'] - HOUR)
        evaluate_alert_thresholds()
        alert.refresh_from_db()
        assert alert.is_open and alert.severity == 'critical'
        assert alert.payload['value'] == 42.9 and alert.payload['threshold'] == '30'
        assert _open('threshold_unfulfilled_rate', 'IN-TG-HYD').count() == 1

    def test_the_sweep_is_scheduled_every_minute(self):
        entry = next(e for e in django_settings.CELERY_BEAT_SCHEDULE.values()
                     if e['task'] == 'command_centre.evaluate_alert_thresholds')
        assert entry['schedule'] == 60.0

    def test_a_zero_denominator_never_alerts(self, geo):
        """A city with no requests has rates of None, and None crosses nothing."""
        _seed_thresholds()
        _zone('IN-TG-HYD', geo['IN-TG-HYD'])
        result = evaluate_alert_thresholds()
        assert result['raised'] == 0
        assert not OperatorAlert.objects.exists()


# ===========================================================================
# Alerts: listing and acknowledging
# ===========================================================================

@pytest.fixture
def alerts(geo, world):
    _seed_thresholds()
    evaluate_alert_thresholds()
    unplaced = raise_alert('sos_unanchored', title='SOS #0 could not be routed', severity='critical',
                           source_type='sos_event', source_id='0')
    austin = raise_alert('threshold_sos_open', title='Austin: Open SOS 1', severity='critical',
                         source_type='alert_threshold', source_id='US-TX-AUS:sos_open',
                         city=geo['US-TX-AUS'], payload={'city': 'US-TX-AUS'})
    return {'unplaced': unplaced, 'austin': austin}


class TestAlertList:
    def test_filters(self, geo, world, alerts):
        sa, _ = operator('super_admin', geo)
        everything = sa.get(f'{ALERTS}?open=1').json()['data']
        assert everything['count'] >= 7
        hyd = sa.get(f'{ALERTS}?city=IN-TG-HYD').json()['data']
        assert {r['city'] for r in hyd['results']} == {'IN-TG-HYD'}
        india = sa.get(f'{ALERTS}?city=IN').json()['data']
        assert india['count'] == hyd['count']
        kind = sa.get(f'{ALERTS}?kind=threshold_sos_open').json()['data']
        assert {r['kind'] for r in kind['results']} == {'threshold_sos_open'}
        assert {r['city'] for r in kind['results']} == {'IN-TG-HYD', 'US-TX-AUS'}
        assert sa.get(f'{ALERTS}?city=XX').status_code == 404
        row = next(r for r in hyd['results'] if r['kind'] == 'threshold_unfulfilled_rate')
        assert row['payload']['metric'] == 'unfulfilled_rate' and row['ack_permission'] == 'trip.write'
        assert row['city_name'] == 'Hyderabad'

    def test_a_city_operator_sees_their_city_and_the_unplaced(self, geo, world, alerts):
        ops, _ = operator('city_ops_manager', geo)
        data = ops.get(ALERTS).json()['data']
        cities = {r['city'] for r in data['results']}
        assert cities == {'IN-TG-HYD', None}
        assert data['open_count'] == data['count']



class TestLiveSosOnlyForResponders:
    """Owner ruling 2026-10-30: live SOS (queue figures and alerts) goes to
    Safety, Super Admin and City Ops within scope; Marketing and Analyst see
    the rest of the board and no live SOS."""

    @pytest.mark.parametrize('role', ['marketing', 'analyst'])
    def test_reports_roles_get_no_sos_figures_or_alerts(self, geo, world, alerts, role):
        client, _ = operator(role, geo)
        _, data = _get(client, window='24h')
        assert data['sos_visible'] is False
        queues = _row(data, 'IN-TG-HYD')['queues']
        assert not {'sos_open', 'sos_oldest_open_minutes', 'sos_raised'} & set(queues)
        assert 'tickets_open' in queues  # the rest of the board stays
        listed = client.get(ALERTS).json()['data']
        kinds = {r['kind'] for r in listed['results']}
        assert listed['count'] > 0
        assert not kinds & {'sos_unanchored', 'threshold_sos_open', 'threshold_sos_unacknowledged'}
        assert listed['open_count'] == listed['count']

    @pytest.mark.parametrize('role', ['city_ops_manager', 'super_admin'])
    def test_responders_and_city_ops_keep_them(self, geo, world, alerts, role):
        client, _ = operator(role, geo)
        _, data = _get(client, window='24h')
        assert data['sos_visible'] is True
        assert 'sos_open' in _row(data, 'IN-TG-HYD')['queues']
        kinds = {r['kind'] for r in client.get(ALERTS).json()['data']['results']}
        assert 'threshold_sos_open' in kinds

class TestAcknowledge:
    def _ack(self, client, alert, note='taken'):
        return client.post(f'{ALERTS}{alert.id}/acknowledge/', {'note': note}, format='json')

    def test_the_kinds_own_permission_decides(self, geo, world, alerts):
        rate = _open('threshold_unfulfilled_rate', 'IN-TG-HYD').get()
        payout = _open('threshold_withdrawal_pending', 'IN-TG-HYD').get()
        sos = _open('threshold_sos_open', 'IN-TG-HYD').get()

        ops, ops_user = operator('city_ops_manager', geo)
        finance, _ = operator('finance', geo)
        safety, _ = operator('safety_officer', geo)
        analyst, _ = operator('analyst', geo)

        assert self._ack(analyst, rate).status_code == 403
        assert self._ack(ops, payout).status_code == 403
        assert self._ack(finance, rate).status_code == 403
        assert self._ack(ops, sos).status_code == 403

        assert self._ack(ops, rate).status_code == 200
        assert self._ack(finance, payout).status_code == 200
        assert self._ack(safety, sos).status_code == 200
        rate.refresh_from_db()
        assert rate.acknowledged_by_id == ops_user.id and rate.acknowledged_note == 'taken'
        rows = AdminAuditLog.objects.filter(action='operator_alert_acknowledged')
        assert rows.count() == 3
        assert rows.get(target_id=str(rate.id)).actor_id == ops_user.id
        assert rows.get(target_id=str(rate.id)).after['city'] == 'IN-TG-HYD'

        # Idempotent: a second ack is a 200 no-op and no second row.
        assert self._ack(ops, rate).status_code == 200
        assert AdminAuditLog.objects.filter(action='operator_alert_acknowledged').count() == 3

    def test_another_citys_alert_is_not_found(self, geo, world, alerts):
        ops, _ = operator('city_ops_manager', geo)   # Hyderabad
        assert self._ack(ops, alerts['austin']).status_code == 404
        aus_ops, _ = operator('city_ops_manager', geo, scope_code='US-TX-AUS')
        # Austin's SOS alert belongs to Safety, not City Ops, even in Austin.
        assert self._ack(aus_ops, alerts['austin']).status_code == 403
        aus_safety, _ = operator('safety_officer', geo, scope_code='US-TX-AUS')
        assert self._ack(aus_safety, alerts['austin']).status_code == 200

    def test_the_unplaced_sos_is_still_safetys_alone(self, geo, world, alerts):
        ops, _ = operator('city_ops_manager', geo)
        assert self._ack(ops, alerts['unplaced']).status_code == 403
        safety, _ = operator('safety_officer', geo)
        assert self._ack(safety, alerts['unplaced']).status_code == 200

    def test_the_table_of_who_acknowledges_what(self):
        assert ACK_PERMISSION['sos_unanchored'] == 'safety.respond'
        assert ACK_PERMISSION['threshold_unfulfilled_rate'] == 'trip.write'
        assert ACK_PERMISSION['threshold_acceptance_rate'] == 'trip.write'
        assert ACK_PERMISSION['threshold_avg_eta_minutes'] == 'trip.write'
        assert ACK_PERMISSION['threshold_sos_open'] == 'safety.respond'
        assert ACK_PERMISSION['threshold_sos_unacknowledged'] == 'safety.respond'
        assert ACK_PERMISSION['threshold_withdrawal_pending'] == 'finance.read'
