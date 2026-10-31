"""Stale-trip actions: "Mark reviewed" and "Release driver" (Trips follow-up).

WHAT THESE PROVE
----------------
  * the capability `trip.stale_resolve` is held by City Ops and Super Admin and
    nobody else; missing capability is 403, out of scope is 404 (a Hyderabad
    City Ops cannot act on an Austin trip);
  * a reason (5 to 500 characters) is required by both actions;
  * mark-reviewed clears the flag, records who and when, changes no status, and
    the same silence is not flagged again (new silence after activity is);
  * release-driver, case by case: A (finished trip, driver still marked busy),
    B (ghost trip before pickup: operator cancellation, no fee, marker cleared,
    rider told), C (started trip: needs a reason code; the money is proved in
    test_stale_started_release.py), D (not stale:
    refused, nothing changes);
  * no TripSettlement, wallet transaction, payment, transaction-history,
    withdrawal or payout-run row is created by either action in any case;
  * every attempt is audited, refusals included;
  * a second release of the same trip creates no second cancellation;
  * the detail payload tells the console which button to offer, and carries no
    contact detail.
"""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from conftest import seed_actor
from servers.admin_access.roles import ROLE_TEMPLATES
from servers.admin_audit.models import AdminAuditLog
from servers.driver.models import WithdrawalRequest
from servers.notifications.models import NotificationMessage
from servers.payments.models import (
    Payment, PayoutRun, TransactionHistory, TripSettlement,
)
from servers.redis_client import (
    clear_driver_active_trip, get_driver_active_trip, set_driver_active_trip,
)
from servers.ride.liveness import flag_stale_trips, record_driver_activity
from servers.ride.models import Trip, driver_active_trip_ids
from servers.rider.models import WalletTransaction
from test_sos_support_zones_matrix import (  # noqa: F401 - fixtures
    _driver, _rider, _zone, geo, operator,
)
from test_trips_module import AUSTIN_POLYGON, AUS_PHONE, HYD_PHONE, _trip

pytestmark = pytest.mark.django_db

LIST = '/api/v1/ride/admin/trips/'
GOOD = {'reason': 'Driver confirmed by phone, ride is fine'}

MONEY_MODELS = (TripSettlement, WalletTransaction, Payment, TransactionHistory,
                WithdrawalRequest, PayoutRun)


def url(trip, action):
    return f'{LIST}{trip.id}/stale/{action}/'


def money_rows():
    return {model.__name__: model.objects.count() for model in MONEY_MODELS}


def audit(action, trip):
    return AdminAuditLog.objects.filter(action=action, target_type='trip',
                                        target_id=str(trip.id))


@pytest.fixture
def world(geo, settings):
    from servers.driver.models import VehicleType

    now = timezone.now()
    hyd = _zone('IN-TG-HYD', geo['IN-TG-HYD'])
    aus = _zone('US-TX-AUS', geo['US-TX-AUS'], country='US', state_code='TX',
                timezone_name='America/Chicago', polygon_geojson=AUSTIN_POLYGON, currency='USD')
    VehicleType.objects.get_or_create(type='sedan')
    rider = _rider(HYD_PHONE, 'Lakshmi Devi')
    aus_rider = _rider(AUS_PHONE, 'Austin Rider')
    driver = _driver('+919876511102', 'Kiran Rao', geo['IN-TG-HYD'])
    aus_driver = _driver('+15125511102', 'Austin Driver', geo['US-TX-AUS'])
    call_command('seed_notification_templates', actor_email=seed_actor().email,
                 stdout=StringIO())

    def ghost(status='accepted', **extra):
        """A trip whose driver went silent 30 minutes ago and is flagged."""
        times = dict(
            requested_at=now - timedelta(minutes=45),
            accepted_at=now - timedelta(minutes=40),
            last_driver_activity_at=now - timedelta(minutes=30),
            stale_flagged_at=now - timedelta(minutes=5),
            stale_reason='driver_silent_long',
        )
        if status in ('reached', 'in_progress'):
            times['reached_at'] = now - timedelta(minutes=35)
        if status == 'in_progress':
            times['started_at'] = now - timedelta(minutes=33)
        times.update(extra)
        return _trip(rider, hyd, status=status, driver=driver, fare=5_300, **times)

    clear_driver_active_trip(driver.id)
    clear_driver_active_trip(aus_driver.id)
    yield {
        'now': now, 'hyd': hyd, 'aus': aus, 'rider': rider, 'driver': driver,
        'aus_rider': aus_rider, 'aus_driver': aus_driver, 'ghost': ghost,
        'aus_ghost': lambda: _trip(
            aus_rider, aus, status='accepted', driver=aus_driver, fare=2_500,
            requested_at=now - timedelta(minutes=45), accepted_at=now - timedelta(minutes=40),
            last_driver_activity_at=now - timedelta(minutes=30),
            stale_flagged_at=now - timedelta(minutes=5), stale_reason='driver_silent_long'),
    }
    clear_driver_active_trip(driver.id)
    clear_driver_active_trip(aus_driver.id)


def post(client, trip, action, body=None):
    return client.post(url(trip, action), body if body is not None else GOOD, format='json')


# ---------------------------------------------------------------------------
# Capability, scope, reason
# ---------------------------------------------------------------------------

class TestCapability:
    def test_exactly_city_ops_and_super_admin_hold_it(self):
        holders = {t['code'] for t in ROLE_TEMPLATES if 'trip.stale_resolve' in t['permissions']}
        assert holders == {'super_admin', 'city_ops_manager'}

    @pytest.mark.parametrize('action', ['mark-reviewed', 'release-driver'])
    @pytest.mark.parametrize('role', ['support_agent', 'support_lead', 'safety_officer',
                                      'finance', 'analyst', 'marketing', 'kyc_verifier'])
    def test_every_other_role_is_403(self, geo, world, role, action):
        trip = world['ghost']()
        client, _ = operator(role, geo)
        response = post(client, trip, action)
        assert response.status_code in (403, 404), (role, response.status_code)
        if role in ('support_agent', 'support_lead', 'safety_officer', 'finance', 'analyst'):
            assert response.status_code == 403
        trip.refresh_from_db()
        assert trip.status_id.status_code == 'accepted' and trip.stale_flagged_at is not None

    @pytest.mark.parametrize('action', ['mark-reviewed', 'release-driver'])
    def test_a_hyderabad_city_ops_cannot_act_on_an_austin_trip(self, geo, world, action):
        trip = world['aus_ghost']()
        client, _ = operator('city_ops_manager', geo)
        assert post(client, trip, action).status_code == 404
        trip.refresh_from_db()
        assert trip.status_id.status_code == 'accepted' and trip.stale_flagged_at is not None
        assert not AdminAuditLog.objects.filter(target_type='trip', target_id=str(trip.id)).exists()

    def test_super_admin_can_act_on_an_austin_trip(self, geo, world):
        trip = world['aus_ghost']()
        client, _ = operator('super_admin', geo)
        assert post(client, trip, 'mark-reviewed').status_code == 200

    @pytest.mark.parametrize('action', ['mark-reviewed', 'release-driver'])
    def test_unauthenticated_is_refused(self, geo, world, action):
        from rest_framework.test import APIClient

        trip = world['ghost']()
        assert post(APIClient(), trip, action).status_code in (401, 403)


class TestReasonRequired:
    @pytest.mark.parametrize('action', ['mark-reviewed', 'release-driver'])
    @pytest.mark.parametrize('body', [{}, {'reason': ''}, {'reason': '    '}, {'reason': 'abcd'},
                                      {'reason': 'x' * 501}])
    def test_a_missing_short_or_long_reason_is_400_and_nothing_changes(
            self, geo, world, action, body):
        trip = world['ghost']()
        client, _ = operator('city_ops_manager', geo)
        before = money_rows()
        response = post(client, trip, action, body)
        assert response.status_code == 400, response.content
        trip.refresh_from_db()
        assert trip.status_id.status_code == 'accepted' and trip.stale_flagged_at is not None
        assert money_rows() == before
        assert not AdminAuditLog.objects.filter(action__startswith='trip_stale').exists()

    def test_five_and_five_hundred_characters_are_accepted(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        assert post(client, world['ghost'](), 'mark-reviewed', {'reason': 'abcde'}).status_code == 200
        assert post(client, world['ghost'](), 'mark-reviewed',
                    {'reason': 'x' * 500}).status_code == 200


# ---------------------------------------------------------------------------
# Mark reviewed
# ---------------------------------------------------------------------------

class TestMarkReviewed:
    def test_it_clears_the_flag_records_who_and_leaves_the_trip_alone(self, geo, world):
        trip = world['ghost'](status='in_progress')
        client, user = operator('city_ops_manager', geo)
        before = money_rows()
        response = post(client, trip, 'mark-reviewed')
        assert response.status_code == 200, response.content
        assert response.json()['data']['outcome'] == 'marked_reviewed'
        trip.refresh_from_db()
        assert trip.status_id.status_code == 'in_progress'
        assert trip.stale_flagged_at is None and trip.stale_reason == ''
        assert trip.stale_reviewed_at is not None and trip.stale_reviewed_by_id == user.id
        assert trip.estimated_fare_minor == 5_300 and trip.cancelled_by == ''
        assert money_rows() == before
        row = audit('trip_stale_marked_reviewed', trip).get()
        assert row.actor_id == user.id and row.reason == GOOD['reason']
        assert row.before['stale_reason'] == 'driver_silent_long'
        assert row.after['stale_flagged_at'] is None and row.after['stale_reviewed_at']

    def test_a_trip_that_is_not_stale_is_409_with_a_code_and_an_audit_row(self, geo, world):
        trip = world['ghost'](stale_flagged_at=None, stale_reason='',
                              last_driver_activity_at=world['now'] - timedelta(seconds=20),
                              accepted_at=world['now'] - timedelta(minutes=2))
        client, _ = operator('city_ops_manager', geo)
        response = post(client, trip, 'mark-reviewed')
        assert response.status_code == 409
        assert response.json()['error']['code'] == 'not_stale'
        trip.refresh_from_db()
        assert trip.stale_reviewed_at is None
        assert audit('trip_stale_marked_reviewed_refused', trip).count() == 1
        assert not audit('trip_stale_marked_reviewed', trip).exists()

    def test_a_time_based_stale_trip_leaves_the_stale_filter_once_reviewed(self, geo, world):
        late = world['ghost'](stale_flagged_at=None, stale_reason='',
                              last_driver_activity_at=None)
        client, _ = operator('city_ops_manager', geo)
        listed = [r['id'] for r in client.get(LIST + '?stale=true').json()['data']['results']]
        assert late.id in listed
        assert post(client, late, 'mark-reviewed').status_code == 200
        listed = [r['id'] for r in client.get(LIST + '?stale=true').json()['data']['results']]
        assert late.id not in listed
        body = client.get(f'{LIST}{late.id}/').json()['data']
        assert body['stale']['is_stale'] is False


class TestReviewedIsNotReflagged:
    def test_the_same_silence_is_not_flagged_again(self, geo, world):
        trip = world['ghost']()
        client, _ = operator('city_ops_manager', geo)
        assert post(client, trip, 'mark-reviewed').status_code == 200
        later = world['now'] + timedelta(minutes=15)
        summary = flag_stale_trips(now=later)
        trip.refresh_from_db()
        assert trip.stale_flagged_at is None
        assert summary['flagged'] == 0 and summary['skipped_reviewed'] >= 1

    def test_new_silence_after_driver_activity_is_flagged_again(self, geo, world):
        trip = world['ghost']()
        client, _ = operator('city_ops_manager', geo)
        assert post(client, trip, 'mark-reviewed').status_code == 200
        trip.refresh_from_db()
        reviewed_at = trip.stale_reviewed_at
        # The driver comes back after the review...
        assert record_driver_activity(trip.id, when=reviewed_at + timedelta(minutes=1))
        # ...and is quiet again for longer than the stale threshold.
        summary = flag_stale_trips(now=reviewed_at + timedelta(minutes=15))
        trip.refresh_from_db()
        assert summary['flagged'] == 1
        assert trip.stale_flagged_at is not None and trip.stale_reason == 'driver_silent'
        assert trip.stale_reviewed_at == reviewed_at


# ---------------------------------------------------------------------------
# Release driver
# ---------------------------------------------------------------------------

class TestCaseB:
    @pytest.mark.parametrize('status', ['accepted', 'reached'])
    def test_a_ghost_trip_before_pickup_is_cancelled_free_and_the_driver_is_freed(
            self, geo, world, status, django_capture_on_commit_callbacks):
        trip = world['ghost'](status=status, cancellation_fee_minor=500)
        driver = world['driver']
        set_driver_active_trip(driver.id, trip.id)
        client, user = operator('city_ops_manager', geo)
        before = money_rows()
        with django_capture_on_commit_callbacks(execute=True):
            response = post(client, trip, 'release-driver')
        assert response.status_code == 200, response.content
        data = response.json()['data']
        assert data['case'] == 'B' and data['outcome'] == 'cancelled_and_released'
        assert data['driver_released'] is True
        assert data['rider_charged_minor'] == 0 and data['driver_fee_minor'] == 0

        trip.refresh_from_db()
        assert trip.status_id.status_code == 'cancelled' and trip.cancelled_by == 'admin'
        assert trip.cancellation_reason == 'operator_released_stale'
        assert trip.cancellation_fee_minor == 0
        assert trip.stale_flagged_at is None
        assert trip.estimated_fare_minor == 5_300
        # Released: not busy in Redis, not busy in the database.
        assert get_driver_active_trip(driver.id) is None
        assert driver_active_trip_ids(driver) == []
        assert money_rows() == before

        released = audit('trip_stale_driver_released', trip).get()
        assert released.actor_id == user.id and released.reason == GOOD['reason']
        assert released.after['case'] == 'B' and released.after['driver_id'] == driver.id
        assert released.after['cancellation_fee_minor'] == 0
        cancelled = audit('trip_cancelled', trip).get()
        assert cancelled.after['fee_waived'] is True and cancelled.reason == GOOD['reason']

    def test_the_rider_is_told_through_notify_and_the_driver_through_the_app(
            self, geo, world, django_capture_on_commit_callbacks):
        from servers.rider.models import Notification

        trip = world['ghost']()
        client, _ = operator('city_ops_manager', geo)
        with django_capture_on_commit_callbacks(execute=True):
            assert post(client, trip, 'release-driver').status_code == 200
        sent = NotificationMessage.objects.filter(
            template_key='trips.operator_cancelled', recipient_user_id=world['rider'].id)
        assert set(sent.values_list('channel', flat=True)) == {'in_app', 'push'}
        assert all(str(trip.id) in m.body for m in sent if m.body)
        assert Notification.objects.filter(user_id=world['driver'].user_id, trip_id=trip.id,
                                           title='Ride Cancelled').exists()
        # One notice per channel, even if the release is repeated.
        post(client, trip, 'release-driver')
        assert NotificationMessage.objects.filter(
            template_key='trips.operator_cancelled',
            recipient_user_id=world['rider'].id).count() == 2

    def test_the_trips_ride_socket_gets_a_cancel_event(
            self, geo, world, django_capture_on_commit_callbacks):
        from unittest import mock

        trip = world['ghost']()
        client, _ = operator('city_ops_manager', geo)
        sent = []

        class Layer:
            async def group_send(self, group, event):
                sent.append((group, event['type'], event['status']))

        with mock.patch('channels.layers.get_channel_layer', return_value=Layer()):
            with django_capture_on_commit_callbacks(execute=True):
                assert post(client, trip, 'release-driver').status_code == 200
        assert (f'trip_{trip.id}', 'trip_status_update', 'cancel') in sent
        assert (f'rider_{world["rider"].id}', 'trip_update', 'cancel') in sent

    def test_an_online_payment_already_taken_goes_back_to_the_rider(
            self, geo, world, django_capture_on_commit_callbacks):
        from servers.payments.payment_gateways import factory

        trip = world['ghost']()
        Payment.objects.create(trip_id=trip, user_id=world['rider'], amount_minor=5_300,
                               method='online', currency='INR', status='completed',
                               gateway_payment_id='pay_test')

        class Gateway:
            def create_refund(self, payment_id):
                return {'id': 'rfnd_test'}

        client, _ = operator('city_ops_manager', geo)
        from unittest import mock

        with mock.patch.object(factory, 'get_payment_gateway', return_value=Gateway()):
            with django_capture_on_commit_callbacks(execute=True):
                assert post(client, trip, 'release-driver').status_code == 200
        assert Payment.objects.get(trip_id=trip).status == 'refunded'
        assert Payment.objects.count() == 1          # nothing new was created


class TestCaseA:
    def test_a_finished_trip_whose_driver_is_still_marked_busy_is_repaired(self, geo, world):
        trip = world['ghost'](status='completed', stale_flagged_at=None, stale_reason='',
                              completed_at=world['now'])
        set_driver_active_trip(world['driver'].id, trip.id)
        client, user = operator('city_ops_manager', geo)
        before = money_rows()
        response = post(client, trip, 'release-driver')
        assert response.status_code == 200, response.content
        data = response.json()['data']
        assert data['case'] == 'A' and data['outcome'] == 'repaired_cleared'
        assert data['driver_released'] is True
        assert get_driver_active_trip(world['driver'].id) is None
        trip.refresh_from_db()
        assert trip.status_id.status_code == 'completed' and trip.cancelled_by == ''
        assert money_rows() == before
        row = audit('trip_stale_driver_released', trip).get()
        assert row.after['case'] == 'A' and row.after['outcome'] == 'repaired_cleared'

    def test_nothing_to_repair_is_a_409_not_a_success(self, geo, world):
        trip = world['ghost'](status='cancelled', stale_flagged_at=None, stale_reason='')
        client, _ = operator('city_ops_manager', geo)
        response = post(client, trip, 'release-driver')
        assert response.status_code == 409
        assert response.json()['error']['code'] == 'nothing_to_release'
        assert audit('trip_stale_driver_released_refused', trip).count() == 1


class TestCaseC:
    def test_a_started_trip_without_a_reason_code_is_a_400_and_nothing_changes(
            self, geo, world):
        trip = world['ghost'](status='in_progress')
        driver = world['driver']
        set_driver_active_trip(driver.id, trip.id)
        client, _ = operator('city_ops_manager', geo)
        before = money_rows()
        response = post(client, trip, 'release-driver')
        assert response.status_code == 400
        assert response.json()['error']['code'] == 'REASON_CODE_REQUIRED'
        trip.refresh_from_db()
        assert trip.status_id.status_code == 'in_progress' and trip.cancelled_by == ''
        assert trip.stale_flagged_at is not None
        assert str(get_driver_active_trip(driver.id)) == str(trip.id)
        assert money_rows() == before
        assert not audit('trip_stale_driver_released', trip).exists()
        assert not audit('trip_cancelled', trip).exists()


class TestCaseD:
    @pytest.mark.parametrize('status', ['accepted', 'reached', 'in_progress'])
    def test_an_alive_trip_is_refused_and_nothing_changes(self, geo, world, status):
        trip = world['ghost'](status=status, stale_flagged_at=None, stale_reason='',
                              last_driver_activity_at=world['now'] - timedelta(seconds=15),
                              accepted_at=world['now'] - timedelta(minutes=3))
        driver = world['driver']
        set_driver_active_trip(driver.id, trip.id)
        client, _ = operator('city_ops_manager', geo)
        response = post(client, trip, 'release-driver')
        assert response.status_code == 409
        assert response.json()['error']['code'] == 'trip_active'
        trip.refresh_from_db()
        assert trip.status_id.status_code == status
        assert str(get_driver_active_trip(driver.id)) == str(trip.id)
        assert audit('trip_stale_driver_released_refused', trip).get().after['case'] == 'D'

    def test_a_trip_with_no_driver_is_refused(self, geo, world):
        trip = _trip(world['rider'], world['hyd'], status='requested', fare=5_000,
                     requested_at=world['now'] - timedelta(minutes=20))
        client, _ = operator('city_ops_manager', geo)
        response = post(client, trip, 'release-driver')
        assert response.status_code == 409
        assert response.json()['error']['code'] == 'no_driver_assigned'


class TestTwoOperators:
    def test_a_second_release_creates_no_second_cancellation(
            self, geo, world, django_capture_on_commit_callbacks):
        trip = world['ghost']()
        set_driver_active_trip(world['driver'].id, trip.id)
        first, _ = operator('city_ops_manager', geo)
        second, _ = operator('city_ops_manager', geo)
        with django_capture_on_commit_callbacks(execute=True):
            assert post(first, trip, 'release-driver').status_code == 200
        with django_capture_on_commit_callbacks(execute=True):
            late = post(second, trip, 'release-driver')
        assert late.status_code == 409
        assert audit('trip_cancelled', trip).count() == 1
        assert audit('trip_stale_driver_released', trip).count() == 1
        assert audit('trip_stale_driver_released_refused', trip).count() == 1


# ---------------------------------------------------------------------------
# The detail payload
# ---------------------------------------------------------------------------

class TestDetailPayload:
    def detail(self, client, trip):
        response = client.get(f'{LIST}{trip.id}/')
        assert response.status_code == 200, response.content
        return response.json()['data']

    def test_a_ghost_trip_before_pickup_offers_both_actions(self, geo, world):
        trip = world['ghost']()
        client, _ = operator('city_ops_manager', geo)
        body = self.detail(client, trip)
        actions = body['stale_actions']
        assert actions['can_resolve'] and actions['mark_reviewed'] and actions['release_driver']
        assert actions['release_case'] == 'B' and actions['reason_codes'] == []
        assert body['stale_detail']['silent_seconds'] >= 1700
        assert body['stale_detail']['reviewed_at'] is None

    def test_a_started_trip_offers_review_and_the_release_with_its_reason_codes(
            self, geo, world):
        trip = world['ghost'](status='in_progress')
        client, _ = operator('city_ops_manager', geo)
        actions = self.detail(client, trip)['stale_actions']
        assert actions['mark_reviewed'] is True and actions['release_driver'] is True
        assert actions['release_case'] == 'C'
        assert [(c['code'], c['rule']) for c in actions['reason_codes']] == [
            ('driver_unresponsive', 'standard'), ('trip_abandoned', 'standard'),
            ('other', 'standard'), ('safety_incident', 'platform_funded'),
            ('platform_fault', 'platform_funded')]
        assert 'refusal' not in actions and 'routes' not in actions

    def test_a_trip_that_is_not_stale_offers_nothing(self, geo, world):
        trip = world['ghost'](stale_flagged_at=None, stale_reason='',
                              last_driver_activity_at=world['now'] - timedelta(seconds=10),
                              accepted_at=world['now'] - timedelta(minutes=2))
        client, _ = operator('city_ops_manager', geo)
        actions = self.detail(client, trip)['stale_actions']
        assert actions['is_stale'] is False
        assert actions['mark_reviewed'] is False and actions['release_driver'] is False

    def test_a_finished_trip_with_a_busy_driver_offers_the_repair(self, geo, world):
        trip = world['ghost'](status='completed', stale_flagged_at=None, stale_reason='',
                              completed_at=world['now'])
        set_driver_active_trip(world['driver'].id, trip.id)
        client, _ = operator('city_ops_manager', geo)
        actions = self.detail(client, trip)['stale_actions']
        assert actions['release_driver'] is True and actions['release_case'] == 'A'

    def test_a_role_without_the_capability_is_offered_nothing(self, geo, world):
        trip = world['ghost']()
        client, _ = operator('support_agent', geo)
        actions = self.detail(client, trip)['stale_actions']
        assert actions['can_resolve'] is False
        assert actions['mark_reviewed'] is False and actions['release_driver'] is False
        assert actions['reason_codes'] == []

    def test_the_new_blocks_carry_no_contact_detail_or_coordinates(self, geo, world):
        import json

        trip = world['ghost'](status='in_progress')
        client, _ = operator('city_ops_manager', geo)
        body = self.detail(client, trip)
        text = json.dumps({'a': body['stale_actions'], 'd': body['stale_detail']})
        for secret in (HYD_PHONE, HYD_PHONE[3:], '+919876511102', '9876511102', 'Lakshmi',
                       'Kiran', '17.4', '78.4', 'Banjara'):
            assert secret not in text, secret

    def test_a_reviewed_trip_shows_who_and_when(self, geo, world):
        trip = world['ghost']()
        client, user = operator('city_ops_manager', geo)
        assert post(client, trip, 'mark-reviewed').status_code == 200
        detail = self.detail(client, trip)
        assert detail['stale_detail']['reviewed_at']
        assert detail['stale_detail']['reviewed_by']['id'] == user.id
        assert detail['stale_actions']['mark_reviewed'] is False


# ---------------------------------------------------------------------------
# Structural: the routes go through the guard lists
# ---------------------------------------------------------------------------

class TestRoutes:
    def test_both_routes_are_gated_by_the_scoped_capability(self):
        from servers.admin_access.bridge import ScopedPermission
        from servers.ride.admin_stale import admin_stale_mark_reviewed, admin_stale_release_driver

        for view in (admin_stale_mark_reviewed, admin_stale_release_driver):
            gates = [p for p in view.cls.permission_classes if isinstance(p, ScopedPermission)]
            assert gates, view
            assert getattr(gates[0], 'permission', None) in (None, 'trip.stale_resolve')

    def test_the_capability_is_in_the_catalogue(self):
        from servers.admin_access.roles import ALL_PERMISSIONS, validate_templates

        assert 'trip.stale_resolve' in ALL_PERMISSIONS and validate_templates() == []
