"""Drivers & KYC (console module 3): list, KYC queue, detail and the writes.

WHAT THESE PROVE
----------------
  * the list is scoped (Hyderabad ops see Hyderabad, Super Admin both, a
    refused ?scope= is 403), and every filter, the sort and the pages work
    server-side; the joined range is each driver's own home-city day;
  * a KYC-only operator sees only drivers in KYC, an approved driver is 404
    to them, and the queue is oldest-waiting first with the checklist;
  * the detail carries per-document review state, blockers, ratings, counts,
    suspension, history, notes, audit trail and the caller's actions, and a
    strict viewer never sees a raw phone number or email;
  * each write: right capability (403), out of scope 404, audited,
    idempotent;
  * a suspended driver fails the dispatch gate (`may_operate`), a KYC
    approval cannot lift a suspension, reinstating restores approval only
    if the driver still passes the KYC gate;
  * bulk over 50 needs a checker; delete raises a deactivation request
    instead of deleting, and a checker's approval locks the account;
  * no migration is missing.
"""

from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from conftest import seed_actor
from servers.admin_audit.models import AdminAuditLog
from servers.approvals.models import EXECUTED, REQUESTED, ApprovalRequest
from servers.driver.models import (
    Driver, DriverDocumentReview, DriverNote, DriverSuspension, Vehicle, VehicleType,
)
from servers.driver.admin_drivers import _today
from servers.driver.qa_eligibility import may_operate
from servers.ride.models import Rating, Trip, TripFlag, TripStatus
from test_sos_support_zones_matrix import (  # noqa: F401 - fixtures
    _driver, _rider, _sos, _ticket, _zone, geo, operator,
)

pytestmark = pytest.mark.django_db

LIST = '/api/v1/driver/admin/'
QUEUE = '/api/v1/driver/admin/kyc-queue/'
BULK = '/api/v1/driver/admin/bulk/'
APPROVALS = '/api/v1/approvals/admin/requests/'

HYD_PHONE = '+919876522201'
AUS_PHONE = '+15125522201'
AUSTIN_POLYGON = {
    'type': 'Polygon',
    'coordinates': [[[-97.85, 30.15], [-97.60, 30.15], [-97.60, 30.40],
                     [-97.85, 30.40], [-97.85, 30.15]]],
}


def url(driver, suffix=''):
    return f'{LIST}{driver.id}/{suffix}'


def ids(response):
    assert response.status_code == 200, response.content
    return [row['id'] for row in response.json()['data']['results']]


def audit_rows(driver, action):
    return AdminAuditLog.objects.filter(target_type='driver', target_id=str(driver.id),
                                        action=action)


def _documents(driver, plate, vehicle_type, *, india=True):
    driver.license_doc = f'license_docs/{plate.lower()}front.jpg'
    driver.license_doc_back = f'license_docs_back/{plate.lower()}back.jpg'
    driver.license_expiry = _today() + timedelta(days=400)
    docs = {'rc_doc': f'rc_docs/{plate.lower()}.jpg',
            'insurance_doc': f'insurance_docs/{plate.lower()}.jpg'}
    if india:
        docs.update(permit_doc=f'permit_docs/{plate.lower()}.jpg',
                    fitness_doc=f'fitness_docs/{plate.lower()}.jpg',
                    puc_doc=f'puc_docs/{plate.lower()}.jpg')
    else:
        docs.update(inspection_doc=f'inspection_docs/{plate.lower()}.jpg')
    vehicle = Vehicle.objects.create(driver_id=driver, vehicle_type_id=vehicle_type,
                                     vehicle_number=plate, **docs)
    driver.active_vehicle = vehicle
    driver.save()
    return vehicle


def _joined(driver, when):
    type(driver.user_id).objects.filter(pk=driver.user_id_id).update(created_at=when)


@pytest.fixture
def policies(geo):
    call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=StringIO())


@pytest.fixture
def world(geo):
    hyd = _zone('IN-TG-HYD', geo['IN-TG-HYD'])
    aus = _zone('US-TX-AUS', geo['US-TX-AUS'], country='US', state_code='TX',
                timezone_name='America/Chicago', polygon_geojson=AUSTIN_POLYGON,
                currency='USD')
    sedan, _ = VehicleType.objects.get_or_create(type='sedan')
    auto, _ = VehicleType.objects.get_or_create(type='auto')
    city = geo['IN-TG-HYD']

    approved = _driver('+919876522202', 'Kiran Rao', city)
    _documents(approved, 'TS09AP0001', sedan)
    Driver.objects.filter(pk=approved.pk).update(
        approved=True, doc_status='approved', status='online', ratings=Decimal('4.80'),
        total_trips=120)

    review = _driver('+919876522203', 'Ravi Teja', city)
    review_vehicle = _documents(review, 'TS09RV0002', auto)
    Driver.objects.filter(pk=review.pk).update(
        uploaded_timestamp=timezone.now() - timedelta(hours=5), ratings=Decimal('3.20'))

    older = _driver('+919876522204', 'Suresh Babu', city)
    _documents(older, 'TS09OL0003', sedan)
    Driver.objects.filter(pk=older.pk).update(
        uploaded_timestamp=timezone.now() - timedelta(days=2), ratings=Decimal('4.10'))

    fresh = _driver('+919876522205', 'Anil Kumar', city)  # nothing uploaded yet

    rejected = _driver('+919876522206', 'Mahesh Varma', city)
    _documents(rejected, 'TS09RJ0004', sedan)
    Driver.objects.filter(pk=rejected.pk).update(doc_status='rejected',
                                                 doc_rejection_reason='blurred')

    expiring = _driver('+919876522207', 'Venkat Reddy', city)
    _documents(expiring, 'TS09EX0005', sedan)
    Driver.objects.filter(pk=expiring.pk).update(
        approved=True, doc_status='approved',
        license_expiry=_today() + timedelta(days=10))
    Vehicle.objects.filter(driver_id=expiring).update(
        insurance_expiry=_today() - timedelta(days=1))

    aus_driver = _driver(AUS_PHONE, 'Austin Driver', geo['US-TX-AUS'])
    _documents(aus_driver, 'TXAUS0001', sedan, india=False)
    Driver.objects.filter(pk=aus_driver.pk).update(
        uploaded_timestamp=timezone.now() - timedelta(days=1))

    rider = _rider(HYD_PHONE, 'Lakshmi Devi')
    status, _ = TripStatus.objects.get_or_create(status_code='completed')
    trip = Trip.objects.create(
        user_id=rider, driver_id=approved, status_id=status, zone=hyd,
        pickup_lat=Decimal('17.4'), pickup_long=Decimal('78.4'),
        destination_lat=Decimal('17.5'), destination_long=Decimal('78.5'),
        pickup_address='12 Road No 1, Banjara Hills, Hyderabad',
        estimated_fare_minor=10_000, currency='INR')
    Rating.objects.create(trip_id=trip, rater_id=rider, score=5)
    _sos(rider, trip)
    _ticket(rider, trip)
    TripFlag.objects.create(trip=trip, reason='dispute')

    drivers = {'approved': approved, 'review': review, 'older': older, 'fresh': fresh,
               'rejected': rejected, 'expiring': expiring, 'aus': aus_driver}
    for driver in drivers.values():
        driver.refresh_from_db()
    return {**drivers, 'review_vehicle': review_vehicle, 'hyd': hyd, 'aus_zone': aus,
            'trip': trip}


HYD = ('approved', 'review', 'older', 'fresh', 'rejected', 'expiring')


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

class TestList:
    def test_city_ops_sees_hyderabad_only_and_super_admin_sees_both(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        assert set(ids(client.get(LIST))) == {world[k].id for k in HYD}
        admin, _ = operator('super_admin', geo)
        assert world['aus'].id in ids(admin.get(LIST))
        assert ids(admin.get(LIST + '?scope=US-TX-AUS')) == [world['aus'].id]

    def test_a_scope_the_caller_does_not_hold_is_refused(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        response = client.get(LIST + '?scope=US-TX-AUS')
        assert response.status_code == 403
        assert response.json()['error']['code'] == 'SCOPE_NOT_HELD'

    @pytest.mark.parametrize('query, expected', [
        ('kyc_state=in_review', ('review', 'older')),
        ('kyc_state=pending', ('fresh',)),
        ('kyc_state=approved', ('approved', 'expiring')),
        ('kyc_state=rejected', ('rejected',)),
        ('kyc_state=expired', ('expiring',)),
        ('kyc_state=rejected,pending', ('rejected', 'fresh')),
        ('online=true', ('approved',)),
        ('vehicle_type=auto', ('review',)),
        ('expiring_within=30', ('expiring',)),
        ('rating_min=4&rating_max=4.5', ('older',)),
        ('flagged=true', ('approved',)),
        ('approved=true', ('approved', 'expiring')),
    ])
    def test_filters(self, geo, world, query, expected):
        client, _ = operator('city_ops_manager', geo)
        assert set(ids(client.get(f'{LIST}?{query}'))) == {world[k].id for k in expected}

    def test_suspended_filter(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        assert client.post(url(world['approved'], 'suspend/'),
                           {'reason_code': 'safety'}, format='json').status_code == 201
        assert ids(client.get(f'{LIST}?suspended=true')) == [world['approved'].id]
        assert world['approved'].id not in ids(client.get(f'{LIST}?suspended=false'))

    def test_joined_range_is_each_drivers_own_city_day(self, geo, world):
        # 2026-10-01 20:00 UTC is 02 Oct 01:30 in Hyderabad and 01 Oct 15:00 in Austin.
        moment = datetime(2026, 10, 1, 20, 0, tzinfo=dt_timezone.utc)
        _joined(world['review'], moment)
        _joined(world['aus'], moment)
        admin, _ = operator('super_admin', geo)
        assert set(ids(admin.get(f'{LIST}?joined_from=2026-10-02&joined_to=2026-10-02'))) == {
            world['review'].id}
        assert set(ids(admin.get(f'{LIST}?joined_from=2026-10-01&joined_to=2026-10-01'))) == {
            world['aus'].id}

    @pytest.mark.parametrize('query', ['kyc_state=lost', 'online=maybe', 'rating_min=x',
                                       'rating_min=6', 'expiring_within=-1', 'ordering=name',
                                       'joined_from=2026/10/01',
                                       'joined_from=2026-10-05&joined_to=2026-10-01'])
    def test_a_malformed_filter_is_400(self, geo, world, query):
        client, _ = operator('city_ops_manager', geo)
        assert client.get(f'{LIST}?{query}').status_code == 400

    def test_sort_and_pages(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        by_rating = ids(client.get(f'{LIST}?ordering=-rating'))
        assert by_rating[0] == world['approved'].id
        page = client.get(f'{LIST}?ordering=id&page_size=4&page=2').json()['data']
        assert page['count'] == 6 and page['total_pages'] == 2 and page['page'] == 2
        assert [r['id'] for r in page['results']] == sorted(world[k].id for k in HYD)[4:]

    def test_rows_carry_state_and_mask_contact_details(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        response = client.get(f'{LIST}?id={world["review"].id}')
        row = response.json()['data']['results'][0]
        assert row['kyc_state'] == 'in_review' and row['vehicle_type'] == 'auto'
        assert row['home_city']['code'] == 'IN-TG-HYD'
        assert row['home_city']['timezone'] == 'Asia/Kolkata'
        assert '+919876522203' not in response.content.decode()


# ---------------------------------------------------------------------------
# KYC-only operator and the queue
# ---------------------------------------------------------------------------

class TestKycOnly:
    def test_kyc_verifier_sees_only_drivers_in_kyc(self, geo, world):
        client, _ = operator('kyc_verifier', geo)
        assert set(ids(client.get(LIST))) == {
            world[k].id for k in ('review', 'older', 'fresh', 'rejected')}
        assert client.get(url(world['approved'], 'full/')).status_code == 404
        assert client.get(url(world['review'], 'full/')).status_code == 200

    def test_queue_is_oldest_waiting_first_with_the_checklist(self, geo, world):
        client, _ = operator('kyc_verifier', geo)
        response = client.get(QUEUE)
        assert ids(response) == [world['older'].id, world['review'].id]
        row = response.json()['data']['results'][0]
        assert row['waiting_seconds'] > 86_400
        assert {d['key'] for d in row['checklist']['vehicle_documents']} == {
            'rc', 'insurance', 'permit', 'fitness', 'puc'}
        assert all(d['review_state'] == 'not_reviewed' for d in row['documents'])
        assert not row['documents'][0]['url_withheld']  # the template holds pii.view
        body = response.content.decode()
        assert '+919876522203' not in body and '+919876522204' not in body

    def test_queue_is_scoped_and_excludes_suspended(self, geo, world):
        ops, _ = operator('city_ops_manager', geo)
        ops.post(url(world['older'], 'suspend/'), {'reason_code': 'fraud'}, format='json')
        client, _ = operator('kyc_verifier', geo)
        assert ids(client.get(QUEUE)) == [world['review'].id]
        assert client.get(url(world['older'], 'full/')).status_code == 404
        admin, _ = operator('super_admin', geo)
        assert ids(admin.get(QUEUE + '?scope=US-TX-AUS')) == [world['aus'].id]

    def test_without_pii_view_the_document_links_are_withheld(self, geo, world):
        client, _ = operator('analyst', geo)
        data = client.get(url(world['review'], 'full/')).json()['data']
        assert all(d['url'] is None for d in data['documents'])
        assert any(d['url_withheld'] for d in data['documents'])


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------

class TestDetail:
    def test_detail_sections(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        data = client.get(url(world['approved'], 'full/')).json()['data']
        assert data['kyc_state'] == 'approved'
        assert data['ratings_summary']['count'] == 1
        assert data['ratings_summary']['distribution']['5'] == 1
        assert data['counts'] == {'sos_total': 1, 'sos_open': 1, 'tickets_total': 1,
                                  'tickets_open': 1}
        assert data['recent_trips'][0]['id'] == world['trip'].id
        assert data['suspension'] == {'active': False}
        actions = data['actions']
        assert actions['suspend'] and not actions['reinstate'] and not actions['review_documents']
        assert len([d for d in data['documents'] if d['owner'] == 'vehicle']) == 5

    def test_no_raw_phone_or_email_for_any_viewer(self, geo, world):
        type(world['review'].user_id).objects.filter(pk=world['review'].user_id_id).update(
            email='ravi.teja@example.test')
        for role in ('city_ops_manager', 'kyc_verifier', 'super_admin'):
            client, _ = operator(role, geo)
            body = client.get(url(world['review'], 'full/')).content.decode()
            assert '+919876522203' not in body and 'ravi.teja@example.test' not in body
            body = client.get(LIST).content.decode()
            assert '+919876522203' not in body and 'ravi.teja@example.test' not in body

    def test_blockers_name_what_is_missing(self, geo, world):
        client, _ = operator('kyc_verifier', geo)
        data = client.get(url(world['fresh'], 'full/')).json()['data']
        codes = {b['code'] for b in data['approval_blockers']}
        assert 'DOCUMENTS_MISSING' in codes and data['actions']['approve_blocked']

    def test_austin_driver_is_404_to_hyderabad(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        assert client.get(url(world['aus'], 'full/')).status_code == 404


# ---------------------------------------------------------------------------
# Per-document review
# ---------------------------------------------------------------------------

class TestDocumentReview:
    def review(self, client, driver, key, decision, reason='', vehicle=None):
        body = {'document_key': key, 'decision': decision, 'reason': reason}
        if vehicle is not None:
            body['vehicle_id'] = vehicle.id
        return client.post(url(driver, 'documents/review/'), body, format='json')

    def test_reject_needs_a_reason_the_driver_can_read(self, geo, world):
        client, _ = operator('kyc_verifier', geo)
        driver = world['review']
        assert self.review(client, driver, 'puc', 'reject').status_code == 400
        response = self.review(client, driver, 'puc', 'reject', 'Certificate has expired')
        assert response.status_code == 201, response.content
        driver.refresh_from_db()
        assert driver.doc_status == 'rejected'
        assert driver.doc_rejection_reason == 'Pollution Under Control: Certificate has expired'
        assert audit_rows(driver, 'driver_document_reviewed').count() == 1
        # The driver app reads the reason off its own profile endpoint.
        assert DriverDocumentReview.objects.get(driver=driver).vehicle == world['review_vehicle']

    def test_same_decision_twice_is_one_review(self, geo, world):
        client, _ = operator('kyc_verifier', geo)
        driver = world['review']
        assert self.review(client, driver, 'license_front', 'approve').status_code == 201
        again = self.review(client, driver, 'license_front', 'approve')
        assert again.status_code == 200 and again.json()['data']['created'] is False
        assert DriverDocumentReview.objects.filter(driver=driver).count() == 1
        assert audit_rows(driver, 'driver_document_reviewed').count() == 1

    def test_approving_the_last_rejected_document_returns_the_driver_to_pending(self, geo, world):
        client, _ = operator('kyc_verifier', geo)
        driver = world['review']
        self.review(client, driver, 'insurance', 'reject', 'Wrong vehicle')
        self.review(client, driver, 'insurance', 'approve')
        driver.refresh_from_db()
        assert driver.doc_status == 'pending' and driver.doc_rejection_reason == ''

    def test_a_reupload_is_not_reviewed(self, geo, world):
        client, _ = operator('kyc_verifier', geo)
        driver = world['review']
        self.review(client, driver, 'license_front', 'reject', 'Blurred')
        Driver.objects.filter(pk=driver.pk).update(license_doc='license_docs/new.jpg')
        data = client.get(url(driver, 'full/')).json()['data']
        row = next(d for d in data['documents'] if d['key'] == 'license_front')
        assert row['review_state'] == 'not_reviewed' and row['review']['current'] is False

    def test_permission_scope_and_missing_file(self, geo, world):
        ops, _ = operator('city_ops_manager', geo)
        assert self.review(ops, world['review'], 'puc', 'approve').status_code == 403
        kyc, _ = operator('kyc_verifier', geo)
        assert self.review(kyc, world['aus'], 'insurance', 'approve').status_code == 404
        assert self.review(kyc, world['approved'], 'puc', 'approve').status_code == 404
        missing = self.review(kyc, world['fresh'], 'license_front', 'approve')
        assert missing.status_code == 409
        assert self.review(kyc, world['review'], 'passport', 'approve').status_code == 400


# ---------------------------------------------------------------------------
# Approve
# ---------------------------------------------------------------------------

class TestApprove:
    def test_kyc_verifier_approves_once(self, geo, world):
        client, _ = operator('kyc_verifier', geo)
        driver = world['review']
        response = client.post(url(driver, 'approve/'), {}, format='json')
        assert response.status_code == 200, response.content
        driver.refresh_from_db()
        assert driver.approved and driver.doc_status == 'approved' and may_operate(driver)
        assert audit_rows(driver, 'driver_approved').count() == 1
        ops, _ = operator('city_ops_manager', geo)
        again = ops.post(url(driver, 'approve/'), {}, format='json')
        assert again.status_code == 200 and again.json()['data']['changed'] is False
        assert audit_rows(driver, 'driver_approved').count() == 1

    def test_refused_with_a_rejected_or_missing_document(self, geo, world):
        client, _ = operator('kyc_verifier', geo)
        TestDocumentReview().review(client, world['review'], 'rc', 'reject', 'Not legible')
        refused = client.post(url(world['review'], 'approve/'), {}, format='json')
        assert refused.status_code == 400
        assert refused.json()['error']['code'] == 'DOCUMENTS_REJECTED'
        missing = client.post(url(world['fresh'], 'approve/'), {}, format='json')
        assert missing.status_code == 400
        assert not Driver.objects.get(pk=world['fresh'].pk).approved

    def test_who_may_approve(self, geo, world):
        support, _ = operator('support_agent', geo)
        assert support.post(url(world['review'], 'approve/'), {}, format='json').status_code == 403
        aus_kyc, _ = operator('kyc_verifier', geo, 'US-TX-AUS')
        assert aus_kyc.post(url(world['review'], 'approve/'), {},
                            format='json').status_code == 404


# ---------------------------------------------------------------------------
# Suspend / reinstate
# ---------------------------------------------------------------------------

class TestSuspension:
    def test_suspended_driver_fails_the_dispatch_gate(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        driver = world['approved']
        assert may_operate(driver)
        response = client.post(url(driver, 'suspend/'),
                               {'reason_code': 'safety', 'note': 'Rider complaint #12'},
                               format='json')
        assert response.status_code == 201, response.content
        driver.refresh_from_db()
        assert not may_operate(driver) and driver.status == 'blocked'
        assert audit_rows(driver, 'driver_suspended').count() == 1
        again = client.post(url(driver, 'suspend/'), {'reason_code': 'safety'}, format='json')
        assert again.status_code == 200 and again.json()['data']['created'] is False
        assert DriverSuspension.objects.filter(driver=driver).count() == 1
        assert audit_rows(driver, 'driver_suspended').count() == 1

    def test_kyc_approval_cannot_lift_a_suspension(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        driver = world['approved']
        client.post(url(driver, 'suspend/'), {'reason_code': 'fraud'}, format='json')
        admin, _ = operator('super_admin', geo)
        refused = admin.post(url(driver, 'approve/'), {}, format='json')
        assert refused.status_code == 409
        legacy = admin.patch(url(driver, 'update-kyc/'), {'approved': True}, format='json')
        assert legacy.status_code == 409
        driver.refresh_from_db()
        assert not driver.approved

    def test_reinstate_restores_approval_when_the_gate_still_passes(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        driver = world['approved']
        client.post(url(driver, 'suspend/'), {'reason_code': 'conduct'}, format='json')
        assert client.post(url(driver, 'reinstate/'), {}, format='json').status_code == 400
        response = client.post(url(driver, 'reinstate/'), {'note': 'Cleared'}, format='json')
        assert response.status_code == 200 and response.json()['data']['approved'] is True
        driver.refresh_from_db()
        assert may_operate(driver) and driver.status == 'off'
        again = client.post(url(driver, 'reinstate/'), {'note': 'Cleared'}, format='json')
        assert again.json()['data']['changed'] is False
        assert audit_rows(driver, 'driver_reinstated').count() == 1

    def test_reinstate_sends_an_expired_driver_back_to_review(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        driver = world['approved']
        client.post(url(driver, 'suspend/'), {'reason_code': 'documents'}, format='json')
        Driver.objects.filter(pk=driver.pk).update(
            license_expiry=_today() - timedelta(days=1))
        data = client.post(url(driver, 'reinstate/'), {'note': 'ok'}, format='json').json()['data']
        assert data['approved'] is False and data['back_to_review'] is True

    def test_permission_and_scope(self, geo, world):
        kyc, _ = operator('kyc_verifier', geo)
        assert kyc.post(url(world['review'], 'suspend/'), {'reason_code': 'safety'},
                        format='json').status_code == 403
        ops, _ = operator('city_ops_manager', geo)
        assert ops.post(url(world['aus'], 'suspend/'), {'reason_code': 'safety'},
                        format='json').status_code == 404
        assert ops.post(url(world['review'], 'suspend/'), {'reason_code': 'other'},
                        format='json').status_code == 400
        assert ops.post(url(world['review'], 'suspend/'), {'reason_code': 'deactivated'},
                        format='json').status_code == 400


# ---------------------------------------------------------------------------
# Note and vehicles
# ---------------------------------------------------------------------------

class TestNoteAndVehicles:
    def test_note_is_idempotent_by_key(self, geo, world):
        client, _ = operator('kyc_verifier', geo)
        body = {'body': 'Called the driver about the permit.', 'idempotency_key': 'n1'}
        assert client.post(url(world['review'], 'note/'), body, format='json').status_code == 201
        assert client.post(url(world['review'], 'note/'), body, format='json').status_code == 200
        assert DriverNote.objects.filter(driver=world['review']).count() == 1
        assert audit_rows(world['review'], 'driver_note_added').count() == 1
        support, _ = operator('support_agent', geo)
        assert support.post(url(world['review'], 'note/'), {'body': 'x'},
                            format='json').status_code == 403

    def test_deactivating_the_active_vehicle_clears_it(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        driver = world['approved']
        vehicle = driver.active_vehicle
        path = url(driver, f'vehicles/{vehicle.id}/status/')
        response = client.post(path, {'status': 'inactive', 'note': 'Fitness lapsed'},
                               format='json')
        assert response.status_code == 200, response.content
        driver.refresh_from_db()
        vehicle.refresh_from_db()
        assert vehicle.status == 'inactive' and driver.active_vehicle_id is None
        again = client.post(path, {'status': 'inactive'}, format='json')
        assert again.json()['data']['changed'] is False
        assert audit_rows(driver, 'driver_vehicle_deactivated').count() == 1
        client.post(path, {'status': 'active'}, format='json')
        driver.refresh_from_db()
        assert driver.active_vehicle_id == vehicle.id

    def test_vehicle_permission_and_ownership(self, geo, world):
        kyc, _ = operator('kyc_verifier', geo)
        vehicle = world['review'].active_vehicle
        assert kyc.post(url(world['review'], f'vehicles/{vehicle.id}/status/'),
                        {'status': 'inactive'}, format='json').status_code == 403
        ops, _ = operator('city_ops_manager', geo)
        other = world['older'].active_vehicle
        assert ops.post(url(world['review'], f'vehicles/{other.id}/status/'),
                        {'status': 'inactive'}, format='json').status_code == 404


# ---------------------------------------------------------------------------
# Bulk and deactivation (maker-checker)
# ---------------------------------------------------------------------------

class TestBulkAndDeactivation:
    def test_bulk_within_limit_runs_now(self, geo, world, policies):
        client, _ = operator('city_ops_manager', geo)
        response = client.post(BULK, {
            'action': 'suspend', 'reason_code': 'documents', 'idempotency_key': 'b1',
            'driver_ids': [world['review'].id, world['older'].id, world['aus'].id]},
            format='json')
        assert response.status_code == 200, response.content
        results = {r['id']: r['result'] for r in response.json()['data']['result']['results']}
        assert results == {world['review'].id: 'suspended', world['older'].id: 'suspended',
                           world['aus'].id: 'not_found'}

    def test_over_50_needs_a_checker(self, geo, world, policies):
        client, _ = operator('city_ops_manager', geo)
        many = [_driver(f'+9198765{30000 + i}', f'Bulk Driver {i}', geo['IN-TG-HYD']).id
                for i in range(51)]
        response = client.post(BULK, {'action': 'suspend', 'reason_code': 'documents',
                                      'driver_ids': many, 'idempotency_key': 'b2'},
                               format='json')
        assert response.status_code == 202, response.content
        data = response.json()['data']
        assert data['approval_request']['action_key'] == 'bulk.action' and data['count'] == 51
        assert not DriverSuspension.objects.exists()
        checker, _ = operator('super_admin', geo)
        rid = data['approval_request']['id']
        assert checker.post(f'{APPROVALS}{rid}/approve/', {'reason': 'ok'},
                            format='json').status_code == 200
        assert ApprovalRequest.objects.get(pk=rid).status == EXECUTED
        assert DriverSuspension.objects.filter(lifted_at__isnull=True).count() == 51

    def test_bulk_validation(self, geo, world, policies):
        client, _ = operator('city_ops_manager', geo)
        for body in ({'action': 'delete', 'driver_ids': [1], 'idempotency_key': 'x'},
                     {'action': 'suspend', 'driver_ids': [], 'reason_code': 'fraud',
                      'idempotency_key': 'x'},
                     {'action': 'suspend', 'driver_ids': [1], 'reason_code': 'fraud'}):
            assert client.post(BULK, body, format='json').status_code == 400

    def test_delete_raises_a_deactivation_request_and_deletes_nothing(self, geo, world, policies):
        maker, _ = operator('super_admin', geo)
        driver = world['review']
        response = maker.delete(url(driver, 'delete/'))
        assert response.status_code == 202, response.content
        rid = response.json()['data']['approval_request']['id']
        assert ApprovalRequest.objects.get(pk=rid).status == REQUESTED
        assert Driver.objects.filter(pk=driver.pk).exists()
        again = maker.delete(url(driver, 'delete/'))
        assert again.json()['data']['duplicate'] is True
        checker, _ = operator('super_admin', geo)
        assert checker.post(f'{APPROVALS}{rid}/approve/', {'reason': 'ok'},
                            format='json').status_code == 200
        driver.refresh_from_db()
        driver.user_id.refresh_from_db()
        assert not driver.user_id.is_active and not may_operate(driver)
        assert audit_rows(driver, 'driver_deactivated').count() == 1
        assert maker.post(url(driver, 'reinstate/'), {'note': 'x'},
                          format='json').status_code == 409

    def test_delete_needs_driver_deactivate(self, geo, world, policies):
        ops, _ = operator('city_ops_manager', geo)
        assert ops.delete(url(world['review'], 'delete/')).status_code == 403


def test_no_migration_is_missing():
    out = StringIO()
    call_command('makemigrations', '--check', '--dry-run', stdout=out, stderr=out)
