"""Direct-to-S3 upload helpers (presigned PUT flow).

The request path must never stream file bytes through Django/Daphne — that
is what produced the "took too long to shut down" kills under load. Clients
upload straight to S3 with a short-lived presigned URL minted here, then
submit the resulting key to the resource endpoints, which verify it with a
single HEAD call before saving.

Credentials/region/bucket come from the same env-driven settings the
django-storages file fields already use (AWS_ACCESS_KEY_ID,
AWS_SECRET_ACCESS_KEY, AWS_S3_REGION_NAME, AWS_S3_BUCKET_NAME), loaded from
.env.local at settings import — no duplicate configuration surface.
"""
import os
import re
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from django.conf import settings
from django.core.exceptions import ValidationError

from base.media import (
    CONTENT_TYPE_EXTENSIONS,
    EXTENSION_CONTENT_TYPES,
    MAX_UPLOAD_SIZE,
    UPLOAD_KINDS,
)

_KEY_PATTERN = r"{prefix}/[0-9a-f]{{32}}\.(?P<ext>{exts})"
_MISSING_OBJECT_CODES = {"404", "NoSuchKey", "NotFound"}


def build_s3_client():
    """A sigv4 S3 client for the configured bucket region.

    Kept as a module-level function (rather than a cached singleton) so
    tests can monkeypatch it and so a rotated key is picked up without a
    process restart.

    Region resolves AWS_S3_REGION_NAME -> AWS_REGION -> AWS_DEFAULT_REGION
    and fails fast if none is set. Virtual-hosted addressing is forced:
    recent botocore defaults generate_presigned_url to the global
    s3.amazonaws.com host (with a regional signature scope), which S3
    rejects with 403 SignatureDoesNotMatch on PUT — virtual style makes
    the presign host <bucket>.s3.<region>.amazonaws.com.
    """
    region = (
        settings.AWS_S3_REGION_NAME
        or settings.AWS_REGION
        or os.environ.get("AWS_DEFAULT_REGION")
    )
    if not region:
        raise ValidationError(
            "S3 region is not configured. Set AWS_S3_REGION "
            "(or AWS_REGION / AWS_DEFAULT_REGION) to the bucket's region."
        )
    addressing_style = getattr(settings, "AWS_S3_ADDRESSING_STYLE", None) or "virtual"
    return boto3.client(
        "s3",
        region_name=region,
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": addressing_style},
        ),
    )


def _bucket():
    if not settings.AWS_S3_BUCKET_NAME:
        raise ValidationError("S3 bucket is not configured.")
    return settings.AWS_S3_BUCKET_NAME


def generate_presigned_put(kind, content_type):
    """Mint {key, upload_url, expires_in} for one direct PUT of `kind`.

    The content type is baked into the signature: the client MUST send this
    exact Content-Type header on the PUT or S3 rejects the request with 403,
    which keeps e.g. an executable from being stored under rc_docs/.
    """
    spec = kind_spec(kind)
    extension = CONTENT_TYPE_EXTENSIONS.get(content_type)
    if not extension:
        raise ValidationError(
            f"Unsupported content type '{content_type}' for '{kind}'. "
            f"Allowed: {sorted(spec['types'])}."
        )
    key = f"{spec['prefix']}/{uuid.uuid4().hex}.{extension}"
    expires_in = settings.AWS_QUERYSTRING_EXPIRE
    url = build_s3_client().generate_presigned_url(
        "put_object",
        Params={
            "Bucket": _bucket(),
            "Key": key,
            "ContentType": content_type,
        },
        ExpiresIn=expires_in,
    )
    return {"key": key, "upload_url": url, "expires_in": expires_in}


def kind_spec(kind):
    try:
        return UPLOAD_KINDS[kind]
    except KeyError:
        raise ValidationError(
            f"Unknown upload kind '{kind}'. Allowed: {sorted(UPLOAD_KINDS)}."
        )


def verify_uploaded_key(kind, key):
    """Validate a client-supplied key against its kind and S3 state.

    Checks, in order:
      1. kind exists in UPLOAD_KINDS
      2. key shape matches `<prefix>/<32-hex>.<ext>` (blocks traversal and
         cross-kind reuse, e.g. pointing vehicle_pic at license_docs/)
      3. extension maps to a content type allowed for the kind
      4. object exists in the bucket (HEAD)
      5. size within MAX_UPLOAD_SIZE and stored content type allowed

    Raises ValidationError with user-safe messages; unexpected boto/network
    failures propagate as real errors (500) rather than masquerading as
    client mistakes.
    """
    spec = kind_spec(kind)
    allowed_extensions = sorted(
        ext for ext, ct in EXTENSION_CONTENT_TYPES.items() if ct in spec["types"]
    )
    pattern = _KEY_PATTERN.format(
        prefix=re.escape(spec["prefix"]),
        exts="|".join(allowed_extensions),
    )
    match = re.fullmatch(pattern, key or "")
    if not match:
        raise ValidationError(
            f"Invalid {kind} reference. Upload the file via "
            f"/api/v1/uploads/presign/ first; expected a "
            f"{spec['prefix']}/<id>.{allowed_extensions[0]} style key."
        )
    extension = match.group("ext")
    content_type = EXTENSION_CONTENT_TYPES.get(extension)
    if content_type not in spec["types"]:
        raise ValidationError(f"'{kind}' does not allow .{extension} files.")

    try:
        metadata = build_s3_client().head_object(
            Bucket=_bucket(), Key=key
        )
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in _MISSING_OBJECT_CODES:
            raise ValidationError(
                f"No uploaded object found at '{key}'. Complete the direct "
                "PUT before submitting the form."
            )
        raise

    size = metadata.get("ContentLength") or 0
    if size > MAX_UPLOAD_SIZE:
        raise ValidationError("File size exceeds the 5MB limit.")

    stored_type = (metadata.get("ContentType") or "").split(";", 1)[0]
    if stored_type and stored_type not in spec["types"]:
        raise ValidationError(
            f"Uploaded object has content type '{stored_type}', which is "
            f"not allowed for '{kind}'."
        )
    return key
