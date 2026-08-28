import pytest
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.core.files.uploadedfile import SimpleUploadedFile

from servers.driver.models import Driver, Vehicle, VehicleType


User = get_user_model()


class TrackingStorage(Storage):
    def __init__(self, base_url, *, signed=False):
        self.base_url = base_url.rstrip("/") + "/"
        self.signed = signed
        self.files = {}
        self.deleted = []

    def _open(self, name, mode="rb"):
        return ContentFile(self.files[name], name=name)

    def _save(self, name, content):
        if hasattr(content, "seek"):
            content.seek(0)
        self.files[name] = content.read()
        return name

    def delete(self, name):
        self.deleted.append(name)
        self.files.pop(name, None)

    def exists(self, name):
        return name in self.files

    def url(self, name):
        suffix = "?signature=fake" if self.signed else ""
        return f"{self.base_url}{name}{suffix}"


@pytest.fixture(autouse=True)
def storage_fields(monkeypatch):
    public_storage = TrackingStorage("https://public-media.test", signed=True)
    private_storage = TrackingStorage("https://private-media.test", signed=True)

    storage_map = {
        User._meta.get_field("avatar"): public_storage,
        Driver._meta.get_field("license_doc"): private_storage,
        Driver._meta.get_field("license_doc_back"): private_storage,
        Vehicle._meta.get_field("rc_doc"): private_storage,
        Vehicle._meta.get_field("vehicle_pic"): public_storage,
    }

    for field, storage in storage_map.items():
        monkeypatch.setattr(field, "storage", storage, raising=False)

    return {"public": public_storage, "private": private_storage}


@pytest.mark.django_db
def test_update_user_avatar_upload_replaces_old_file(auth_client_rider, storage_fields):
    client, user = auth_client_rider
    user.avatar = "avatars/old.png"
    user.save(update_fields=["avatar"])
    storage_fields["public"].files["avatars/old.png"] = b"old-avatar"

    response = client.patch(
        "/api/v1/auth/update/",
        data={
            "full_name": "Avatar User",
            "avatar": SimpleUploadedFile("avatar.png", b"avatar-bytes", content_type="image/png"),
        },
        format="multipart",
    )

    assert response.status_code == 200
    user.refresh_from_db()
    assert user.full_name == "Avatar User"
    assert user.avatar.name.startswith("avatars/")
    assert user.avatar.name != "avatars/old.png"
    assert response.data["data"]["avatar"].startswith(f"https://public-media.test/{user.avatar.name}")
    assert response.data["data"]["avatar"].endswith("?signature=fake")
    assert "avatars/old.png" in storage_fields["public"].deleted


@pytest.mark.django_db
def test_create_vehicle_uploads_files_via_django_storage(auth_client_driver):
    client, user = auth_client_driver
    VehicleType.objects.create(type="Auto")

    response = client.post(
        "/api/v1/driver/vehicles/add/",
        data={
            "vehicle_number": "KA01AB1234",
            "vehicle_type": "Auto",
            "brand": "Bajaj",
            "rc_doc": SimpleUploadedFile("vehicle.pdf", b"pdf-bytes", content_type="application/pdf"),
            "vehicle_pic": SimpleUploadedFile("vehicle.png", b"img-bytes", content_type="image/png"),
        },
        format="multipart",
    )

    assert response.status_code == 201
    vehicle = Vehicle.objects.get(driver_id=user.driver)
    assert vehicle.rc_doc.name.startswith("rc_docs/")
    assert vehicle.vehicle_pic.name.startswith("vehicle_pics/")
    assert response.data["data"]["rc_doc"].startswith("https://private-media.test/rc_docs/")
    assert response.data["data"]["rc_doc"].endswith("?signature=fake")
    assert response.data["data"]["vehicle_pic"].startswith(f"https://public-media.test/{vehicle.vehicle_pic.name}")
    assert response.data["data"]["vehicle_pic"].endswith("?signature=fake")


@pytest.mark.django_db
def test_update_vehicle_replaces_old_files_and_cleans_them(auth_client_driver, storage_fields):
    client, user = auth_client_driver
    vehicle_type = VehicleType.objects.create(type="Sedan")
    vehicle = Vehicle.objects.create(
        driver_id=user.driver,
        vehicle_type_id=vehicle_type,
        vehicle_number="KA02CD5678",
        rc_doc="rc_docs/old.pdf",
        vehicle_pic="vehicle_pics/old.png",
    )
    storage_fields["private"].files["rc_docs/old.pdf"] = b"old-rc"
    storage_fields["public"].files["vehicle_pics/old.png"] = b"old-pic"

    response = client.patch(
        f"/api/v1/driver/vehicles/{vehicle.id}/",
        data={
            "brand": "Toyota",
            "rc_doc": SimpleUploadedFile("rc.pdf", b"pdf-bytes", content_type="application/pdf"),
            "vehicle_pic": SimpleUploadedFile("vehicle.png", b"img-bytes", content_type="image/png"),
        },
        format="multipart",
    )

    assert response.status_code == 200
    vehicle.refresh_from_db()
    assert vehicle.brand == "Toyota"
    assert vehicle.rc_doc.name.startswith("rc_docs/")
    assert vehicle.vehicle_pic.name.startswith("vehicle_pics/")
    assert "rc_docs/old.pdf" in storage_fields["private"].deleted
    assert "vehicle_pics/old.png" in storage_fields["public"].deleted


@pytest.mark.django_db
def test_update_driver_profile_uploads_private_doc_and_signed_url(auth_client_driver, storage_fields):
    client, user = auth_client_driver
    driver = user.driver
    driver.license_doc = "license_docs/old.png"
    driver.save(update_fields=["license_doc"])
    storage_fields["private"].files["license_docs/old.png"] = b"old-license"
    vehicle_type = VehicleType.objects.create(type="Mini")
    vehicle = Vehicle.objects.create(
        driver_id=driver,
        vehicle_type_id=vehicle_type,
        vehicle_number="KA03EF9012",
    )

    response = client.patch(
        "/api/v1/driver/driver/",
        data={
            "active_vehicle": str(vehicle.id),
            "license_expiry": "2030-01-01",
            "license_doc": SimpleUploadedFile("license.png", b"license-bytes", content_type="image/png"),
        },
        format="multipart",
    )

    assert response.status_code == 200
    driver.refresh_from_db()
    assert driver.active_vehicle_id == vehicle.id
    assert str(driver.license_expiry) == "2030-01-01"
    assert driver.license_doc.name.startswith("license_docs/")
    assert response.data["data"]["license_doc"].startswith("https://private-media.test/license_docs/")
    assert response.data["data"]["license_doc"].endswith("?signature=fake")
    assert "license_docs/old.png" in storage_fields["private"].deleted


@pytest.mark.django_db
def test_delete_vehicle_cleans_up_storage_files(auth_client_driver, storage_fields):
    client, user = auth_client_driver
    vehicle_type = VehicleType.objects.create(type="SUV")
    vehicle = Vehicle.objects.create(
        driver_id=user.driver,
        vehicle_type_id=vehicle_type,
        vehicle_number="KA04GH3456",
        rc_doc="rc_docs/delete.pdf",
        vehicle_pic="vehicle_pics/delete.png",
    )
    storage_fields["private"].files["rc_docs/delete.pdf"] = b"delete-rc"
    storage_fields["public"].files["vehicle_pics/delete.png"] = b"delete-pic"

    response = client.delete(f"/api/v1/driver/vehicles/{vehicle.id}/delete/")

    assert response.status_code == 200
    assert not Vehicle.objects.filter(id=vehicle.id).exists()
    assert "rc_docs/delete.pdf" in storage_fields["private"].deleted
    assert "vehicle_pics/delete.png" in storage_fields["public"].deleted


@pytest.mark.django_db
def test_update_driver_profile_can_clear_license_doc(auth_client_driver, storage_fields):
    client, user = auth_client_driver
    driver = user.driver
    driver.license_doc = "license_docs/existing.png"
    driver.save(update_fields=["license_doc"])
    storage_fields["private"].files["license_docs/existing.png"] = b"existing-license"

    response = client.patch(
        "/api/v1/driver/driver/",
        data={"license_doc": None},
        format="json",
    )

    assert response.status_code == 200
    driver.refresh_from_db()
    assert not driver.license_doc
    assert "license_docs/existing.png" in storage_fields["private"].deleted


@pytest.mark.django_db
def test_upload_endpoints_are_removed(auth_client_rider):
    client, _ = auth_client_rider

    response = client.post("/api/v1/upload/direct/", data={}, format="multipart")

    assert response.status_code == 404
