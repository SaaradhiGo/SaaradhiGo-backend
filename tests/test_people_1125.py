"""25 Nov (owner), console capabilities, People: payout details with a second person, a vehicle added
by staff, KYC reviewers, and data requests by the references people see."""

from unittest import mock

import pytest

from notifications_support import geo, make_driver_user, make_rider_user  # noqa: F401
from servers.admin_audit.models import AdminAuditLog
from test_finance_actions import checker
from test_sos_support_zones_matrix import operator

pytestmark = pytest.mark.django_db
DRIVERS = '/api/v1/driver/admin'
BANK = {'method': 'bank', 'holder_name': 'Ravi Kumar', 'account_number': '123456789012',
        'account_number_confirm': '123456789012', 'ifsc': 'HDFC0001234',
        'reason': 'Driver called: old account closed'}


class TestPayoutDetails:
    def test_needs_a_second_person_and_never_stores_the_number_readably(self, geo):
        from servers.approvals.models import ApprovalRequest
        from servers.driver.models import DriverBankAccount

        driver = make_driver_user(geo).driver
        c, _ = operator('city_ops_manager', geo)
        r = c.post(f'{DRIVERS}/{driver.id}/payout-details/', BANK, format='json')
        assert r.status_code == 202, r.content
        row = ApprovalRequest.objects.get(pk=r.json()['data']['approval_request']['id'])
        assert row.status == 'requested' and '123456789012' not in str(row.payload)
        assert row.payload['account_masked'].endswith('9012')
        assert not DriverBankAccount.objects.filter(driver=driver).exists()

        approver, _ = checker()
        with mock.patch('servers.notifications.dispatcher.notify') as notify:
            ok = approver.post(f"/api/v1/approvals/admin/requests/{row.pk}/approve/", {'reason': 'Called back, confirmed'},
                               format='json')
        assert ok.status_code == 200, ok.content
        bank = DriverBankAccount.objects.get(driver=driver)
        assert bank.account_number == '123456789012' and bank.ifsc_code == 'HDFC0001234'
        driver.refresh_from_db()
        assert driver.payout_details_changed_at is not None  # payouts wait 24 hours
        assert notify.called  # the driver is told, always

    def test_refusals(self, geo):
        driver = make_driver_user(geo).driver
        c, _ = operator('city_ops_manager', geo)
        assert c.post(f'{DRIVERS}/{driver.id}/payout-details/', {**BANK, 'reason': 'short'}, format='json').status_code == 400
        assert c.post(f'{DRIVERS}/{driver.id}/payout-details/', {**BANK, 'ifsc': 'BAD'}, format='json').status_code == 400
        s, _ = operator('support_agent', geo)
        assert s.post(f'{DRIVERS}/{driver.id}/payout-details/', BANK, format='json').status_code == 403


class TestAddVehicle:
    def test_added_inactive_beside_the_current_one_and_audited(self, geo):
        from servers.driver.models import Vehicle, VehicleType

        VehicleType.objects.get_or_create(type='sedan')
        driver = make_driver_user(geo).driver
        c, _ = operator('city_ops_manager', geo)
        r = c.post(f'{DRIVERS}/{driver.id}/vehicles/add/', {'vehicle_type': 'sedan', 'vehicle_number': 'TS09EF4321',
                                                             'brand': 'Maruti', 'model': 'Dzire', 'note': 'Driver bought a new car'},
                   format='json')
        assert r.status_code == 201, r.content
        v = Vehicle.objects.get(pk=r.json()['data']['vehicle_id'])
        assert v.status == 'inactive' and v.driver_id_id == driver.id
        assert AdminAuditLog.objects.filter(action='driver_vehicle_added', target_id=str(driver.id)).exists()
        assert c.post(f'{DRIVERS}/{driver.id}/vehicles/add/', {'vehicle_type': 'sedan', 'vehicle_number': 'X',
                                                                'note': 'x'}, format='json').status_code == 400


class TestKycReviewers:
    def test_bulk_assign_to_me_and_filter(self, geo):
        d1 = make_driver_user(geo, approved=False).driver
        d2 = make_driver_user(geo, approved=False).driver
        c, me = operator('kyc_verifier', geo)
        r = c.post(f'{DRIVERS}/kyc-queue/assign/', {'driver_ids': [d1.id, d2.id], 'reviewer': 'me'}, format='json')
        assert r.status_code == 200, r.content
        assert r.json()['data']['assigned'] == 2
        d1.refresh_from_db()
        assert d1.kyc_reviewer_id == me.id
        assert AdminAuditLog.objects.filter(action='driver_kyc_reviewer_assigned').count() == 2
        back = c.post(f'{DRIVERS}/kyc-queue/assign/', {'driver_ids': [d1.id], 'reviewer': 'none'}, format='json')
        assert back.json()['data']['assigned'] == 1

    def test_a_reviewer_without_kyc_there_is_refused(self, geo):
        d1 = make_driver_user(geo, approved=False).driver
        c, _ = operator('kyc_verifier', geo)
        _, support = operator('support_agent', geo)
        r = c.post(f'{DRIVERS}/kyc-queue/assign/', {'driver_ids': [d1.id], 'reviewer': support.id}, format='json')
        assert r.status_code == 400 and r.json()['error']['code'] == 'REVIEWER_NOT_ALLOWED'


class TestDataRequestByReference:
    def test_a_rider_reference_finds_the_rider(self):
        from servers.auth_user.data_request_views import _subject
        from servers.rider.models import Rider

        assert _subject('R-SG-NOPE') is None
        rider = Rider.objects.filter(public_ref__isnull=False).first()
        if rider is not None:
            assert _subject(rider.public_ref) == rider.user_id

    def test_with_a_rider_made_here(self, geo):
        from servers.auth_user.data_request_views import _subject
        from servers.rider.models import Rider

        user = make_rider_user(geo)
        rider = Rider.objects.get(user_id=user)
        Rider.objects.filter(pk=rider.pk).update(public_ref='R-SG-TEST')
        assert _subject('r-sg-test') == user
