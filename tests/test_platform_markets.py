"""Markets (module 15): the readiness checklist, launch and pause through a checker, the booking
gate, adding a place, and the backfill of existing cities.

AUDIT 27.36 product decision 1: launching, pausing or adding a country or city happens in
Platform Settings with maker-checker; a city cannot go live until its checklist is green.
"""

import importlib

import pytest
from django.apps import apps as django_apps
from django.contrib.auth import get_user_model

from base.service_area import validate_service_area_detail
from servers.admin_access import readiness
from servers.admin_access.models import GeoNode
from servers.admin_audit.models import AdminAuditLog
from servers.approvals.models import ApprovalRequest
from servers.payments.models import PayoutCountryConfig
from servers.platform_ops import markets
from servers.pricing.models import ServiceZone
from test_sos_support_zones_matrix import _zone, geo, operator  # noqa: F401 - fixtures
from platform_support import (
    BASE, approve, data, error_code, post, ready_up, two_super_admins,
)

pytestmark = pytest.mark.django_db

User = get_user_model()
WGL = 'IN-TG-WGL'


def items(node):
    return {i['key']: i for i in readiness.city_readiness(node)}


# ===========================================================================
# The checklist: each item ok or missing
# ===========================================================================

class TestReadinessItems:
    def test_a_fresh_draft_city_fails_every_blocking_item_and_names_where_to_fix_it(self, geo):
        got = items(geo['US-TX-AUS'])        # no zone, no cards, no payout rules, no gateway
        for key in ('zone', 'fare_card', 'commission', 'safety_coverage', 'payout_config',
                    'approval_policies', 'payment_gateway'):
            assert key in got
        assert got['zone']['status'] == 'missing' and got['zone']['blocking'] is True
        assert got['zone']['href'] == '/zones' and got['fare_card']['href'] == '/pricing'
        for item in got.values():
            assert set(item) >= {'key', 'ok', 'detail', 'status', 'blocking', 'label', 'href'}
            assert item['status'] in ('ok', 'missing', 'warning')

    def test_zone_fare_card_commission_and_safety_go_green_together(self, geo, settings):
        ready_up(geo, settings)
        got = items(geo[WGL])
        for key in ('zone', 'fare_card', 'commission', 'safety_coverage'):
            assert got[key]['status'] == 'ok', got[key]

    def test_payout_config_ok_and_missing(self, geo):
        assert items(geo[WGL])['payout_config']['status'] == 'missing'
        PayoutCountryConfig.objects.create(
            country='IN', fee_percent=2, minimum_withdrawal_minor=50000, is_active=True)
        assert items(geo[WGL])['payout_config']['status'] == 'ok'

    def test_approval_policies_ok_and_missing(self, geo, settings):
        from io import StringIO

        from django.core.management import call_command

        from conftest import seed_actor

        assert items(geo[WGL])['approval_policies']['status'] == 'missing'
        call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=StringIO())
        assert items(geo[WGL])['approval_policies']['status'] == 'ok'

    def test_payment_gateway_ok_for_inr_and_missing_for_usd_with_blocker_17(self, geo, settings):
        settings.CASHFREE_APP_ID = settings.CASHFREE_SECRET_KEY = ''
        assert items(geo[WGL])['payment_gateway']['status'] == 'missing'
        settings.CASHFREE_APP_ID, settings.CASHFREE_SECRET_KEY = 'a-test', 'b-test'
        assert items(geo[WGL])['payment_gateway']['status'] == 'ok'
        us = items(geo['US-TX-AUS'])['payment_gateway']
        assert us['status'] == 'missing' and '#17' in us['detail']

    def test_stubbed_sms_is_a_visible_warning_on_qa_and_a_blocker_when_live_is_required(
            self, geo, settings):
        settings.NOTIFY_LIVE_SEND = False
        item = items(geo[WGL])['sms_provider']
        assert item['status'] == 'warning' and item['blocking'] is False
        assert 'stubbed' in item['detail'] and 'QA' in item['detail']

        settings.PLATFORM_REQUIRE_LIVE_PROVIDERS = True
        item = items(geo[WGL])['sms_provider']
        assert item['status'] == 'missing' and item['blocking'] is True

        settings.NOTIFY_LIVE_SEND, settings.MSG91_AUTH_KEY = True, 'k-test'
        assert items(geo[WGL])['sms_provider']['status'] == 'ok'

    def test_tax_is_always_a_warning_that_cites_blocker_15_and_never_blocks(self, geo):
        item = items(geo[WGL])['tax']
        assert item['status'] == 'warning' and item['blocking'] is False
        assert '#15' in item['detail']

    def test_a_ready_city_has_no_blocking_item(self, geo, settings):
        ready_up(geo, settings)
        assert [i['key'] for i in readiness.city_readiness(geo[WGL]) if i['blocking']] == []


# ===========================================================================
# Launch through a checker
# ===========================================================================

class TestLaunch:
    def launch(self, client, code=WGL, reason='Warangal is ready'):
        return post(client, f'{BASE}/markets/{code}/status/', {'status': 'live', 'reason': reason})

    def test_a_launch_is_refused_with_the_failing_items_and_no_request_is_raised(self, geo):
        (maker, _), _ = two_super_admins(geo)
        before = ApprovalRequest.objects.count()

        response = self.launch(maker)

        assert response.status_code == 409 and error_code(response) == 'CITY_NOT_READY'
        assert ApprovalRequest.objects.count() == before
        assert GeoNode.objects.get(code=WGL).status == 'draft'

    def test_a_ready_launch_waits_for_a_checker_then_goes_live_and_is_audited(self, geo, settings):
        ready_up(geo, settings)
        (maker, maker_user), (checker, _) = two_super_admins(geo)

        response = self.launch(maker)
        assert response.status_code == 202, response.content
        body = data(response)
        assert body['state'] == 'pending_approval'
        rid = body['approval_request']['id']
        assert GeoNode.objects.get(code=WGL).status == 'draft'
        assert approve(maker, rid).status_code == 403          # not your own request

        done = approve(checker, rid)
        assert done.status_code == 200, done.content
        assert GeoNode.objects.get(code=WGL).status == 'active'
        row = AdminAuditLog.objects.filter(action='city_status_changed').latest('id')
        assert row.after['status_to'] == 'active'
        assert AdminAuditLog.objects.filter(action='approval_requested',
                                            target_id=str(rid)).exists()

    def test_readiness_is_rechecked_when_the_checker_approves(self, geo, settings):
        ready_up(geo, settings)
        (maker, _), (checker, _) = two_super_admins(geo)
        rid = data(self.launch(maker))['approval_request']['id']

        ServiceZone.objects.filter(code=WGL).update(is_active=False)    # it went red meanwhile
        approve(checker, rid)

        request = ApprovalRequest.objects.get(pk=rid)
        assert request.status == 'failed' and request.error_code == 'CITY_NOT_READY'
        assert GeoNode.objects.get(code=WGL).status == 'draft'

    def test_city_ops_cannot_launch_and_only_a_live_city_can_be_paused(self, geo, settings):
        client, _ = operator('city_ops_manager', geo, scope_code='IN-TG-HYD')
        assert self.launch(client, 'IN-TG-HYD').status_code == 403
        assert post(client, f'{BASE}/markets/IN-TG-HYD/status/',
                    {'status': 'paused', 'reason': 'x' * 8}).status_code in (202, 409)
        (maker, _), _ = two_super_admins(geo)
        wgl = post(maker, f'{BASE}/markets/{WGL}/status/', {'status': 'paused', 'reason': 'x' * 8})
        assert wgl.status_code == 409 and error_code(wgl) == 'NOT_LIVE'


# ===========================================================================
# Pause
# ===========================================================================

@pytest.fixture
def policies(db):
    from io import StringIO

    from django.core.management import call_command

    from conftest import seed_actor

    call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=StringIO())


@pytest.mark.usefixtures('policies')
class TestPause:
    HYD = 'IN-TG-HYD'
    # Inside the matrix polygon (78.30-78.50, 17.40-17.50).
    A, B = (17.45, 78.40), (17.46, 78.41)

    @pytest.fixture
    def hyd(self, geo):
        _zone(self.HYD, geo[self.HYD])
        return geo[self.HYD]

    def pause(self, client, code=HYD):
        return post(client, f'{BASE}/markets/{code}/status/',
                    {'status': 'paused', 'reason': 'Heavy flooding in the city'})

    def test_city_ops_asks_for_a_pause_of_their_own_city_and_a_checker_applies_it(
            self, geo, hyd):
        ops, _ = operator('city_ops_manager', geo, scope_code=self.HYD)
        _, (checker, _) = two_super_admins(geo)

        response = self.pause(ops)
        assert response.status_code == 202, response.content
        rid = data(response)['approval_request']['id']
        assert GeoNode.objects.get(code=self.HYD).status == 'active'

        assert approve(checker, rid).status_code == 200
        assert GeoNode.objects.get(code=self.HYD).status == 'paused'

    def test_city_ops_cannot_pause_another_city_it_is_not_found(self, geo, hyd):
        ops, _ = operator('city_ops_manager', geo, scope_code='US-TX-AUS')
        assert self.pause(ops).status_code == 404

    def test_a_paused_city_refuses_a_new_booking_with_a_stable_code_and_a_neutral_message(
            self, geo, hyd):
        assert validate_service_area_detail(*self.A, *self.B)[0] is True
        GeoNode.objects.filter(code=self.HYD).update(status='paused')

        ok, code, message = validate_service_area_detail(*self.A, *self.B)

        assert (ok, code) == (False, 'CITY_PAUSED')
        assert 'pause' not in message.lower() and 'operator' not in message.lower()

    def test_the_fare_estimate_endpoint_returns_the_same_code(self, geo, hyd):
        from rest_framework.test import APIClient

        GeoNode.objects.filter(code=self.HYD).update(status='paused')
        response = APIClient().post('/api/v1/ride/estimate-fare/', {
            'pickup_lat': 17.45, 'pickup_long': 78.40, 'destination_lat': 17.46,
            'destination_long': 78.41, 'distance_km': 3, 'duration_min': 9,
            'vehicle_type': 'sedan'}, format='json')
        assert response.status_code == 400 and error_code(response) == 'CITY_PAUSED'

    def test_a_draft_city_is_not_bookable_either(self, geo, hyd):
        GeoNode.objects.filter(code=self.HYD).update(status='draft')
        assert validate_service_area_detail(*self.A, *self.B)[1] == 'CITY_NOT_LIVE'

    def test_trips_already_requested_are_not_touched_by_a_pause(self, geo, hyd):
        from servers.ride.models import Trip, TripStatus

        rider = User.objects.create_user(phone_number='+919876500777', username='r777',
                                         role='rider')
        status_row, _ = TripStatus.objects.get_or_create(status_code='in_progress')
        trip = Trip.objects.create(
            user_id=rider, zone=ServiceZone.objects.get(code=self.HYD), pickup_lat=17.45,
            pickup_long=78.40, destination_lat=17.46, destination_long=78.41,
            pickup_address='a', destination_address='b', status_id=status_row)

        GeoNode.objects.filter(code=self.HYD).update(status='paused')

        trip.refresh_from_db()
        assert trip.status_id.status_code == 'in_progress'
        # The gate is a booking-start check; nothing that runs a trip goes through it.
        from servers.pricing.zone_workflow import in_progress_trips

        assert in_progress_trips(ServiceZone.objects.get(code=self.HYD)).count() == 1

    def test_the_command_centre_still_shows_the_paused_city(self, geo, hyd):
        GeoNode.objects.filter(code=self.HYD).update(status='paused')
        client, _ = operator('super_admin', geo)
        body = client.get('/api/v1/command-centre/admin/overview/').json()['data']
        cities = {c['code']: c for c in body['cities']}
        assert self.HYD in cities and cities[self.HYD]['status'] == 'paused'


# ===========================================================================
# Adding a place
# ===========================================================================

@pytest.mark.usefixtures('policies')
class TestAddPlace:
    def test_adding_a_city_is_a_request_and_arrives_as_a_draft(self, geo):
        (maker, _), (checker, _) = two_super_admins(geo)
        response = post(maker, f'{BASE}/markets/nodes/', {
            'kind': 'city', 'code': 'US-TX-DAL', 'name': 'Dallas', 'parent_code': 'US-TX',
            'reason': 'Second Texas city'})
        assert response.status_code == 202, response.content
        assert not GeoNode.objects.filter(code='US-TX-DAL').exists()

        approve(checker, data(response)['approval_request']['id'])

        node = GeoNode.objects.get(code='US-TX-DAL')
        assert node.status == 'draft' and node.parent.code == 'US-TX'

    @pytest.mark.parametrize('body,code', [
        ({'kind': 'country', 'code': 'ZZ', 'name': 'Zedland', 'reason': 'trying it'},
         'UNKNOWN_COUNTRY'),
        ({'kind': 'city', 'code': 'US-TX-AUS', 'name': 'Austin', 'parent_code': 'US-TX',
          'reason': 'duplicate'}, 'ALREADY_EXISTS'),
        ({'kind': 'city', 'code': 'US-TX-DAL', 'name': 'Dallas', 'parent_code': 'US',
          'reason': 'wrong parent'}, 'INVALID_PARENT'),
        ({'kind': 'city', 'code': 'IN-TG-DAL', 'name': 'Dallas', 'parent_code': 'US-TX',
          'reason': 'wrong prefix'}, 'INVALID_CODE'),
    ])
    def test_a_bad_place_is_refused(self, geo, body, code):
        (maker, _), _ = two_super_admins(geo)
        response = post(maker, f'{BASE}/markets/nodes/', body)
        assert response.status_code in (400, 409) and error_code(response) == code


# ===========================================================================
# Reading
# ===========================================================================

class TestRead:
    def test_the_overview_lists_countries_with_a_derived_status_and_readiness_counts(
            self, geo, settings):
        client, _ = operator('super_admin', geo)
        body = data(client.get(f'{BASE}/markets/'))
        countries = {c['code']: c for c in body['countries']}
        assert countries['IN']['status'] == 'live'                  # Hyderabad is live
        assert countries['US']['status'] == 'draft'                 # Austin is a draft
        cities = {c['code']: c for c in countries['IN']['cities']}
        assert cities['IN-TG-HYD']['status'] == 'live' and cities[WGL]['status'] == 'draft'
        assert cities[WGL]['blocking_count'] >= 1

    def test_city_ops_sees_only_their_own_city(self, geo):
        client, _ = operator('city_ops_manager', geo, scope_code='IN-TG-HYD')
        body = data(client.get(f'{BASE}/markets/'))
        codes = {c['code'] for country in body['countries'] for c in country['cities']}
        assert codes == {'IN-TG-HYD'}
        assert client.get(f'{BASE}/markets/{WGL}/readiness/').status_code == 404

    def test_a_country_is_live_when_one_city_is_live_paused_when_none_is_but_one_was(self, geo):
        assert markets.country_status('IN') == 'live'
        GeoNode.objects.filter(code='IN-TG-HYD').update(status='paused')
        assert markets.country_status('IN') == 'paused'
        GeoNode.objects.filter(kind='city', path__startswith='GLOBAL/IN/').update(status='draft')
        assert markets.country_status('IN') == 'draft'


# ===========================================================================
# The backfill
# ===========================================================================

class TestBackfillMigration:
    def run(self):
        module = importlib.import_module(
            'servers.admin_access.migrations.0002_city_status_draft')
        module.backfill(django_apps, None)

    def test_a_paused_city_with_an_active_zone_becomes_live_one_without_becomes_draft(self, geo):
        GeoNode.objects.filter(kind='city').update(status='paused')
        GeoNode.objects.filter(code='IN-TG-HYD').update(status='active')
        _zone('US-TX-AUS', geo['US-TX-AUS'])
        # The migrations seed a zone for every Indian city; this one is switched off.
        ServiceZone.objects.filter(code='IN-AP-VJA').update(is_active=False)

        self.run()

        status = dict(GeoNode.objects.filter(kind='city').values_list('code', 'status'))
        assert status['IN-TG-HYD'] == 'active'
        assert status['US-TX-AUS'] == 'active'          # was bookable, so it stays bookable
        assert status[WGL] == 'active'                  # it has an active zone: bookable today
        assert status['IN-AP-VJA'] == 'draft'           # no active zone: never could take a booking
        rows = AdminAuditLog.objects.filter(action='city_status_backfilled')
        assert {r.target_id for r in rows} >= {'US-TX-AUS', WGL, 'IN-AP-VJA'}

    def test_it_is_idempotent(self, geo):
        self.run()
        assert not AdminAuditLog.objects.filter(action='city_status_backfilled').exists()
        before = dict(GeoNode.objects.values_list('code', 'status'))
        self.run()
        assert dict(GeoNode.objects.values_list('code', 'status')) == before
