"""Upload hardening (AUDIT 27.78, Low 5).

  * `/uploads/presign/` is rate-limited per account;
  * a presigned key is bound to the account that asked for it, and nobody
    else can submit it;
  * an oversized object is refused at verify AND removed from the bucket (a
    presigned PUT cannot carry a size limit);
  * the multipart path takes the stored extension from the sniffed content,
    never the client's filename, and refuses a declared type that does not
    match the bytes.
"""

import uuid

import pytest
from botocore.exceptions import ClientError
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from base.media import MAX_UPLOAD_SIZE, PrefixedUUIDPath, validate_document_file, validate_image_file
from integrations.storage import s3 as s3_module

pytestmark = pytest.mark.django_db

User = get_user_model()

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PDF = b"%PDF-1.4\n" + b"\x00" * 32


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.deleted = []

    def head_object(self, Bucket=None, Key=None):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        size, content_type = self.objects[Key]
        return {"ContentLength": size, "ContentType": content_type}

    def delete_object(self, Bucket=None, Key=None):
        self.deleted.append(Key)
        self.objects.pop(Key, None)

    def generate_presigned_url(self, ClientMethod=None, Params=None, ExpiresIn=None):
        return f"https://s3.test/{Params['Key']}?X-Amz-Signature=fake"


@pytest.fixture
def fake_s3(settings, monkeypatch):
    settings.AWS_S3_BUCKET_NAME = "test-bucket"
    client = FakeS3()
    monkeypatch.setattr("integrations.storage.s3.build_s3_client", lambda: client)
    return client


def _account(phone, role="rider"):
    user = User.objects.create_user(phone_number=phone, role=role)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {AccessToken.for_user(user)}")
    return user, client


class TestPresignRateLimit:
    def test_presign_is_capped_per_account(self, fake_s3, monkeypatch):
        from django.core.cache import cache

        from base.throttles import UploadPresignThrottle

        monkeypatch.setattr(UploadPresignThrottle, "rate", "2/hour", raising=False)
        cache.clear()
        _, client = _account("+919741000601")
        body = {"kind": "avatar", "content_type": "image/png"}
        codes = [client.post("/api/v1/uploads/presign/", body, format="json").status_code
                 for _ in range(3)]
        assert codes == [200, 200, 429]
        # Another account has its own budget.
        _, other = _account("+919741000602")
        assert other.post("/api/v1/uploads/presign/", body, format="json").status_code == 200
        cache.clear()


class TestKeysAreBoundToTheirOwner:
    def test_a_presigned_key_verifies_for_its_owner_only(self, fake_s3):
        owner, client = _account("+919741000611")
        intruder, _ = _account("+919741000612")
        r = client.post("/api/v1/uploads/presign/", {"kind": "avatar", "content_type": "image/png"},
                        format="json")
        assert r.status_code == 200, r.content.decode()
        key = r.json()["data"]["key"]
        fake_s3.objects[key] = (1024, "image/png")

        assert s3_module.verify_uploaded_key("avatar", key, owner) == key
        with pytest.raises(ValidationError):
            s3_module.verify_uploaded_key("avatar", key, intruder)
        with pytest.raises(ValidationError):
            s3_module.verify_uploaded_key("avatar", key, None)

    def test_a_bare_uuid_key_belongs_to_nobody(self, fake_s3):
        owner, _ = _account("+919741000613")
        key = f"avatars/{uuid.uuid4().hex}.png"
        fake_s3.objects[key] = (1024, "image/png")
        with pytest.raises(ValidationError):
            s3_module.verify_uploaded_key("avatar", key, owner)

    def test_another_accounts_key_cannot_be_set_as_my_avatar(self, fake_s3):
        victim, _ = _account("+919741000614")
        _, attacker = _account("+919741000615")
        key = s3_module.owned_key("avatar", victim, "png")
        fake_s3.objects[key] = (1024, "image/png")
        r = attacker.patch("/api/v1/auth/update/", {"avatar": key}, format="json")
        assert r.status_code == 400
        assert User.objects.get(phone_number="+919741000615").avatar.name in (None, "")


class TestOversizedObjects:
    def test_an_oversized_object_is_refused_and_removed(self, fake_s3):
        owner, _ = _account("+919741000621")
        key = s3_module.owned_key("avatar", owner, "png")
        fake_s3.objects[key] = (MAX_UPLOAD_SIZE + 1, "image/png")
        with pytest.raises(ValidationError):
            s3_module.verify_uploaded_key("avatar", key, owner)
        assert fake_s3.deleted == [key]


class TestMultipartUsesTheContent:
    def test_the_extension_comes_from_the_content_not_the_name(self):
        upload = SimpleUploadedFile("evil.html", PNG, content_type="image/png")
        validate_image_file(upload)
        stored = PrefixedUUIDPath("avatars")(None, upload.name)
        assert stored.endswith(".png")
        assert not stored.endswith(".html")

    def test_a_pdf_named_as_a_jpeg_is_stored_as_pdf(self):
        upload = SimpleUploadedFile("licence.jpg", PDF, content_type="application/pdf")
        validate_document_file(upload)
        assert PrefixedUUIDPath("license_docs")(None, upload.name).endswith(".pdf")

    def test_a_declared_type_that_does_not_match_the_content_is_refused(self):
        with pytest.raises(ValidationError):
            validate_document_file(SimpleUploadedFile("x.png", PDF, content_type="image/png"))

    def test_content_that_is_not_an_allowed_type_is_refused_whatever_it_claims(self):
        with pytest.raises(ValidationError):
            validate_image_file(SimpleUploadedFile("x.png", b"<html><script>", content_type="image/png"))

    def test_an_unknown_client_extension_never_reaches_the_key(self):
        assert PrefixedUUIDPath("avatars")(None, "x.svg").count(".") == 0
