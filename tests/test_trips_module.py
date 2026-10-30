"""Trips module (console module 2): list, detail and the operator writes.

WHAT THESE PROVE
----------------
  * the list is scoped (Hyderabad ops see Hyderabad, Super Admin both, a
    refused ?scope= is 403), and every filter, the sort and the pages work
    server-side;
  * the date range is each trip's own city day (IST for Hyderabad, Central
    for Austin) in one query;
  * the stale filter uses the liveness settings: requested and never
    accepted past TRIP_STALE_AFTER_SECONDS, accepted and never arrived past
    TRIP_OPERATOR_ATTENTION_AFTER_SECONDS;
  * the detail carries the timeline, fare in minor units, payment summary,
    tickets, flags, notes, audit trail and the caller's allowed actions,
    and a strict viewer never sees a raw phone number;
  * each write: right capability, out of scope is 404, audited, idempotent;
  * refund thresholds (support 500 INR alone, 501 needs a checker; finance
    5,000 / 5,001), adjustment threshold (2,000), bulk over 50 needs a
    checker, a checker's approval runs the executor once, and an Austin
    trip answers THRESHOLD_NOT_CONFIGURED.
"""

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from conftest import seed_actor
from servers.admin_audit.models import AdminAuditLog
from servers.approvals.models import EXECUTED, REQUESTED, ApprovalRequest
from servers.driver.models import VehicleType
from servers.payments.models import Payment
from servers.ride.models import FarePricing, Trip, TripFlag, TripMoneyAction, TripNote, TripStatus
from servers.rider.models import Wallet
from test_sos_support_zones_matrix import (  # noqa: F401 - fixtures
    _driver, _rider, _sos, _ticket, _zone, geo, operator,
)

pytestmark = pytest.mark.django_db

LIST = '/api/v1/ride/admin/trips/'
APPROVALS = '/api/v1/approvals/admin/requests/'

HYD_PHONE = '+919876511101'
AUS_PHONE = '+15125511101'
AUSTIN_POLYGON = {
    'type': 'Polygon',
    'coordinates': [[[-97.85, 30.15], [-97.60, 30.15], [-97.60, 30.40],
                     [-97.85, 30.40], [-97.85, 30.15]]],
}


def detail(trip):
    return f'{LIST}{trip.id}/'


def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


def _trip(rider, zone, *, status='completed', driver=None, requested_at=None, fare=10_000,
          method='cash', paid=False, vehicle_type=None, **times):
    trip = Trip.objects.create(
        user_id=rider, driver_id=driver, status_id=_status(status), zone=zone,
        pickup_lat=Decimal('17.4'), pickup_long=Decimal('78.4'),
        destination_lat=Decimal('17.5'), destination_long=Decimal('78.5'),
        pickup_address='12 Road No 1, Banjara Hills, Hyderabad',
        estimated_fare_minor=fare, currency=zone.currency or 'INR',
        payment_method=method, payment_status='completed' if paid else 'pending',
        requested_vehicle_type=vehicle_type,
    )
    fields = {'requested_at': requested_at or timezone.now()}
    fields.update(times)
    Trip.objects.filter(pk=trip.pk).update(**fields)
    trip.refresh_from_db()
    return trip


def ids(response):
    assert response.status_code == 200, response.content
    return [row['id'] for row in response.json()['data']['results']]


@pytest.fixture
def policies(geo):
    call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=StringIO())


@pytest.fixture
def world(geo, settings):
    settings.RIDER_CREDIT_BALANCE_CAP = Decimal('100000.00')
    now = timezone.now()
    hyd = _zone('IN-TG-HYD', geo['IN-TG-HYD'])
    aus = _zone('US-TX-AUS', geo['US-TX-AUS'], country='US', state_code='TX',
                timezone_name='America/Chicago', polygon_geojson=AUSTIN_POLYGON,
                currency='USD')
    sedan, _ = VehicleType.objects.get_or_create(type='sedan')
    auto, _ = VehicleType.objects.get_or_create(type='auto')
    rider = _rider(HYD_PHONE, 'Lakshmi Devi')
    aus_rider = _rider(AUS_PHONE, 'Austin Rider')
    driver = _driver('+919876511102', 'Kiran Rao', geo['IN-TG-HYD'])
    aus_driver = _driver('+15125511102', 'Austin Driver', geo['US-TX-AUS'])

    paid = _trip(rider, hyd, driver=driver, fare=600_000, paid=True, method='cash',
                 vehicle_type=sedan, requested_at=now - timedelta(hours=3),
                 accepted_at=now - timedelta(hours=3) + timedelta(minutes=1),
                 reached_at=now - timedelta(hours=3) + timedelta(minutes=6),
                 started_at=now - timedelta(hours=3) + timedelta(minutes=8),
                 completed_at=now - timedelta(hours=2))
    online = _trip(rider, hyd, driver=driver, fare=700_000, paid=True, method='online',
                   vehicle_type=auto, requested_at=now - timedelta(hours=4),
                   completed_at=now - timedelta(hours=3, minutes=30))
    Payment.objects.create(trip_id=online, user_id=rider, amount_minor=700_000,
                           method='online', currency='INR', status='completed')
    searching = _trip(rider, hyd, status='requested', fare=5_000,
                      requested_at=now - timedelta(minutes=20))
    fresh = _trip(rider, hyd, status='requested', fare=5_100,
                  requested_at=now - timedelta(minutes=2))
    late = _trip(rider, hyd, status='accepted', driver=driver, fare=5_200,
                 requested_at=now - timedelta(minutes=45),
                 accepted_at=now - timedelta(minutes=40))
    riding = _trip(rider, hyd, status='in_progress', driver=driver, fare=5_300,
                   requested_at=now - timedelta(minutes=30),
                   accepted_at=now - timedelta(minutes=29),
                   reached_at=now - timedelta(minutes=25),
                   started_at=now - timedelta(minutes=20))
    aus_trip = _trip(aus_rider, aus, driver=aus_driver, fare=2_500, paid=True,
                     method='cash', requested_at=now - timedelta(hours=1),
                     completed_at=now - timedelta(minutes=30))
    FarePricing.objects.create(
        trip_id=paid, base_fare_minor=5_000, distance_fare_minor=500_000,
        time_fare_minor=95_000, surge_multiplier=Decimal('1.00'), total_fare_minor=600_000,
        currency='INR',
    )
    _sos(rider, paid)
    _ticket(rider, online)
    return {
        'hyd': hyd, 'aus': aus, 'rider': rider, 'driver': driver,
        'paid': paid, 'online': online, 'searching': searching, 'fresh': fresh,
        'late': late, 'riding': riding, 'aus_trip': aus_trip,
    }


HYD_TRIPS = ('paid', 'online', 'searching', 'fresh', 'late', 'riding')


# ---------------------------------------------------------------------------
# List: scope, filters, sort, pages
# ---------------------------------------------------------------------------

class TestList:
    def test_city_ops_sees_hyderabad_only_and_super_admin_sees_both(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        assert set(ids(client.get(LIST))) == {world[k].id for k in HYD_TRIPS}
        admin, _ = operator('super_admin', geo)
        assert world['aus_trip'].id in ids(admin.get(LIST))
        assert ids(admin.get(LIST + '?scope=US-TX-AUS')) == [world['aus_trip'].id]

    def test_a_scope_the_caller_does_not_hold_is_refused(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        response = client.get(LIST + '?scope=US-TX-AUS')
        assert response.status_code == 403
        assert response.json()['error']['code'] == 'SCOPE_NOT_HELD'

    @pytest.mark.parametrize('query, expected', [
        ('status=requested', ('searching', 'fresh')),
        ('status=requested,accepted', ('searching', 'fresh', 'late')),
        ('payment_method=online', ('online',)),
        ('vehicle_type=sedan', ('paid',)),
        ('has_sos=true', ('paid',)),
        ('has_ticket=1', ('online',)),
    ])
    def test_filters(self, geo, world, query, expected):
        client, _ = operator('city_ops_manager', geo)
        assert set(ids(client.get(f'{LIST}?{query}'))) == {world[k].id for k in expected}

    def test_driver_rider_id_and_flagged_filters(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        TripFlag.objects.create(trip=world['late'], reason='dispute')
        assert ids(client.get(f'{LIST}?flagged=true')) == [world['late'].id]
        assert set(ids(client.get(f'{LIST}?driver_id={world["driver"].id}'))) == {
            world[k].id for k in ('paid', 'online', 'late', 'riding')}
        assert len(ids(client.get(f'{LIST}?rider_id={world["rider"].id}'))) == 6
        assert ids(client.get(f'{LIST}?id={world["fresh"].id}')) == [world['fresh'].id]

    @pytest.mark.parametrize('query', ['driver_id=x', 'has_sos=maybe', 'date_from=2026/10/01',
                                       'ordering=rider', 'date_from=2026-10-05&date_to=2026-10-01'])
    def test_a_malformed_filter_is_400(self, geo, world, query):
        client, _ = operator('city_ops_manager', geo)
        assert client.get(f'{LIST}?{query}').status_code == 400

    def test_sort_and_pages(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        by_fare = ids(client.get(f'{LIST}?ordering=fare'))
        assert by_fare[0] == world['searching'].id and by_fare[-1] == world['online'].id
        page = client.get(f'{LIST}?ordering=-fare&page_size=4&page=2').json()['data']
        assert page['count'] == 6 and page['total_pages'] == 2 and page['page'] == 2
        assert [r['id'] for r in page['results']] == [world['fresh'].id, world['searching'].id]

    def test_rows_carry_minor_units_city_and_markers(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        rows = {r['id']: r for r in client.get(LIST).json()['data']['results']}
        row = rows[world['paid'].id]
        assert row['charged_fare_minor'] == 600_000 and row['currency'] == 'INR'
        assert row['city']['code'] == 'IN-TG-HYD'
        assert row['city']['timezone'] == 'Asia/Kolkata'
        assert row['has_sos'] is True and row['flagged'] is False

    def test_date_range_is_each_citys_own_day(self, geo, world):
        # 20:00 UTC on 1 Oct is 01:30 on 2 Oct in Hyderabad; 03:00 UTC on
        # 2 Oct is 22:00 on 1 Oct in Austin.
        Trip.objects.filter(pk=world['paid'].pk).update(
            requested_at=datetime(2026, 10, 1, 20, 0, tzinfo=dt_timezone.utc))
        Trip.objects.filter(pk=world['aus_trip'].pk).update(
            requested_at=datetime(2026, 10, 2, 3, 0, tzinfo=dt_timezone.utc))
        admin, _ = operator('super_admin', geo)
        assert ids(admin.get(f'{LIST}?date_from=2026-10-02&date_to=2026-10-02')) == [
            world['paid'].id]
        assert ids(admin.get(f'{LIST}?date_from=2026-10-01&date_to=2026-10-01')) == [
            world['aus_trip'].id]

    def test_stale_filter_uses_the_liveness_thresholds(self, geo, world, settings):
        settings.TRIP_STALE_AFTER_SECONDS = 600
        settings.TRIP_OPERATOR_ATTENTION_AFTER_SECONDS = 1800
        client, _ = operator('city_ops_manager', geo)
        stale = client.get(f'{LIST}?stale=true').json()['data']['results']
        assert {r['id']: r['stale_reason'] for r in stale} == {
            world['searching'].id: 'no_acceptance', world['late'].id: 'no_arrival'}
        assert world['fresh'].id in ids(client.get(f'{LIST}?stale=false'))
        assert world['searching'].id not in ids(client.get(f'{LIST}?stale=false'))

    def test_marketing_and_kyc_are_refused(self, geo, world):
        for role in ('marketing', 'kyc_verifier'):
            client, _ = operator(role, geo)
            assert client.get(LIST).status_code == 403


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------

class TestDetail:
    def test_everything_ops_needs_is_on_one_page(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        data = client.get(detail(world['paid'])).json()['data']
        assert [e['event'] for e in data['timeline_events']] == [
            'requested', 'accepted', 'arrived', 'started', 'completed']
        assert data['timeline_events'][1]['actor'] == {'type': 'driver', 'id': world['driver'].id}
        fare = data['fare_minor']
        assert fare['charged_minor'] == 600_000 and fare['currency'] == 'INR'
        assert fare['breakdown']['distance_minor'] == 500_000
        assert data['payment_summary']['refundable_minor'] == 600_000
        assert data['payment_summary']['payout']['status'] == 'unsettled'
        assert data['city']['timezone'] == 'Asia/Kolkata'
        assert len(data['sos_events']) == 1
        for key in ('flags', 'notes', 'money_actions', 'audit_trail', 'support_tickets',
                    'approval_requests', 'route', 'stale', 'actions'):
            assert key in data
        assert data['offers_recorded'] is False

    def test_actions_follow_the_role(self, geo, world):
        ops, _ = operator('city_ops_manager', geo)
        support, _ = operator('support_agent', geo)
        finance, _ = operator('finance', geo)
        searching = world['searching']
        a = ops.get(detail(searching)).json()['data']['actions']
        assert a['cancel'] and a['flag'] and a['note'] and not a['refund'] and not a['adjust']
        s = support.get(detail(world['paid'])).json()['data']['actions']
        assert s['refund'] and s['refund_action_key'] == 'support.refund' and not s['cancel']
        f = finance.get(detail(world['paid'])).json()['data']['actions']
        assert f['refund_action_key'] == 'finance.refund' and f['adjust'] and not f['flag']
        assert not ops.get(detail(world['riding'])).json()['data']['actions']['cancel']

    def test_a_strict_viewer_never_sees_the_phone(self, geo, world):
        for role in ('analyst', 'support_agent', 'city_ops_manager'):
            client, _ = operator(role, geo)
            for url in (detail(world['paid']), LIST):
                body = client.get(url).content.decode()
                assert HYD_PHONE not in body and HYD_PHONE[3:] not in body, (role, url)

    def test_out_of_scope_is_not_found(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        assert client.get(detail(world['aus_trip'])).status_code == 404


# ---------------------------------------------------------------------------
# Cancel, flag, note
# ---------------------------------------------------------------------------

def audit_rows(trip, action):
    return AdminAuditLog.objects.filter(target_type='trip', target_id=str(trip.id),
                                        action=action)


class TestCancel:
    def test_cancel_is_audited_and_idempotent(self, geo, world):
        client, user = operator('city_ops_manager', geo)
        trip = world['searching']
        body = {'reason_code': 'stale', 'note': 'nobody accepted', 'waive_fee': True}
        first = client.post(f'{detail(trip)}cancel/', body, format='json')
        assert first.status_code == 200, first.content
        trip.refresh_from_db()
        assert trip.status_id.status_code == 'cancelled' and trip.cancelled_by == 'admin'
        assert trip.cancellation_fee_minor == 0
        row = audit_rows(trip, 'trip_cancelled').get()
        assert row.actor_id == user.id and row.after['fee_waived'] is True
        again = client.post(f'{detail(trip)}cancel/', body, format='json')
        assert again.status_code == 200 and again.json()['data']['already_cancelled'] is True
        assert audit_rows(trip, 'trip_cancelled').count() == 1

    def test_only_cancellable_states(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        for key in ('riding', 'paid'):
            r = client.post(f'{detail(world[key])}cancel/', {'reason_code': 'safety'},
                            format='json')
            assert r.status_code == 409 and r.json()['error']['code'] == 'NOT_CANCELLABLE'

    def test_reason_is_required_and_other_needs_a_note(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        url = f'{detail(world["fresh"])}cancel/'
        assert client.post(url, {}, format='json').status_code == 400
        assert client.post(url, {'reason_code': 'other'}, format='json').status_code == 400
        assert client.post(url, {'reason_code': 'nope'}, format='json').status_code == 400

    def test_permission_and_scope(self, geo, world):
        support, _ = operator('support_agent', geo)
        assert support.post(f'{detail(world["fresh"])}cancel/', {'reason_code': 'stale'},
                            format='json').status_code == 403
        ops, _ = operator('city_ops_manager', geo)
        assert ops.post(f'{detail(world["aus_trip"])}cancel/', {'reason_code': 'stale'},
                        format='json').status_code == 404


class TestFlagsAndNotes:
    def test_flag_unflag_is_idempotent_and_audited(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        trip = world['paid']
        first = client.post(f'{detail(trip)}flag/', {'reason': 'fraud_suspicion'}, format='json')
        assert first.status_code == 201
        second = client.post(f'{detail(trip)}flag/', {'reason': 'fraud_suspicion'}, format='json')
        assert second.status_code == 200 and second.json()['data']['created'] is False
        assert TripFlag.objects.filter(trip=trip, cleared_at__isnull=True).count() == 1
        assert audit_rows(trip, 'trip_flagged').count() == 1
        assert client.post(f'{detail(trip)}flag/', {'reason': 'bogus'},
                           format='json').status_code == 400
        cleared = client.post(f'{detail(trip)}unflag/', {'note': 'checked'}, format='json')
        assert cleared.json()['data']['cleared'] == 1
        assert client.post(f'{detail(trip)}unflag/', {}, format='json').json()['data']['cleared'] == 0
        assert audit_rows(trip, 'trip_unflagged').count() == 1

    def test_note_is_internal_audited_and_idempotent_by_key(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        trip = world['paid']
        body = {'body': 'Rider called about the route', 'idempotency_key': 'note-1'}
        assert client.post(f'{detail(trip)}note/', body, format='json').status_code == 201
        assert client.post(f'{detail(trip)}note/', body, format='json').status_code == 200
        assert TripNote.objects.filter(trip=trip).count() == 1
        assert audit_rows(trip, 'trip_note_added').count() == 1
        assert client.post(f'{detail(trip)}note/', {'body': ''}, format='json').status_code == 400
        notes = client.get(detail(trip)).json()['data']['notes']
        assert notes[0]['body'] == 'Rider called about the route'

    def test_analyst_cannot_write(self, geo, world):
        client, _ = operator('analyst', geo)
        assert client.post(f'{detail(world["paid"])}note/', {'body': 'x'},
                           format='json').status_code == 403


# ---------------------------------------------------------------------------
# Refunds and adjustments through maker-checker
# ---------------------------------------------------------------------------

def refund(client, trip, amount, key='k1', reason='late driver'):
    return client.post(f'{detail(trip)}refund/', {
        'amount_minor': amount, 'reason': reason, 'idempotency_key': key}, format='json')


class TestRefund:
    def test_support_refunds_500_alone_into_the_wallet(self, geo, world, policies):
        client, user = operator('support_agent', geo)
        trip = world['paid']
        response = refund(client, trip, 50_000)
        assert response.status_code == 200, response.content
        data = response.json()['data']
        assert data['state'] == 'executed'
        assert data['approval_request']['action_key'] == 'support.refund'
        action = TripMoneyAction.objects.get(trip=trip)
        assert (action.kind, action.amount_minor, action.method, action.status) == (
            'refund', 50_000, 'wallet_credit', 'completed')
        assert Wallet.objects.get(user_id=world['rider'], scope='rider').balance_minor == 50_000
        assert audit_rows(trip, 'trip_refunded').count() == 1
        # Same key: the same request back, nothing paid twice.
        again = refund(client, trip, 50_000)
        assert again.json()['data']['duplicate'] is True
        assert TripMoneyAction.objects.filter(trip=trip).count() == 1

    def test_support_501_needs_a_checker_who_runs_it_once(self, geo, world, policies):
        client, _ = operator('support_agent', geo)
        trip = world['paid']
        response = refund(client, trip, 50_100)
        assert response.status_code == 202, response.content
        request_id = response.json()['data']['approval_request']['id']
        assert ApprovalRequest.objects.get(pk=request_id).status == REQUESTED
        assert not TripMoneyAction.objects.filter(trip=trip).exists()
        checker, _ = operator('super_admin', geo)
        approved = checker.post(f'{APPROVALS}{request_id}/approve/', {'reason': 'ok'},
                                format='json')
        assert approved.status_code == 200, approved.content
        assert ApprovalRequest.objects.get(pk=request_id).status == EXECUTED
        action = TripMoneyAction.objects.get(trip=trip)
        assert action.amount_minor == 50_100 and action.approval_request_id == request_id
        assert checker.post(f'{APPROVALS}{request_id}/approve/', {'reason': 'ok'},
                            format='json').status_code in (200, 409)
        assert TripMoneyAction.objects.filter(trip=trip).count() == 1

    def test_finance_5000_alone_and_5001_with_a_checker(self, geo, world, policies):
        client, _ = operator('finance', geo)
        trip = world['online']
        ok = refund(client, trip, 500_000)
        assert ok.status_code == 200, ok.content
        assert ok.json()['data']['approval_request']['action_key'] == 'finance.refund'
        action = TripMoneyAction.objects.get(trip=trip)
        # Online payment: recorded as owed, no provider called (TODO(integration)).
        assert (action.method, action.status) == ('gateway', 'pending_gateway')
        over = refund(client, trip, 200_000 + 1, key='k2')
        assert over.status_code == 400  # only 2,000.00 left of 7,000.00 paid
        big = _trip(world['rider'], world['hyd'], fare=900_000, paid=True, method='online')
        Payment.objects.create(trip_id=big, user_id=world['rider'], amount_minor=900_000,
                               method='online', currency='INR', status='completed')
        assert refund(client, big, 500_100, key='k3').status_code == 202

    def test_refund_cannot_exceed_what_was_paid(self, geo, world, policies):
        client, _ = operator('support_agent', geo)
        response = refund(client, world['searching'], 100)
        assert response.status_code == 400
        assert response.json()['error']['code'] == 'REFUND_EXCEEDS_PAID'

    @pytest.mark.parametrize('amount', ['500', 500.0, True, 0, -1, None])
    def test_amount_must_be_positive_integer_minor_units(self, geo, world, policies, amount):
        client, _ = operator('support_agent', geo)
        assert refund(client, world['paid'], amount).status_code == 400

    def test_reason_and_key_are_required(self, geo, world, policies):
        client, _ = operator('support_agent', geo)
        url = f'{detail(world["paid"])}refund/'
        assert client.post(url, {'amount_minor': 100, 'idempotency_key': 'a'},
                           format='json').status_code == 400
        assert client.post(url, {'amount_minor': 100, 'reason': 'x'},
                           format='json').status_code == 400

    def test_a_us_trip_has_no_threshold(self, geo, world, policies):
        client, _ = operator('super_admin', geo)
        response = refund(client, world['aus_trip'], 100)
        assert response.status_code == 409
        assert response.json()['error']['code'] == 'THRESHOLD_NOT_CONFIGURED'

    def test_city_ops_and_analyst_cannot_refund(self, geo, world, policies):
        for role in ('city_ops_manager', 'analyst'):
            client, _ = operator(role, geo)
            assert refund(client, world['paid'], 100).status_code == 403


class TestAdjust:
    def adjust(self, client, trip, amount, key='a1'):
        return client.post(f'{detail(trip)}adjust-fare/', {
            'amount_minor': amount, 'reason': 'toll not charged', 'idempotency_key': key},
            format='json')

    def test_2000_alone_and_2001_with_a_checker(self, geo, world, policies):
        client, _ = operator('finance', geo)
        trip = world['paid']
        ok = self.adjust(client, trip, -200_000)
        assert ok.status_code == 200, ok.content
        row = TripMoneyAction.objects.get(trip=trip)
        assert (row.kind, row.amount_minor, row.status) == ('adjustment', -200_000, 'recorded')
        assert audit_rows(trip, 'trip_fare_adjusted').count() == 1
        assert self.adjust(client, trip, 200_100, key='a2').status_code == 202

    def test_bounds_and_permission(self, geo, world, policies):
        client, _ = operator('finance', geo)
        assert self.adjust(client, world['paid'], 0).status_code == 400
        assert self.adjust(client, world['paid'], -600_001).status_code == 400
        support, _ = operator('support_agent', geo)
        assert self.adjust(support, world['paid'], 100).status_code == 403


# ---------------------------------------------------------------------------
# Bulk
# ---------------------------------------------------------------------------

class TestBulk:
    URL = f'{LIST}bulk/'

    def test_small_bulk_runs_now_with_per_row_results(self, geo, world, policies):
        client, _ = operator('city_ops_manager', geo)
        trip_ids = [world['fresh'].id, world['riding'].id, world['aus_trip'].id]
        response = client.post(self.URL, {
            'action': 'cancel', 'trip_ids': trip_ids, 'reason_code': 'duplicate',
            'idempotency_key': 'b1'}, format='json')
        assert response.status_code == 200, response.content
        results = {r['id']: r['result'] for r in response.json()['data']['result']['results']}
        assert results == {world['fresh'].id: 'cancelled', world['riding'].id: 'not_cancellable',
                           world['aus_trip'].id: 'not_found'}
        assert audit_rows(world['fresh'], 'trip_cancelled').count() == 1

    def test_bulk_flag(self, geo, world, policies):
        client, _ = operator('city_ops_manager', geo)
        response = client.post(self.URL, {
            'action': 'flag', 'trip_ids': [world['paid'].id, world['online'].id],
            'reason': 'dispute', 'idempotency_key': 'b2'}, format='json')
        assert response.status_code == 200, response.content
        assert TripFlag.objects.filter(reason='dispute').count() == 2

    def test_over_50_needs_a_checker(self, geo, world, policies):
        client, _ = operator('city_ops_manager', geo)
        many = [_trip(world['rider'], world['hyd'], status='requested').id for _ in range(51)]
        response = client.post(self.URL, {
            'action': 'flag', 'trip_ids': many, 'reason': 'safety',
            'idempotency_key': 'b3'}, format='json')
        assert response.status_code == 202, response.content
        data = response.json()['data']
        assert data['approval_request']['action_key'] == 'bulk.action' and data['count'] == 51
        assert not TripFlag.objects.filter(reason='safety').exists()
        checker, _ = operator('super_admin', geo)
        rid = data['approval_request']['id']
        assert checker.post(f'{APPROVALS}{rid}/approve/', {'reason': 'ok'},
                            format='json').status_code == 200
        assert TripFlag.objects.filter(reason='safety').count() == 51

    def test_austin_bulk_has_no_threshold(self, geo, world, policies):
        client, _ = operator('super_admin', geo)
        response = client.post(self.URL, {
            'action': 'flag', 'trip_ids': [world['aus_trip'].id], 'reason': 'safety',
            'idempotency_key': 'b4'}, format='json')
        assert response.status_code == 409
        assert response.json()['error']['code'] == 'THRESHOLD_NOT_CONFIGURED'

    def test_validation(self, geo, world, policies):
        client, _ = operator('city_ops_manager', geo)
        for body in ({'action': 'delete', 'trip_ids': [1], 'idempotency_key': 'x'},
                     {'action': 'flag', 'trip_ids': [], 'reason': 'safety', 'idempotency_key': 'x'},
                     {'action': 'flag', 'trip_ids': [1], 'reason': 'safety'}):
            assert client.post(self.URL, body, format='json').status_code == 400
