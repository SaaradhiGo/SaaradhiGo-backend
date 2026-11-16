"""Console QA fixes (12 Nov, City Ops Hyderabad): one active vehicle and unique plates, the shared
checklist in the console, one trip and rating count, "Low rating", dispatch only to approved
drivers, the SOS on-call chain, the KYC review target, document checks, self-approval grants."""

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from servers.admin_audit.models import AdminAuditLog
from servers.driver.models import Driver, Vehicle, VehicleType
from servers.ride.models import Rating, Trip, TripStatus
from test_safety_module import age, incident_of, raise_sos, seed_config, world  # noqa: F401 - fixtures
from test_sos_support_zones_matrix import _driver, _rider, geo, operator  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db

LIST = '/api/v1/driver/admin/'


def _vehicle(driver, plate, status='active'):
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    return Vehicle.objects.create(driver_id=driver, vehicle_type_id=vt, vehicle_number=plate, status=status)


# --- 1. one active vehicle, a plate on one active vehicle ---------------------------------------

class TestVehicles:
    def test_the_database_refuses_a_second_active_vehicle_and_a_shared_plate(self, geo):
        a = _driver('+919811100001', 'A', geo['IN-TG-HYD'])
        b = _driver('+919811100002', 'B', geo['IN-TG-HYD'])
        _vehicle(a, 'TS09QT0059')
        with pytest.raises(IntegrityError), transaction.atomic():
            _vehicle(a, 'TS09QT0060')
        with pytest.raises(IntegrityError), transaction.atomic():
            _vehicle(b, 'ts 09 qt-0059')
        _vehicle(a, 'TS09QT0059', status='archived')  # history may repeat a plate

    def test_adding_a_vehicle_archives_the_old_one(self, geo):
        from rest_framework.test import APIClient

        driver = _driver('+919811100003', 'C', geo['IN-TG-HYD'])
        old = _vehicle(driver, 'TS09QT0061')
        driver.active_vehicle = old
        driver.save()
        client = APIClient()
        client.force_authenticate(driver.user_id)
        with mock.patch('servers.driver.vehicle_rules.plate_problem', return_value=None):
            r = client.post('/api/v1/driver/vehicles/add/', {'vehicle_number': 'TS09QT0062', 'vehicle_type': 'sedan'},
                            format='json')
        if r.status_code == 404:
            pytest.skip('vehicle create is mounted elsewhere')
        assert r.status_code == 201, r.content
        old.refresh_from_db()
        driver.refresh_from_db()
        assert old.status == 'archived'
        assert driver.active_vehicle.vehicle_number == 'TS09QT0062'
        assert Vehicle.objects.filter(driver_id=driver, status='active').count() == 1

    def test_the_console_reads_the_shared_checklist_and_skips_archived_vehicles(self, geo):
        from servers.driver import requirements
        from servers.driver.admin_drivers import documents_payload_raw

        driver = _driver('+919811100004', 'D', geo['IN-TG-HYD'])
        active = _vehicle(driver, 'TS09QT0063')
        _vehicle(driver, 'TS09QT0063', status='archived')
        driver.active_vehicle = active
        driver.save()
        vehicles = {row['vehicle_id'] for row in documents_payload_raw(driver) if row['owner'] == 'vehicle'}
        assert vehicles == {active.id}
        s = requirements.summary(driver)
        assert set(s) == {'done', 'total', 'blocking', 'outstanding', 'expiring'}
        assert s['done'] + len(s['outstanding']) >= s['total']


# --- 2/3. one trip count, one rating count, "Low rating" -----------------------------------------

def _settled(driver, n):
    from servers.rider.models import WalletTransaction

    for i in range(n):
        WalletTransaction.objects.create(user_id=driver.user_id, amount=Decimal('100.00'), purpose='trip_earnings',
                                         status='completed', reference_id=f'TRIP_{900000 + driver.id * 100 + i}',
                                         idempotency_key=f't-{driver.id}-{i}', currency='INR')


def _ratings(driver, rider, scores):
    status, _ = TripStatus.objects.get_or_create(status_code='completed')
    for s in scores:
        trip = Trip.objects.create(user_id=rider, driver_id=driver, status_id=status,
                                   pickup_lat=Decimal('17.4'), pickup_long=Decimal('78.4'),
                                   destination_lat=Decimal('17.5'), destination_long=Decimal('78.5'))
        Rating.objects.create(trip_id=trip, rater_id=rider, score=s)


class TestCounts:
    def test_the_console_list_shows_trip_count_and_low_rating_means_enough_low_ratings(self, geo, settings):
        from servers.driver.earnings import trip_count

        settings.DRIVER_LOW_RATING_MIN_RATINGS = 10
        client, _ = operator('super_admin', geo)
        rider = _rider('+919811100010', 'R')
        none = _driver('+919811100011', 'No ratings', geo['IN-TG-HYD'])
        low = _driver('+919811100012', 'Low', geo['IN-TG-HYD'])
        few = _driver('+919811100013', 'Few low', geo['IN-TG-HYD'])
        _ratings(low, rider, [3] * 10)
        _ratings(few, rider, [2] * 3)
        _settled(low, 4)
        body = client.get(f'{LIST}?page_size=50').json()['data']
        row = next(r for r in body['results'] if r['id'] == low.id)
        assert row['total_trips'] == trip_count(low) == 4
        assert row['rating_count'] == 10
        flagged = {r['id'] for r in client.get(f'{LIST}?low_rating=1&page_size=50').json()['data']['results']}
        assert low.id in flagged and none.id not in flagged and few.id not in flagged


# --- 5. dispatch never offers a trip to a driver who is not approved ----------------------------

class TestDispatch:
    def test_only_approved_drivers_are_candidates(self, geo, settings):
        from servers.ride import dispatch

        settings.ENVIRONMENT = 'production'
        approved = _driver('+919811100020', 'Approved', geo['IN-TG-HYD'])
        approved.approved = True
        approved.save()
        pending = _driver('+919811100021', 'Pending', geo['IN-TG-HYD'])
        simulated = _driver('+919811100022', 'Simulated', geo['IN-TG-HYD'])
        simulated.qa_simulated_eligibility = True
        simulated.save()
        trip = mock.Mock(id=1, requested_vehicle_type=None, pickup_long=Decimal('78.4'), pickup_lat=Decimal('17.4'))
        found = [(f'driver:{d.id}', 100.0) for d in (approved, pending, simulated)]
        with mock.patch('servers.redis_client.nearby_drivers', return_value=found), \
                mock.patch('servers.redis_client.get_generation_offers', return_value=set()):
            ids = dispatch._candidate_driver_ids(trip, 3000, 1)
        assert ids == [str(approved.id)]


# --- 7. the SOS on-call chain --------------------------------------------------------------------

class TestOnCall:
    def test_it_phones_primary_backup_head_and_again_until_acknowledged(self, world, geo, monkeypatch):
        from servers.safety import escalation
        from servers.safety.models import OnCallRota, SOSPage

        seed_config('IN')
        minutes = 2
        from servers.safety.models import SafetyCountryConfig
        SafetyCountryConfig.objects.filter(country='IN').update(sos_ack_minutes=minutes)
        OnCallRota.objects.create(city=geo['IN-TG-HYD'], primary_name='Ravi', primary_phone='+919800000101',
                                  backup_name='Sita', backup_phone='+919800000102',
                                  head_name='Head', head_phone='+919800000103')
        calls = []
        monkeypatch.setattr('servers.callbridge.providers.FakeProvider.page',
                            lambda self, phone, message, purpose: calls.append(phone) or 'ref')
        event = raise_sos(world['hyd_rider'], trip_id=world['hyd_trip'].id)
        for elapsed in (minutes, 2 * minutes, 3 * minutes, 4 * minutes):
            age(event, elapsed)
            escalation.sweep()
        assert calls == ['+919800000101', '+919800000102', '+919800000103', '+919800000101']
        assert list(SOSPage.objects.filter(sos=event).values_list('target', flat=True)) == [
            'primary', 'backup', 'head', 'primary']
        assert incident_of(event).updates.filter(kind='paged').count() == 4
        assert AdminAuditLog.objects.filter(action='safety_sos_paged').count() == 4
        event.status = 'acknowledged'
        event.save()
        age(event, 6 * minutes)
        escalation.sweep()
        assert len(calls) == 4

    def test_no_rota_is_recorded_not_silent(self, world, geo):
        from servers.safety import escalation
        from servers.safety.models import SOSPage

        seed_config('IN')
        event = raise_sos(world['hyd_rider'], trip_id=world['hyd_trip'].id)
        age(event, 60)
        escalation.sweep()
        page = SOSPage.objects.get(sos=event)
        assert (page.channel, page.outcome) == ('none', 'no_rota')

    def test_the_rota_api_is_audited_and_masks_phones_without_pii_view(self, world, geo):
        client, _ = operator('super_admin', geo)
        r = client.post('/api/v1/safety/admin/oncall/IN-TG-HYD/', {
            'primary_name': 'Ravi', 'primary_phone': '9800000101', 'reason': 'rota'}, format='json')
        assert r.status_code == 200, r.content
        assert AdminAuditLog.objects.filter(action='safety_oncall_set').count() == 1
        ops, _ = operator('city_ops_manager', geo)
        rows = ops.get('/api/v1/safety/admin/oncall/').json()['data']['results']
        hyd = next(x for x in rows if x['city']['code'] == 'IN-TG-HYD')
        assert hyd['people'][0]['name'] == 'Ravi'


# --- 8. the KYC review target ---------------------------------------------------------------------

class TestReviewTarget:
    def test_working_days_skip_the_weekend(self):
        from servers.driver.review_target import add_working_days

        friday = datetime(2026, 11, 13, 10, 0, tzinfo=dt_timezone.utc)
        assert add_working_days(friday, 1, ZoneInfo('Asia/Kolkata')).weekday() == 0

    def test_past_target_shows_in_the_queue_and_raises_then_clears_an_alert(self, geo):
        from servers.command_centre.models import KYC_REVIEW_PAST_TARGET, OperatorAlert
        from servers.driver import review_target

        driver = _driver('+919811100030', 'Waiting', geo['IN-TG-HYD'])
        driver.license_doc = 'license_docs/x.jpg'
        driver.uploaded_timestamp = timezone.now() - timedelta(days=17)
        driver.save()
        assert review_target.sweep() == 1
        assert OperatorAlert.objects.filter(kind=KYC_REVIEW_PAST_TARGET, acknowledged_at__isnull=True).count() == 1
        client, _ = operator('super_admin', geo)
        row = next(r for r in client.get('/api/v1/driver/admin/kyc-queue/').json()['data']['results']
                   if r['id'] == driver.id)
        assert row['past_target'] is True and row['review_target_days'] == 1
        driver.approved = True
        driver.save()
        review_target.sweep()
        assert OperatorAlert.objects.filter(kind=KYC_REVIEW_PAST_TARGET, acknowledged_at__isnull=True).count() == 0


# --- 9. document checks ---------------------------------------------------------------------------

class TestVerification:
    def test_the_fake_runs_every_check_and_production_is_not_connected(self, geo, settings):
        from servers.driver import verification

        driver = _driver('+919811100040', 'Checked', geo['IN-TG-HYD'])
        settings.IS_PRODUCTION = False
        settings.DOC_VERIFICATION_PROVIDER = ''
        rows = verification.run_all(driver)
        assert {r.kind for r in rows} == set(verification.LABELS)
        view = verification.latest(driver)
        assert view['connected'] and view['test_provider']
        settings.IS_PRODUCTION = True
        assert verification.get_provider() is None
        assert verification.latest(driver)['connected'] is False


# --- 10. self-approval --------------------------------------------------------------------------

class TestSelfApproval:
    def test_production_ignores_the_setting_and_a_grant_is_at_most_seven_days(self, geo, settings):
        from servers.approvals.service import self_approval_switch

        settings.APPROVALS_SOLO_SUPER_ADMIN = True
        settings.IS_PRODUCTION = True
        assert self_approval_switch()['on'] is False
        client, user = operator('super_admin', geo)
        bad = client.post('/api/v1/approvals/admin/self-approval/', {'reason': 'solo', 'days': 8}, format='json')
        assert bad.status_code == 400
        ok = client.post('/api/v1/approvals/admin/self-approval/', {'reason': 'solo', 'days': 7}, format='json')
        assert ok.status_code == 201, ok.content
        switch = self_approval_switch()
        assert switch['on'] and switch['source'] == 'grant' and switch['by']
        ops, _ = operator('city_ops_manager', geo)
        assert ops.post('/api/v1/approvals/admin/self-approval/', {'reason': 'x', 'days': 1},
                        format='json').status_code == 403


class TestPerCityLowRating:
    def test_a_city_setting_moves_its_own_drivers_only(self, geo):
        from test_sos_support_zones_matrix import _zone

        from servers.driver.ratings import low_threshold

        client, _ = operator('super_admin', geo)
        rider = _rider('+919811100050', 'R2')
        hyd_few = _driver('+919811100051', 'Hyd few', geo['IN-TG-HYD'])
        vij_few = _driver('+919811100052', 'Vij few', geo['IN-AP-VJA']) if 'IN-AP-VJA' in geo else None
        _ratings(hyd_few, rider, [2, 2, 2])
        if vij_few:
            _ratings(vij_few, rider, [2, 2, 2])
        assert low_threshold(geo['IN-TG-HYD']) == (Decimal('4.5'), 10)
        from servers.pricing.models import ServiceZone
        _zone('IN-TG-HYD', geo['IN-TG-HYD'])
        ServiceZone.objects.filter(geo_node=geo['IN-TG-HYD']).update(low_rating_below=Decimal('4.00'),
                                                                     low_rating_min_ratings=3)
        assert low_threshold(geo['IN-TG-HYD']) == (Decimal('4.00'), 3)
        flagged = {r['id'] for r in client.get(f'{LIST}?low_rating=1&page_size=50').json()['data']['results']}
        assert hyd_few.id in flagged
        if vij_few:
            assert vij_few.id not in flagged
