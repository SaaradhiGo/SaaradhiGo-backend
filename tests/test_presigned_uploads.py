"""Tests for the presigned direct-to-S3 upload flow (/uploads/presign/)
and for resource endpoints accepting already-uploaded S3 keys alongside
legacy multipart files.
"""
import uuid
from urllib.parse import urlparse

import pytest
from botocore.exceptions import ClientError
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from rest_framework.test import APIClient

from servers.driver.models import Driver, Vehicle, VehicleType


User = get_user_model()

PNG_KEY = lambda prefix="avatars": f"{prefix}/{uuid.uuid4().hex}.png"  # noqa: E731
PDF_KEY = lambda prefix="rc_docs": f"{prefix}/{uuid.uuid4().hex}.pdf"  # noqa: E731


class FakeS3Client:
    """Stands in for boto3 S3 client: tracks presigns, serves HEADs."""

    def __init__(self):
        self.objects = {}  # key -> (size, content_type)
        self.presigned = []

    def seed(self, key, content_type, size=1024):
        self.objects[key] = (size, content_type)

    def head_object(self, Bucket=None, Key=None):
        if Key not in self.objects:
            raise ClientError(
                {"Error": {"Code": "404", "Message": "Not Found"}},
                "HeadObject",
            )
        size, content_type = self.objects[Key]
        return {"ContentLength": size, "ContentType": content_type}

    def generate_presigned_url(self, ClientMethod=None, Params=None, ExpiresIn=None):
        Params = Params or {}
        self.presigned.append(
            {
                "method": ClientMethod,
                "bucket": Params.get("Bucket"),
                "key": Params.get("Key"),
                "content_type": Params.get("ContentType"),
                "expires_in": ExpiresIn,
            }
        )
        bucket = Params.get("Bucket", "bucket")
        return f"https://s3.test/{bucket}/{Params.get('Key')}?X-Amz-Signature=fake"


class TrackingStorage(Storage):
    """In-memory storage so no test ever touches a real bucket."""

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

    def size(self, name):
        return len(self.files.get(name, b""))

    def url(self, name):
        suffix = "?signature=fake" if self.signed else ""
        return f"{self.base_url}{name}{suffix}"


@pytest.fixture(autouse=True)
def storage_fields(settings, monkeypatch):
    settings.AWS_S3_BUCKET_NAME = "test-bucket"

    public_storage = TrackingStorage("https://public-media.test", signed=True)
    private_storage = TrackingStorage("https://private-media.test", signed=True)

    for field, storage in (
        (User._meta.get_field("avatar"), public_storage),
        (Driver._meta.get_field("license_doc"), private_storage),
        (Driver._meta.get_field("license_doc_back"), private_storage),
        (Vehicle._meta.get_field("rc_doc"), private_storage),
        (Vehicle._meta.get_field("vehicle_pic"), public_storage),
    ):
        monkeypatch.setattr(field, "storage", storage, raising=False)

    return {"public": public_storage, "private": private_storage}


@pytest.fixture
def fake_s3(monkeypatch):
    client = FakeS3Client()
    monkeypatch.setattr("base.s3.build_s3_client", lambda: client)
    return client


# ── POST /api/v1/uploads/presign/ ────────────────────────────────────────


@pytest.mark.django_db
def test_presign_rc_doc_returns_signed_put(auth_client_driver, fake_s3, settings):
    client, user = auth_client_driver

    response = client.post(
        "/api/v1/uploads/presign/",
        data={"kind": "rc_doc", "content_type": "image/png"},
        format="json",
    )

    assert response.status_code == 200
    body = response.data["data"]
    assert body["kind"] == "rc_doc"
    assert body["key"].startswith("rc_docs/")
    assert body["key"].endswith(".png")
    assert len(body["key"].split("/")[1].split(".")[0]) == 32
    assert body["method"] == "PUT"
    assert body["headers"] == {"Content-Type": "image/png"}
    assert body["expires_in"] == settings.AWS_QUERYSTRING_EXPIRE
    assert "X-Amz-Signature" in body["upload_url"]

    record = fake_s3.presigned[0]
    assert record["method"] == "put_object"
    assert record["bucket"] == "test-bucket"
    assert record["key"] == body["key"]
    # Content type is baked into the signature — the PUT must carry it.
    assert record["content_type"] == "image/png"


@pytest.mark.django_db
def test_presign_avatar_allowed_for_rider(auth_client_rider, fake_s3):
    client, user = auth_client_rider

    response = client.post(
        "/api/v1/uploads/presign/",
        data={"kind": "avatar", "content_type": "image/webp"},
        format="json",
    )

    assert response.status_code == 200
    assert response.data["data"]["key"].startswith("avatars/")


@pytest.mark.django_db
def test_presign_document_kinds_require_driver(auth_client_rider, fake_s3):
    client, user = auth_client_rider

    response = client.post(
        "/api/v1/uploads/presign/",
        data={"kind": "license_doc", "content_type": "application/pdf"},
        format="json",
    )

    assert response.status_code == 403


@pytest.mark.django_db
def test_presign_rejects_unknown_kind(auth_client_driver, fake_s3):
    client, user = auth_client_driver

    response = client.post(
        "/api/v1/uploads/presign/",
        data={"kind": "passport", "content_type": "image/png"},
        format="json",
    )

    assert response.status_code == 400


@pytest.mark.django_db
def test_presign_rejects_disallowed_content_type(auth_client_driver, fake_s3):
    client, user = auth_client_driver

    response = client.post(
        "/api/v1/uploads/presign/",
        data={"kind": "vehicle_pic", "content_type": "application/pdf"},
        format="json",
    )

    assert response.status_code == 400


# ── Presign host must target the bucket's region ─────────────────────────


@pytest.mark.django_db
def test_presign_url_targets_bucket_region_host(settings):
    """Regression: presigning must use the bucket's region (ap-south-2).

    A client built without an explicit region lets botocore fall back to
    its default, minting https://<bucket>.s3.us-east-1.amazonaws.com/...
    style URLs that S3 rejects with 403 SignatureDoesNotMatch on PUT.
    Presigning is a local signing operation, so this runs offline.
    """
    import base.s3 as s3_module

    settings.AWS_S3_BUCKET_NAME = "saaradhigo-s3-dev"
    settings.AWS_S3_REGION_NAME = "ap-south-2"
    settings.AWS_ACCESS_KEY_ID = "testing"
    settings.AWS_SECRET_ACCESS_KEY = "testing"
    settings.AWS_QUERYSTRING_EXPIRE = 900

    url = s3_module.generate_presigned_put("rc_doc", "application/pdf")["upload_url"]

    host = urlparse(url).netloc
    assert host == "saaradhigo-s3-dev.s3.ap-south-2.amazonaws.com"


@pytest.mark.django_db
def test_build_s3_client_fails_fast_without_region(settings, monkeypatch):
    import base.s3 as s3_module
    from django.core.exceptions import ValidationError

    settings.AWS_S3_REGION_NAME = None
    settings.AWS_REGION = None
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

    with pytest.raises(ValidationError):
        s3_module.build_s3_client()


# ── Resource endpoints accepting S3 keys ─────────────────────────────────


@pytest.mark.django_db
def test_create_vehicle_with_presigned_keys(auth_client_driver, fake_s3):
    client, user = auth_client_driver
    VehicleType.objects.create(type="Auto")

    rc_key, pic_key = PDF_KEY(), PNG_KEY("vehicle_pics")
    fake_s3.seed(rc_key, "application/pdf", size=2048)
    fake_s3.seed(pic_key, "image/png", size=2048)

    response = client.post(
        "/api/v1/driver/vehicles/add/",
        data={
            "vehicle_number": "KA01AB1234",
            "vehicle_type": "Auto",
            "brand": "Bajaj",
            "rc_doc": rc_key,
            "vehicle_pic": pic_key,
        },
        format="json",
    )

    assert response.status_code == 201, response.data
    vehicle = Vehicle.objects.get(driver_id=user.driver)
    assert vehicle.rc_doc.name == rc_key
    assert vehicle.vehicle_pic.name == pic_key
    # Private docs come back as signed URLs, public pics as plain ones.
    assert response.data["data"]["rc_doc"].endswith("?signature=fake")
    assert response.data["data"]["vehicle_pic"].endswith("?signature=fake")


@pytest.mark.django_db
def test_create_vehicle_rejects_key_never_uploaded(auth_client_driver, fake_s3):
    client, user = auth_client_driver
    VehicleType.objects.create(type="Auto")

    response = client.post(
        "/api/v1/driver/vehicles/add/",
        data={
            "vehicle_number": "KA01AB1234",
            "vehicle_type": "Auto",
            "rc_doc": PDF_KEY(),
        },
        format="json",
    )

    assert response.status_code == 400
    assert "rc_doc" in response.data["error"]["details"]["field"] or \
        "rc_doc" in str(response.data)


@pytest.mark.django_db
def test_create_vehicle_rejects_cross_kind_prefix(auth_client_driver, fake_s3):
    client, user = auth_client_driver
    VehicleType.objects.create(type="Auto")

    smuggled = PDF_KEY(prefix="license_docs")
    fake_s3.seed(smuggled, "application/pdf")

    response = client.post(
        "/api/v1/driver/vehicles/add/",
        data={
            "vehicle_number": "KA01AB1234",
            "vehicle_type": "Auto",
            "rc_doc": smuggled,
        },
        format="json",
    )

    assert response.status_code == 400


@pytest.mark.django_db
def test_create_vehicle_rejects_oversize_object(auth_client_driver, fake_s3):
    client, user = auth_client_driver
    VehicleType.objects.create(type="Auto")

    key = PDF_KEY()
    fake_s3.seed(key, "application/pdf", size=6 * 1024 * 1024)

    response = client.post(
        "/api/v1/driver/vehicles/add/",
        data={
            "vehicle_number": "KA01AB1234",
            "vehicle_type": "Auto",
            "rc_doc": key,
        },
        format="json",
    )

    assert response.status_code == 400


@pytest.mark.django_db
def test_create_vehicle_rejects_wrong_stored_content_type(auth_client_driver, fake_s3):
    client, user = auth_client_driver
    VehicleType.objects.create(type="Auto")

    key = PNG_KEY("vehicle_pics")
    fake_s3.seed(key, "video/mp4")

    response = client.post(
        "/api/v1/driver/vehicles/add/",
        data={
            "vehicle_number": "KA01AB1234",
            "vehicle_type": "Auto",
            "vehicle_pic": key,
        },
        format="json",
    )

    assert response.status_code == 400


@pytest.mark.django_db
def test_update_vehicle_accepts_keys_and_clearing(auth_client_driver, fake_s3):
    client, user = auth_client_driver
    vehicle_type = VehicleType.objects.create(type="Sedan")
    vehicle = Vehicle.objects.create(
        driver_id=user.driver,
        vehicle_type_id=vehicle_type,
        vehicle_number="KA02CD5678",
        rc_doc="rc_docs/00000000000000000000000000000000.pdf",
    )
    new_rc = PDF_KEY()
    fake_s3.seed(new_rc, "application/pdf")

    response = client.patch(
        f"/api/v1/driver/vehicles/{vehicle.id}/",
        data={"brand": "Toyota", "rc_doc": new_rc},
        format="json",
    )

    assert response.status_code == 200, response.data
    vehicle.refresh_from_db()
    assert vehicle.brand == "Toyota"
    assert vehicle.rc_doc.name == new_rc


@pytest.mark.django_db
def test_update_driver_profile_license_via_key(auth_client_driver, fake_s3):
    client, user = auth_client_driver
    driver = user.driver

    license_key = PDF_KEY(prefix="license_docs")
    fake_s3.seed(license_key, "application/pdf")

    response = client.patch(
        "/api/v1/driver/driver/update/",
        data={
            "license_expiry": "2030-01-01",
            "license_doc": license_key,
        },
        format="json",
    )

    assert response.status_code == 200, response.data
    driver.refresh_from_db()
    assert driver.license_doc.name == license_key
    assert str(driver.license_expiry) == "2030-01-01"


@pytest.mark.django_db
def test_avatar_update_via_key(auth_client_rider, fake_s3):
    client, user = auth_client_rider

    key = PNG_KEY()
    fake_s3.seed(key, "image/png")

    response = client.patch(
        "/api/v1/auth/update/",
        data={"avatar": key},
        format="json",
    )

    assert response.status_code == 200, response.data
    user.refresh_from_db()
    assert user.avatar.name == key


# ── Licence back side ────────────────────────────────────────────────────


@pytest.mark.django_db
def test_presign_license_doc_back(auth_client_driver, fake_s3, settings):
    client, user = auth_client_driver

    response = client.post(
        "/api/v1/uploads/presign/",
        data={"kind": "license_doc_back", "content_type": "image/jpeg"},
        format="json",
    )

    assert response.status_code == 200, response.data
    body = response.data["data"]
    assert body["kind"] == "license_doc_back"
    assert body["key"].startswith("license_docs_back/")
    assert body["expires_in"] == settings.AWS_QUERYSTRING_EXPIRE


@pytest.mark.django_db
def test_presign_license_doc_back_requires_driver(auth_client_rider, fake_s3):
    client, user = auth_client_rider

    response = client.post(
        "/api/v1/uploads/presign/",
        data={"kind": "license_doc_back", "content_type": "image/jpeg"},
        format="json",
    )

    assert response.status_code == 403


@pytest.mark.django_db
def test_profile_license_doc_back_via_key(auth_client_driver, fake_s3):
    client, user = auth_client_driver
    driver = user.driver

    front_key = PDF_KEY(prefix="license_docs")
    back_key = PDF_KEY(prefix="license_docs_back")
    fake_s3.seed(front_key, "application/pdf")
    fake_s3.seed(back_key, "application/pdf")

    response = client.patch(
        "/api/v1/driver/driver/update/",
        data={"license_doc": front_key, "license_doc_back": back_key},
        format="json",
    )

    assert response.status_code == 200, response.data
    driver.refresh_from_db()
    assert driver.license_doc.name == front_key
    assert driver.license_doc_back.name == back_key
    # Both licence sides are private docs -> short-lived signed URLs.
    assert response.data["data"]["license_doc"].endswith("?signature=fake")
    assert response.data["data"]["license_doc_back"].endswith("?signature=fake")
    assert "license_docs/" in response.data["data"]["license_doc"]
    assert "license_docs_back/" in response.data["data"]["license_doc_back"]

    profile = client.get("/api/v1/driver/driver/profile/")
    assert profile.status_code == 200
    assert profile.data["data"]["license_doc_back"].endswith("?signature=fake")


@pytest.mark.django_db
def test_license_front_rejects_back_side_key(auth_client_driver, fake_s3):
    client, user = auth_client_driver

    smuggled = PDF_KEY(prefix="license_docs_back")
    fake_s3.seed(smuggled, "application/pdf")

    response = client.patch(
        "/api/v1/driver/driver/update/",
        data={"license_doc": smuggled},
        format="json",
    )

    assert response.status_code == 400


@pytest.mark.django_db
def test_clearing_license_doc_back(auth_client_driver, fake_s3, storage_fields):
    client, user = auth_client_driver
    driver = user.driver
    driver.license_doc_back = "license_docs_back/0000000000000000000000000000ff.png"
    driver.save(update_fields=["license_doc_back"])
    storage_fields["private"].files[driver.license_doc_back.name] = b"old-back"

    response = client.patch(
        "/api/v1/driver/driver/update/",
        data={"license_doc_back": None},
        format="json",
    )

    assert response.status_code == 200, response.data
    driver.refresh_from_db()
    assert not driver.license_doc_back


# ── Legacy multipart uploads keep working ────────────────────────────────


@pytest.mark.django_db
def test_legacy_multipart_vehicle_creation_still_supported(
    auth_client_driver, fake_s3, storage_fields
):
    from django.core.files.uploadedfile import SimpleUploadedFile

    client, user = auth_client_driver
    VehicleType.objects.create(type="Auto")

    response = client.post(
        "/api/v1/driver/vehicles/add/",
        data={
            "vehicle_number": "KA05IJ6789",
            "vehicle_type": "Auto",
            "rc_doc": SimpleUploadedFile("rc.pdf", b"pdf-bytes", content_type="application/pdf"),
            "vehicle_pic": SimpleUploadedFile("pic.png", b"img-bytes", content_type="image/png"),
        },
        format="multipart",
    )

    assert response.status_code == 201, response.data
    vehicle = Vehicle.objects.get(driver_id=user.driver)
    assert vehicle.rc_doc.name.startswith("rc_docs/")
    assert vehicle.vehicle_pic.name.startswith("vehicle_pics/")
    assert response.data["data"]["rc_doc"].endswith("?signature=fake")


@pytest.mark.django_db
def test_clearing_fields_still_works(auth_client_driver, fake_s3, storage_fields):
    client, user = auth_client_driver
    driver = user.driver
    driver.license_doc = "license_docs/0000000000000000000000000000ff.png"
    driver.save(update_fields=["license_doc"])
    storage_fields["private"].files[driver.license_doc.name] = b"old-license"

    response = client.patch(
        "/api/v1/driver/driver/update/",
        data={"license_doc": None},
        format="json",
    )

    assert response.status_code == 200, response.data
    driver.refresh_from_db()
    assert not driver.license_doc
