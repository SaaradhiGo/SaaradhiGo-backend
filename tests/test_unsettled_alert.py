"""A trip left unsettled because no commission rule governs it raises a CRITICAL alert.

WHAT THESE PROVE (module 9 owner item)
--------------------------------------
  * The deferral path (`settle_or_defer`) raises ONE critical Command Centre alert of kind
    `settlement.no_commission_rule`, anchored to the trip's city, with no rider or driver
    detail in it. A second deferral of the same trip does not duplicate it.
  * Who sees it: `finance.read` holders in scope and Super Admin. City Ops, Marketing, Analyst
    (and Support) do not; Hyderabad finance does not see an Austin trip's alert.
  * It resolves itself when a rule exists and the hourly retry runs (or "Retry now" is used).
  * The Finance overview lists the unsettled trips inside `money_visible_to` scope, with the
    trip id, city, completed_at, fare, currency and the reason, and nothing about a person.
  * "Retry now" needs finance.adjust (the finance write capability), is audited, and needs no
    approval; out of scope is 404.
  * The hourly task is registered in the beat schedule.
"""

from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.conf import settings

from conftest import seed_actor
from finance_world import (
    client_for, make_driver, make_rider, make_trip, operator, zone,
)
from servers.admin_access.models import GeoNode
from servers.admin_audit.models import AdminAuditLog
from servers.command_centre.models import (
    CRITICAL, FINANCE_ALERT_KINDS, SETTLEMENT_NO_COMMISSION_RULE, OperatorAlert,
)
from servers.driver.utils import settle_or_defer
from servers.payments.models import TripSettlement
from servers.payments import unsettled
from servers.pricing.commission_models import ACTIVE, CommissionRule
from test_finance_actions import body_of, error_code, get, post

pytestmark = [pytest.mark.django_db, pytest.mark.no_default_commission]

ALERTS = '/api/v1/command-centre/admin/alerts/'
OVERVIEW = '/api/v1/payments/admin/overview/'
KIND = 'settlement.no_commission_rule'
LONG_AGO = datetime(2000, 1, 1, tzinfo=dt_timezone.utc)


@pytest.fixture
def geo(db):
    from conftest import seed_admin_access

    seed_admin_access()
    call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=StringIO())
    return {n.code: n for n in GeoNode.objects.all()}


@pytest.fixture
def world(geo):
    from types import SimpleNamespace

    hyd = zone('IN-TG-HYD', geo['IN-TG-HYD'], country='IN')
    aus = zone('US-TX-AUS', geo['US-TX-AUS'], country='US', state_code='TX',
               timezone_name='America/Chicago')
    in_driver = make_driver(geo, 'IN-TG-HYD', 'U1', currency='INR', calling='91')
    us_driver = make_driver(geo, 'US-TX-AUS', 'U2', currency='USD', calling='1')
    return SimpleNamespace(
        geo=geo, hyd=hyd, aus=aus, in_driver=in_driver, us_driver=us_driver,
        super=operator('super_admin', geo),
        hyd_finance=operator('finance', geo, 'IN-TG-HYD'),
        aus_finance=operator('finance', geo, 'US-TX-AUS'),
        hyd_ops=operator('city_ops_manager', geo, 'IN-TG-HYD'),
        marketing=operator('marketing', geo, 'IN-TG-HYD'),
        analyst=operator('analyst', geo, 'IN-TG-HYD'),
        support=operator('support_agent', geo, 'IN-TG-HYD'))


def stranded(world, which='hyd', fare='500.00'):
    """A completed trip with no commission rule: deferred, alert raised."""
    if which == 'hyd':
        trip = make_trip(world.hyd, make_rider(f'x{fare}', '91'), world.in_driver, fare=fare)
    else:
        trip = make_trip(world.aus, make_rider(f'y{fare}', '1'), world.us_driver, fare=fare)
    assert settle_or_defer(trip) is False
    return trip


def alerts_seen(client):
    data = get(client, f'{ALERTS}?kind={KIND}&status=all')
    return data['results']


def make_rule(country='IN', percent='15.00'):
    return CommissionRule.objects.create(
        country=country, geo_node=None, vehicle_type=None, percent=Decimal(percent),
        status=ACTIVE, version=1, effective_from=LONG_AGO, reason='test')


# ===========================================================================
# THE ALERT
# ===========================================================================

class TestAlertRaised:
    def test_a_deferred_settlement_raises_one_critical_alert_for_the_trips_city(self, world):
        trip = stranded(world)
        alert = OperatorAlert.objects.get(kind=SETTLEMENT_NO_COMMISSION_RULE)
        assert alert.severity == CRITICAL and alert.acknowledged_at is None
        assert alert.city.code == 'IN-TG-HYD'
        assert (alert.source_type, alert.source_id) == ('trip_settlement', str(trip.id))
        assert alert.payload['trip_id'] == trip.id and alert.payload['currency'] == 'INR'
        assert alert.payload['fare_minor'] == 50000
        assert not TripSettlement.objects.filter(trip=trip).exists()

    def test_the_alert_names_no_rider_or_driver(self, world):
        stranded(world)
        alert = OperatorAlert.objects.get(kind=SETTLEMENT_NO_COMMISSION_RULE)
        blob = f'{alert.title} {alert.payload}'
        for fragment in ('Rider', 'Kumar', 'Driver', 'Reddy', '+91', 'okaxis', '@'):
            assert fragment not in blob

    def test_a_second_deferral_of_the_same_trip_does_not_duplicate_it(self, world):
        trip = stranded(world)
        assert settle_or_defer(trip) is False
        assert settle_or_defer(trip) is False
        assert OperatorAlert.objects.filter(kind=SETTLEMENT_NO_COMMISSION_RULE).count() == 1

    def test_each_trip_gets_its_own_alert(self, world):
        stranded(world, fare='500.00')
        stranded(world, fare='600.00')
        assert OperatorAlert.objects.filter(kind=SETTLEMENT_NO_COMMISSION_RULE).count() == 2

    def test_a_trip_with_no_zone_still_raises_an_alert_with_no_city(self, world):
        trip = make_trip(world.hyd, make_rider('nz', '91'), world.in_driver, fare='700.00')
        type(trip).objects.filter(pk=trip.pk).update(zone=None)
        trip.refresh_from_db()
        assert settle_or_defer(trip) is False
        assert OperatorAlert.objects.get(kind=KIND).city_id is None

    def test_a_failure_to_raise_the_alert_never_fails_the_completion(self, world, monkeypatch):
        trip = make_trip(world.hyd, make_rider('boom', '91'), world.in_driver, fare='500.00')

        def boom(*a, **k):
            raise RuntimeError('alert table gone')

        monkeypatch.setattr('servers.payments.unsettled.raise_unsettled_alert', boom)
        assert settle_or_defer(trip) is False                       # still a clean deferral

    def test_the_kind_is_a_finance_kind_and_is_not_acknowledgeable_by_hand(self, world):
        from servers.command_centre.models import ACK_PERMISSION

        assert KIND in FINANCE_ALERT_KINDS and KIND not in ACK_PERMISSION
        stranded(world)
        alert = OperatorAlert.objects.get(kind=KIND)
        for who in (world.hyd_finance, world.super):
            client, _ = who
            r = client.post(f'{ALERTS}{alert.id}/acknowledge/', {'note': 'x'}, format='json')
            assert r.status_code == 403


# ===========================================================================
# WHO SEES IT
# ===========================================================================

class TestVisibility:
    def test_finance_in_scope_and_super_admin_see_it(self, world):
        trip = stranded(world)
        for who in (world.hyd_finance, world.super):
            client, _ = who
            rows = alerts_seen(client)
            assert [r['kind'] for r in rows] == [KIND]
            assert rows[0]['payload']['trip_id'] == trip.id

    @pytest.mark.parametrize('role', ['hyd_ops', 'marketing', 'analyst', 'support'])
    def test_city_ops_marketing_analyst_and_support_do_not(self, world, role):
        stranded(world)
        client, _ = getattr(world, role)
        try:
            rows = alerts_seen(client)
        except AssertionError:
            return                               # a role without report.read gets a 403: also hidden
        assert rows == []
        listing = get(client, f'{ALERTS}?status=all')
        assert all(r['kind'] != KIND for r in listing['results'])
        assert listing['open_count'] == 0

    def test_hyderabad_finance_does_not_see_an_austin_trips_alert(self, world):
        stranded(world, 'hyd')
        stranded(world, 'aus')
        hyd_rows = alerts_seen(world.hyd_finance[0])
        aus_rows = alerts_seen(world.aus_finance[0])
        super_rows = alerts_seen(world.super[0])
        assert {r['payload']['city'] for r in hyd_rows} == {'IN-TG-HYD'}
        assert {r['payload']['city'] for r in aus_rows} == {'US-TX-AUS'}
        assert len(super_rows) == 2

    def test_an_alert_with_no_city_is_for_unrestricted_callers_only(self, world):
        trip = make_trip(world.hyd, make_rider('nc', '91'), world.in_driver, fare='700.00')
        type(trip).objects.filter(pk=trip.pk).update(zone=None)
        trip.refresh_from_db()
        settle_or_defer(trip)
        assert alerts_seen(world.hyd_finance[0]) == []
        assert len(alerts_seen(world.super[0])) == 1

    def test_finance_read_held_in_one_city_and_ops_in_another_sees_only_the_finance_city(self, world):
        from servers.admin_access.models import AdminAssignment, AdminRole

        stranded(world, 'hyd')
        stranded(world, 'aus')
        client, user = operator('city_ops_manager', world.geo, 'US-TX-AUS')
        AdminAssignment.objects.create(
            admin=user, role=AdminRole.objects.get(code='finance'), scope=world.geo['IN-TG-HYD'])
        cities = {r['payload']['city'] for r in alerts_seen(client)}
        assert cities == {'IN-TG-HYD'}


# ===========================================================================
# RESOLUTION: the hourly task and "Retry now"
# ===========================================================================

class TestResolution:
    def test_the_alert_resolves_after_a_rule_exists_and_the_retry_runs(self, world):
        from servers.payments.tasks import retry_unsettled_trips

        trip = stranded(world)
        assert retry_unsettled_trips() == {'checked': 1, 'settled': 0, 'still_unsettled': 1}
        assert OperatorAlert.objects.filter(kind=KIND, acknowledged_at__isnull=True).count() == 1
        make_rule('IN')
        assert retry_unsettled_trips() == {'checked': 1, 'settled': 1, 'still_unsettled': 0}
        settlement = TripSettlement.objects.get(trip=trip)
        assert settlement.commission_percent == Decimal('15.00')
        alert = OperatorAlert.objects.get(kind=KIND)
        assert alert.acknowledged_at is not None and alert.acknowledged_by_id is None
        assert 'settled' in alert.acknowledged_note
        row = AdminAuditLog.objects.filter(action='operator_alert_cleared').latest('id')
        assert row.actor_label == 'system:unsettled_retry'
        assert retry_unsettled_trips() == {'checked': 0, 'settled': 0, 'still_unsettled': 0}

    def test_a_trip_settled_some_other_way_has_its_alert_cleared_by_the_sweep(self, world):
        from servers.payments.tasks import retry_unsettled_trips

        trip = stranded(world)
        make_rule('IN')
        from servers.driver.utils import credit_driver_wallet

        credit_driver_wallet(trip)                                  # settled outside the task
        assert retry_unsettled_trips()['settled'] == 1
        assert OperatorAlert.objects.get(kind=KIND).acknowledged_at is not None

    def test_a_new_occurrence_after_resolution_raises_a_new_alert(self, world):
        from servers.payments.tasks import retry_unsettled_trips

        stranded(world)
        make_rule('IN')
        retry_unsettled_trips()
        CommissionRule.objects.all().delete()
        stranded(world, fare='650.00')
        assert OperatorAlert.objects.filter(kind=KIND, acknowledged_at__isnull=True).count() == 1
        assert OperatorAlert.objects.filter(kind=KIND).count() == 2

    def test_the_hourly_task_is_registered_in_beat(self):
        entry = settings.CELERY_BEAT_SCHEDULE['payments-retry-unsettled-trips-hourly']
        assert entry['task'] == 'payments.retry_unsettled_trips'
        from servers.payments import tasks

        assert tasks.retry_unsettled_trips.name == 'payments.retry_unsettled_trips'


# ===========================================================================
# THE FINANCE OVERVIEW LIST AND RETRY NOW
# ===========================================================================

class TestOverviewAndRetry:
    def test_the_overview_lists_unsettled_trips_inside_money_scope(self, world):
        hyd = stranded(world, 'hyd')
        aus = stranded(world, 'aus')
        data = get(world.hyd_finance[0], OVERVIEW)
        assert [r['trip_id'] for r in data['unsettled_trips']] == [hyd.id]
        assert data['unsettled_trips_count'] == 1
        row = data['unsettled_trips'][0]
        assert set(row) == {'trip_id', 'city', 'completed_at', 'fare_minor', 'currency', 'reason'}
        assert row['city'] == {'code': 'IN-TG-HYD', 'name': 'Hyderabad'}
        assert (row['fare_minor'], row['currency']) == (50000, 'INR')
        assert 'no commission rule' in row['reason'].lower()
        seen_by_aus = get(world.aus_finance[0], OVERVIEW)
        assert [r['trip_id'] for r in seen_by_aus['unsettled_trips']] == [aus.id]
        both = get(world.super[0], OVERVIEW)
        assert {r['trip_id'] for r in both['unsettled_trips']} == {hyd.id, aus.id}

    def test_the_list_carries_no_rider_or_driver_detail(self, world):
        stranded(world)
        text = str(get(world.hyd_finance[0], OVERVIEW)['unsettled_trips'])
        for fragment in ('Rider', 'Kumar', 'Driver', 'Reddy', '+91', 'okaxis', 'rider', 'driver'):
            assert fragment not in text

    def test_a_settled_trip_leaves_the_list(self, world):
        stranded(world)
        make_rule('IN')
        from servers.payments.tasks import retry_unsettled_trips

        retry_unsettled_trips()
        data = get(world.hyd_finance[0], OVERVIEW)
        assert data['unsettled_trips'] == [] and data['unsettled_trips_count'] == 0

    def test_retry_now_settles_when_a_rule_exists_is_audited_and_needs_no_approval(self, world):
        trip = stranded(world)
        make_rule('IN')
        client, user = world.hyd_finance
        before = AdminAuditLog.objects.filter(action='unsettled_trip_retried').count()
        r = post(client, f'/api/v1/payments/admin/unsettled/{trip.id}/retry/',
                 {'note': 'Rule approved this morning'}, expect=200)
        assert body_of(r) == {'trip_id': trip.id, 'settled': True, 'still_unsettled_reason': None}
        assert TripSettlement.objects.filter(trip=trip).exists()
        assert OperatorAlert.objects.get(kind=KIND).acknowledged_at is not None
        row = AdminAuditLog.objects.filter(action='unsettled_trip_retried').latest('id')
        assert AdminAuditLog.objects.filter(action='unsettled_trip_retried').count() == before + 1
        assert row.actor_id == user.id and row.target_id == str(trip.id)
        assert row.reason == 'Rule approved this morning'

    def test_retry_now_with_no_rule_says_so_and_changes_nothing(self, world):
        trip = stranded(world)
        client, _ = world.hyd_finance
        r = post(client, f'/api/v1/payments/admin/unsettled/{trip.id}/retry/', {}, expect=200)
        assert body_of(r)['settled'] is False and body_of(r)['still_unsettled_reason']
        assert not TripSettlement.objects.filter(trip=trip).exists()
        assert OperatorAlert.objects.filter(kind=KIND, acknowledged_at__isnull=True).count() == 1

    def test_retry_now_is_404_out_of_scope_and_403_without_the_capability(self, world):
        austin_trip = stranded(world, 'aus')
        hyd_trip = stranded(world, 'hyd')
        hyd_client, _ = world.hyd_finance
        r = hyd_client.post(f'/api/v1/payments/admin/unsettled/{austin_trip.id}/retry/', {},
                            format='json')
        assert r.status_code == 404
        ops_client, _ = world.hyd_ops
        r = ops_client.post(f'/api/v1/payments/admin/unsettled/{hyd_trip.id}/retry/', {},
                            format='json')
        assert r.status_code == 403
        analyst_client, _ = world.analyst
        assert analyst_client.post(f'/api/v1/payments/admin/unsettled/{hyd_trip.id}/retry/', {},
                                   format='json').status_code == 403

    def test_a_settled_trip_is_not_retryable(self, world):
        trip = make_trip(world.hyd, make_rider('ok', '91'), world.in_driver, fare='500.00')
        client, _ = world.hyd_finance
        r = client.post(f'/api/v1/payments/admin/unsettled/{trip.id}/retry/', {}, format='json')
        assert r.status_code == 409 and error_code(r) == 'NOT_UNSETTLED'
