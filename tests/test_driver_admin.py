import pytest
from rest_framework import status
from servers.driver.models import Driver

@pytest.mark.django_db
def test_list_drivers_admin(auth_client_admin, auth_client_driver):
    """Test listing drivers by an admin."""
    client, admin_user = auth_client_admin
    driver_client, driver_user = auth_client_driver
    
    # Get driver instance
    driver = Driver.objects.get(user_id=driver_user)
    
    url = "/api/v1/driver/admin/"
    response = client.get(url)
    assert response.status_code == status.HTTP_200_OK
    
    data = response.json()
    assert data.get("status") == "success"
    # Basic pagination structure check
    assert "count" in data["data"]
    assert "results" in data["data"]
    results = data["data"]["results"]
    assert len(results) > 0

@pytest.mark.django_db
def test_list_drivers_admin_unauthorized(api_client, auth_client_driver):
    """Test standard users or unauthenticated users cannot list drivers."""
    driver_client, driver_user = auth_client_driver
    
    url = "/api/v1/driver/admin/"
    
    # Unauthenticated
    response = api_client.get(url)
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    
    # Authenticated but not admin
    response = driver_client.get(url)
    assert response.status_code == status.HTTP_403_FORBIDDEN

@pytest.mark.django_db
def test_list_drivers_admin_filters(auth_client_admin, auth_client_driver):
    """Test filtering drivers by approved status and activity status."""
    client, admin_user = auth_client_admin
    driver_client, driver_user = auth_client_driver
    
    # The driver from fixture shouldn't be approved by default
    driver = Driver.objects.get(user_id=driver_user)
    assert driver.approved is False
    
    url = "/api/v1/driver/admin/"
    
    # Test approved=false
    res_unapproved = client.get(url, {"approved": "false"})
    assert res_unapproved.status_code == status.HTTP_200_OK
    results_unapproved = res_unapproved.json()["data"]["results"]
    assert len(results_unapproved) > 0
    assert any(d["id"] == driver.id for d in results_unapproved)
    
    # Test approved=true
    res_approved = client.get(url, {"approved": "true"})
    assert res_approved.status_code == status.HTTP_200_OK
    results_approved = res_approved.json()["data"]["results"]
    assert not any(d["id"] == driver.id for d in results_approved)
    
    # Test filtering by status
    res_status = client.get(url, {"status": "off"})
    assert res_status.status_code == status.HTTP_200_OK
    results_status = res_status.json()["data"]["results"]
    assert len(results_status) > 0

@pytest.mark.django_db
def test_retrieve_driver_admin(auth_client_admin, auth_client_driver):
    """Test retrieving a single driver by ID."""
    client, admin_user = auth_client_admin
    driver_client, driver_user = auth_client_driver
    
    driver = Driver.objects.get(user_id=driver_user)
    
    url = f"/api/v1/driver/admin/{driver.id}/"
    response = client.get(url)
    
    assert response.status_code == status.HTTP_200_OK
    data = response.json()
    assert data["status"] == "success"
    
    driver_data = data["data"]
    assert driver_data["id"] == driver.id
    assert "user_details" in driver_data
    # Masked by default for every operator, the superuser included: the
    # number keeps its last four and nothing else (admin_access.masking).
    masked = driver_data["user_details"]["phone_number"]
    assert masked != driver_user.phone_number
    assert masked.endswith(driver_user.phone_number[-4:])
    assert driver_user.phone_number not in response.content.decode()

@pytest.mark.django_db
def test_retrieve_driver_admin_not_found(auth_client_admin):
    """Test retrieving a non-existent driver."""
    client, admin_user = auth_client_admin
    
    url = "/api/v1/driver/admin/99999/"
    response = client.get(url)
    
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["error"]["code"] == "NOT_FOUND"

@pytest.mark.django_db
def test_update_kyc_status_admin(auth_client_admin, auth_client_driver):
    """Approving a driver requires the documents the KYC gate insists on
    (license doc + expiry in the future + active vehicle with RC doc).

    We set the file-field values to placeholder paths directly rather
    than going through SimpleUploadedFile; the latter would upload to
    S3 via private_document_storage and is flaky in CI without moto."""
    from datetime import date, timedelta
    from servers.driver.models import Vehicle, VehicleType
    client, admin_user = auth_client_admin
    driver_client, driver_user = auth_client_driver

    driver = Driver.objects.get(user_id=driver_user)
    assert driver.approved is False
    driver_user.full_name = 'Ravi Kumar'  # the KYC gate requires a real name
    driver_user.save(update_fields=['full_name'])

    # Seed the documents the KYC gate requires (just the truthy values —
    # the serializer only checks presence, not content).
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    vehicle = Vehicle.objects.create(
        driver_id=driver,
        vehicle_type_id=vt,
        vehicle_number='KA01TEST1234',
    )
    vehicle.rc_doc = 'rc_docs/fake.pdf'
    vehicle.save(update_fields=['rc_doc'])
    driver.license_doc = 'license_docs/fake.pdf'
    driver.license_expiry = date.today() + timedelta(days=365)
    driver.active_vehicle = vehicle
    driver.save(update_fields=['license_doc', 'license_expiry', 'active_vehicle'])

    url = f"/api/v1/driver/admin/{driver.id}/update-kyc/"
    payload = {"approved": True, "status": "active"}
    response = client.patch(url, data=payload, format="json")

    assert response.status_code == status.HTTP_200_OK, response.content

    driver.refresh_from_db()
    assert driver.approved is True
    assert driver.status == "active"


@pytest.mark.django_db
def test_update_kyc_status_admin_rejects_without_documents(auth_client_admin, auth_client_driver):
    """KYC gate: approving a driver with no documents on file must fail.

    Regression guard for the Phase-0 KYC document gate. Without this
    serializer-level validation, an admin (or compromised admin session)
    could mark a driver approved with zero verification."""
    client, _ = auth_client_admin
    driver_client, driver_user = auth_client_driver
    driver = Driver.objects.get(user_id=driver_user)

    url = f"/api/v1/driver/admin/{driver.id}/update-kyc/"
    response = client.patch(url, data={"approved": True}, format="json")
    assert response.status_code == status.HTTP_400_BAD_REQUEST

@pytest.mark.django_db
def test_delete_driver_admin_raises_a_deactivation_request(auth_client_admin, auth_client_driver):
    """Delete no longer deletes: it asks a checker to deactivate (AUDIT s.10,
    "permanent driver deactivation: always a checker"). The row is kept."""
    from io import StringIO

    from django.core.management import call_command

    from conftest import seed_actor

    call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=StringIO())
    client, admin_user = auth_client_admin
    driver_client, driver_user = auth_client_driver

    driver = Driver.objects.get(user_id=driver_user)
    driver_id = driver.id

    url = f"/api/v1/driver/admin/{driver_id}/delete/"
    response = client.delete(url)

    assert response.status_code == status.HTTP_202_ACCEPTED, response.content
    assert response.json()['data']['approval_request']['action_key'] == 'driver.deactivate'
    assert Driver.objects.filter(id=driver_id).exists()


# ---------------------------------------------------------------------------
# The review screen has to show the documents it is reviewing
# ---------------------------------------------------------------------------
#
# The console rendered expiry DATES and no way to open the paperwork, so an
# operator approving a driver to carry paying passengers was looking at four
# em-dashes. Approval is gated on documents; a reviewer who cannot see them
# is rubber-stamping.

def _driver_with_docs(driver_user):
    """A driver carrying a document in every column the console reviews."""
    from servers.driver.models import Vehicle, VehicleType

    driver = Driver.objects.get(user_id=driver_user)
    driver.license_doc = 'license_docs/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg'
    driver.license_doc_back = 'license_docs_back/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.jpg'
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    vehicle = Vehicle.objects.create(
        driver_id=driver, vehicle_type_id=vt, vehicle_number='TS09KYC001',
        rc_doc='rc_docs/cccccccccccccccccccccccccccccccc.jpg',
        insurance_doc='insurance_docs/dddddddddddddddddddddddddddddddd.jpg',
    )
    driver.active_vehicle = vehicle
    driver.save()
    return driver


@pytest.mark.django_db
def test_admin_driver_detail_offers_a_link_for_every_document(
    auth_client_admin, auth_client_driver,
):
    """The payload must carry a slot for every document under review.

    Asserted on SHAPE rather than on a built URL: the test settings have no
    S3 region, so no signed URL can be generated here. Whether one can
    actually be built on a real deployment is what
    `manage.py check_storage` answers, against the real bucket.
    """
    client, _ = auth_client_admin
    _, driver_user = auth_client_driver
    driver = _driver_with_docs(driver_user)

    resp = client.get(f'/api/v1/driver/admin/{driver.id}/full/')

    assert resp.status_code == 200, resp.content.decode()
    data = resp.json()['data']
    assert 'license_doc_url' in data
    assert 'license_doc_back_url' in data
    vehicle = data['vehicles'][0]
    for key in ('rc_doc_url', 'insurance_doc_url', 'permit_doc_url',
                'fitness_doc_url', 'puc_doc_url', 'inspection_doc_url',
                'vehicle_pic_url'):
        assert key in vehicle, f'{key} missing — an operator cannot open it'


class TestTheDocumentLinkHelper:
    """`_document_url` decides whether an operator can open a document.

    It must never raise: one unbuildable link costing the whole driver page
    is a worse outcome than a missing link, and storage misconfiguration is
    not hypothetical here.
    """

    def test_an_empty_field_has_no_link(self):
        from servers.driver.admin_views import _document_url

        assert _document_url(None) is None
        assert _document_url('') is None

    def test_a_usable_field_returns_its_url(self):
        from servers.driver.admin_views import _document_url

        class Field:
            name = 'license_docs/x.jpg'
            url = 'https://bucket.example/license_docs/x.jpg?X-Amz-Signature=abc'

            def __bool__(self):
                return True

        assert _document_url(Field()).startswith('https://bucket.example/')

    def test_a_storage_failure_becomes_no_link_not_an_exception(self):
        from servers.driver.admin_views import _document_url

        class Broken:
            name = 'license_docs/x.jpg'

            def __bool__(self):
                return True

            @property
            def url(self):
                raise ValueError('Invalid endpoint')

        assert _document_url(Broken()) is None


@pytest.mark.django_db
def test_admin_detail_carries_the_same_checklist_the_driver_sees(
    auth_client_admin, auth_client_driver,
):
    """Operator and driver must not be looking at different lists."""
    client, _ = auth_client_admin
    _, driver_user = auth_client_driver
    driver = _driver_with_docs(driver_user)

    resp = client.get(f'/api/v1/driver/admin/{driver.id}/full/')

    compliance = resp.json()['data']['compliance']
    assert compliance['country']
    keys = {d['key'] for d in compliance['driver_documents']}
    assert 'license_front' in keys
    assert isinstance(compliance['missing'], list)


@pytest.mark.django_db
def test_a_missing_document_is_reported_missing_not_hidden(
    auth_client_admin, auth_client_driver,
):
    client, _ = auth_client_admin
    _, driver_user = auth_client_driver
    driver = _driver_with_docs(driver_user)
    driver.license_doc = ''
    driver.save(update_fields=['license_doc'])

    resp = client.get(f'/api/v1/driver/admin/{driver.id}/full/')

    data = resp.json()['data']
    assert data['license_doc_url'] is None
    assert 'license_front' in data['compliance']['missing']


# ---------------------------------------------------------------------------
# A rejection has to reach the driver
# ---------------------------------------------------------------------------
#
# The reason an operator typed went to the audit log and nowhere else:
# KYCApprovalSerializer only writes `approved` and `status`, so `doc_status`
# stayed 'pending' forever and `doc_rejection_reason` was never written. The
# driver saw "not approved" with no reason -- identical to a driver who has
# uploaded nothing -- while the operator believed they had explained.

@pytest.mark.django_db
def test_rejecting_a_driver_records_the_reason_for_them_to_read(
    auth_client_admin, auth_client_driver,
):
    client, _ = auth_client_admin
    _, driver_user = auth_client_driver
    driver = _driver_with_docs(driver_user)

    resp = client.patch(
        f'/api/v1/driver/admin/{driver.id}/update-kyc/',
        {'approved': False, 'reason': 'Licence photo is unreadable.'},
        format='json',
    )

    assert resp.status_code == 200, resp.content.decode()
    driver.refresh_from_db()
    assert driver.approved is False
    assert driver.doc_status == 'rejected'
    assert driver.doc_rejection_reason == 'Licence photo is unreadable.'
    assert driver.doc_status_updated_at is not None


@pytest.mark.django_db
def test_the_driver_can_then_read_that_reason(auth_client_admin, auth_client_driver):
    """End to end: operator rejects, driver sees why on their own screen."""
    client, _ = auth_client_admin
    driver_client, driver_user = auth_client_driver
    driver = _driver_with_docs(driver_user)

    client.patch(
        f'/api/v1/driver/admin/{driver.id}/update-kyc/',
        {'approved': False, 'reason': 'Insurance has expired.'},
        format='json',
    )

    seen = driver_client.get('/api/v1/driver/driver/onboarding/').json()['data']
    assert seen['doc_status'] == 'rejected'
    assert seen['doc_rejection_reason'] == 'Insurance has expired.'


@pytest.mark.django_db
def test_approving_clears_a_previous_rejection_reason(
    auth_client_admin, auth_client_driver,
):
    """A stale reason beside an approved account is worse than none."""
    from django.utils import timezone as tz

    client, _ = auth_client_admin
    _, driver_user = auth_client_driver
    driver_user.full_name = 'Ravi Kumar'  # the KYC gate requires a real name
    driver_user.save(update_fields=['full_name'])
    driver = _driver_with_docs(driver_user)
    driver.license_expiry = tz.localdate() + tz.timedelta(days=400)
    driver.doc_status = 'rejected'
    driver.doc_rejection_reason = 'Licence photo is unreadable.'
    driver.save()

    resp = client.patch(
        f'/api/v1/driver/admin/{driver.id}/update-kyc/',
        {'approved': True}, format='json',
    )

    assert resp.status_code == 200, resp.content.decode()
    driver.refresh_from_db()
    assert driver.approved is True
    assert driver.doc_status == 'approved'
    assert driver.doc_rejection_reason == ''
