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
import hashlib
import hmac
import os
import re
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from django.conf import settings

from base import object_store
from django.core.exceptions import ValidationError

from base.media import (
    CONTENT_TYPE_EXTENSIONS,
    EXTENSION_CONTENT_TYPES,
    MAX_UPLOAD_SIZE,
    UPLOAD_KINDS,
)

_KEY_PATTERN = r"{prefix}/[0-9a-f]{{32}}\.(?P<ext>{exts})"
_MISSING_OBJECT_CODES = {"404", "NoSuchKey", "NotFound"}


def build_s3_client(kind=None):
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
    # An S3-compatible endpoint when one is configured, real AWS S3 otherwise.
    # Threaded through here because this function builds its own client for
    # presigning; the django-storages backends already read the same setting.
    endpoint_url = getattr(settings, "AWS_S3_ENDPOINT_URL", None) or None
    access_key = settings.AWS_ACCESS_KEY_ID
    secret_key = settings.AWS_SECRET_ACCESS_KEY
    # A private kind may live behind its own credentials; empty = the main ones.
    if _is_private(kind):
        endpoint_url = getattr(settings, "AWS_S3_PRIVATE_ENDPOINT_URL", None) or endpoint_url
        access_key = getattr(settings, "AWS_S3_PRIVATE_ACCESS_KEY_ID", "") or access_key
        secret_key = getattr(settings, "AWS_S3_PRIVATE_SECRET_ACCESS_KEY", "") or secret_key

    region = (
        settings.AWS_S3_REGION_NAME
        or settings.AWS_REGION
        or os.environ.get("AWS_DEFAULT_REGION")
        # R2 has no regions; its only valid scope is `auto`. Falling back to
        # it here means an R2 deployment does not have to set a region whose
        # only correct value is a constant.
        or object_store.default_region(endpoint_url)
    )
    if not region:
        raise ValidationError(
            "S3 region is not configured. Set AWS_S3_REGION "
            "(or AWS_REGION / AWS_DEFAULT_REGION) to the bucket's region."
        )

    # Addressing style, region and checksum behaviour all come from
    # base/object_store.py, which is also what the django-storages backends
    # read. Two clients that disagree on any of them produce a signature the
    # server rejects, on whichever path happens to be exercised first.
    addressing_style = (
        getattr(settings, "AWS_S3_ADDRESSING_STYLE", None)
        or object_store.default_addressing_style(endpoint_url)
    )
    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": addressing_style},
            **object_store.client_config_kwargs(endpoint_url),
        ),
    )


def _is_private(kind):
    return kind is not None and UPLOAD_KINDS.get(kind, {}).get("visibility") == "private"


def _bucket(kind=None):
    """The bucket `kind` lives in: private documents may have their own.

    `AWS_S3_PRIVATE_BUCKET_NAME` (AUDIT 27.78): avatars and vehicle photos are
    served from the bucket's PUBLIC hostname, and a bucket with public read
    serves every object in it -- KYC documents included -- to anyone holding a
    key. Unset, documents stay in the shared bucket, as before.
    """
    if _is_private(kind):
        private = getattr(settings, "AWS_S3_PRIVATE_BUCKET_NAME", "") or ""
        if private:
            return private
    if not settings.AWS_S3_BUCKET_NAME:
        raise ValidationError("S3 bucket is not configured.")
    return settings.AWS_S3_BUCKET_NAME


def _owner_tag(owner_id, prefix, nonce):
    """16 hex of an HMAC binding a key to the account that asked for it."""
    message = f"upload-key:{owner_id}:{prefix}:{nonce}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), message, hashlib.sha256).hexdigest()[:16]


def owned_key(kind, owner, extension):
    """`<prefix>/<16-hex nonce><16-hex owner tag>.<ext>` for `owner`.

    BOUND TO THE ACCOUNT THAT PRESIGNED IT (AUDIT 27.78, Low 5). A key used to
    be a bare uuid, and `verify_uploaded_key` accepted any existing key from
    anyone: a driver who learned another driver's key (from a log, a support
    screenshot, a shared device) could attach that driver's licence to their
    own profile. The tag is an HMAC under SECRET_KEY, so the binding needs no
    storage, survives restarts, and keeps the key the same 32-hex shape every
    client and every stored row already has.
    """
    spec = kind_spec(kind)
    nonce = uuid.uuid4().hex[:16]
    return f"{spec['prefix']}/{nonce}{_owner_tag(owner.pk, spec['prefix'], nonce)}.{extension}"


def key_belongs_to(key, kind, owner):
    spec = kind_spec(kind)
    stem = (key or "").rsplit("/", 1)[-1].split(".", 1)[0]
    if owner is None or not getattr(owner, "pk", None) or len(stem) != 32:
        return False
    return hmac.compare_digest(stem[16:], _owner_tag(owner.pk, spec["prefix"], stem[:16]))


def generate_presigned_put(kind, content_type, owner):
    """Mint {key, upload_url, expires_in} for one direct PUT of `kind` by `owner`.

    The content type is baked into the signature: the client MUST send this
    exact Content-Type header on the PUT or S3 rejects the request with 403,
    which keeps e.g. an executable from being stored under rc_docs/.

    THE SIZE IS NOT. A presigned PUT cannot carry a size range (only a
    presigned POST policy can), and switching to POST would break the shipped
    driver app and the website, which PUT with the headers returned here. So
    the 5 MB limit is enforced at `verify_uploaded_key`, which also deletes an
    oversized object rather than leaving it in the bucket; an object that is
    uploaded and never submitted is left for the bucket lifecycle rule (expire
    unreferenced uploads; an owner action on the bucket, AUDIT 27.78).
    """
    spec = kind_spec(kind)
    extension = CONTENT_TYPE_EXTENSIONS.get(content_type)
    if not extension:
        raise ValidationError(
            f"Unsupported content type '{content_type}' for '{kind}'. "
            f"Allowed: {sorted(spec['types'])}."
        )
    key = owned_key(kind, owner, extension)
    expires_in = settings.AWS_QUERYSTRING_EXPIRE
    url = build_s3_client(kind).generate_presigned_url(
        "put_object",
        Params={
            "Bucket": _bucket(kind),
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


def verify_uploaded_key(kind, key, owner):
    """Validate a client-supplied key against its kind, its owner and S3 state.

    Checks, in order:
      1. kind exists in UPLOAD_KINDS
      2. key shape matches `<prefix>/<32-hex>.<ext>` (blocks traversal and
         cross-kind reuse, e.g. pointing vehicle_pic at license_docs/)
      3. extension maps to a content type allowed for the kind
      4. the key was presigned for `owner` (`owned_key`); anybody else's key
         is refused as if it did not exist
      5. object exists in the bucket (HEAD)
      6. size within MAX_UPLOAD_SIZE (an oversized object is deleted) and
         stored content type allowed

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

    if not key_belongs_to(key, kind, owner):
        # The same words as a key that was never uploaded: a refusal that
        # said "this belongs to someone else" would confirm the key exists.
        raise ValidationError(
            f"No uploaded object found at '{key}'. Complete the direct "
            "PUT before submitting the form."
        )

    try:
        metadata = build_s3_client(kind).head_object(
            Bucket=_bucket(kind), Key=key
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
        # Removed, not just refused: the PUT could not be capped, so this is
        # where an oversized upload stops occupying the bucket.
        try:
            build_s3_client(kind).delete_object(Bucket=_bucket(kind), Key=key)
        except Exception:  # noqa: BLE001 - the refusal matters more than the cleanup
            pass
        raise ValidationError("File size exceeds the 5MB limit.")

    stored_type = (metadata.get("ContentType") or "").split(";", 1)[0]
    if stored_type and stored_type not in spec["types"]:
        raise ValidationError(
            f"Uploaded object has content type '{stored_type}', which is "
            f"not allowed for '{kind}'."
        )
    return key
