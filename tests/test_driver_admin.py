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
    assert driver_data["user_details"]["phone_number"] == driver_user.phone_number

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
def test_delete_driver_admin(auth_client_admin, auth_client_driver):
    """Test deleting a driver."""
    client, admin_user = auth_client_admin
    driver_client, driver_user = auth_client_driver
    
    driver = Driver.objects.get(user_id=driver_user)
    driver_id = driver.id
    
    url = f"/api/v1/driver/admin/{driver_id}/delete/"
    response = client.delete(url)
    
    assert response.status_code == status.HTTP_200_OK
    
    # Verify deleted
    with pytest.raises(Driver.DoesNotExist):
        Driver.objects.get(id=driver_id)
