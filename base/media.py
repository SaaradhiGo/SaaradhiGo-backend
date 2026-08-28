import mimetypes
import os
import uuid

from django.core.exceptions import ValidationError
from django.utils.deconstruct import deconstructible


ALLOWED_IMAGE_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
}
ALLOWED_DOCUMENT_TYPES = ALLOWED_IMAGE_TYPES | {"application/pdf"}
EMPTY_FILE_VALUES = {"", "null", "None"}
MAX_UPLOAD_SIZE = 5 * 1024 * 1024

# Canonical extension for each content type; used both when minting keys on
# /uploads/presign/ and when validating that an existing key's extension
# matches its kind.
CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "application/pdf": "pdf",
}
EXTENSION_CONTENT_TYPES = {ext: ct for ct, ext in CONTENT_TYPE_EXTENSIONS.items()}


@deconstructible
class PrefixedUUIDPath:
    def __init__(self, prefix):
        self.prefix = prefix.rstrip("/")

    def __call__(self, instance, filename):
        _, extension = os.path.splitext(filename or "")
        return f"{self.prefix}/{uuid.uuid4().hex}{extension.lower()}"


def _guess_content_type(file_obj):
    content_type = getattr(file_obj, "content_type", None)
    if content_type:
        return content_type

    guessed_type, _ = mimetypes.guess_type(getattr(file_obj, "name", ""))
    return guessed_type or "application/octet-stream"


def validate_file_size(file_obj):
    size = getattr(file_obj, "size", None)
    if size is None:
        return

    if size > MAX_UPLOAD_SIZE:
        raise ValidationError("File size exceeds the 5MB limit.")


def validate_image_file(file_obj):
    content_type = _guess_content_type(file_obj)
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise ValidationError("Only JPG, PNG, WEBP, or GIF images are allowed.")


def validate_document_file(file_obj):
    content_type = _guess_content_type(file_obj)
    if content_type not in ALLOWED_DOCUMENT_TYPES:
        raise ValidationError("Only JPG, PNG, WEBP, GIF, or PDF files are allowed.")


# Registry of direct-to-S3 upload kinds (presigned PUT flow). Each kind maps
# to the storage prefix the resulting key must live under, the visibility of
# its target bucket layout (drives permissioning on /uploads/presign/), and
# the content types a presigned PUT / later HEAD verification may use.
#
#   public  -> PublicMediaStorage (plain URLs)
#   private -> PrivateDocumentStorage (signed GET URLs)
UPLOAD_KINDS = {
    "avatar": {
        "prefix": "avatars",
        "visibility": "public",
        "types": ALLOWED_IMAGE_TYPES,
        "file_validator": validate_image_file,
    },
    "vehicle_pic": {
        "prefix": "vehicle_pics",
        "visibility": "public",
        "types": ALLOWED_IMAGE_TYPES,
        "file_validator": validate_image_file,
    },
    "license_doc": {
        "prefix": "license_docs",
        "visibility": "private",
        "types": ALLOWED_DOCUMENT_TYPES,
        "file_validator": validate_document_file,
    },
    "license_doc_back": {
        "prefix": "license_docs_back",
        "visibility": "private",
        "types": ALLOWED_DOCUMENT_TYPES,
        "file_validator": validate_document_file,
    },
    "rc_doc": {
        "prefix": "rc_docs",
        "visibility": "private",
        "types": ALLOWED_DOCUMENT_TYPES,
        "file_validator": validate_document_file,
    },
}


def resolve_file_input(request, field_name, kind=None):
    """Resolve an upload field from a request into (provided, value, error).

    Three accepted inputs, in priority order:

    1. A real file in request.FILES  -> returned as-is (legacy multipart
       path; validators run later via full_clean / serializer).
    2. An empty sentinel ("", "null", "None") or explicit null -> value is
       None, meaning "clear the field".
    3. A non-empty string:
       - with `kind` set: treated as a key previously uploaded through the
         presigned-PUT flow and verified against S3 (existence, prefix,
         size, content type) before being returned. The string can be
         assigned straight to the model FileField.
       - without `kind`: legacy behaviour — rejected as an error.
    """
    if field_name in request.FILES:
        return True, request.FILES[field_name], None

    if field_name not in request.data:
        return False, None, None

    raw_value = request.data.get(field_name)
    if raw_value in EMPTY_FILE_VALUES or raw_value is None:
        return True, None, None

    if kind is not None:
        from base.s3 import verify_uploaded_key

        try:
            verify_uploaded_key(kind, str(raw_value).strip())
        except ValidationError as exc:
            return True, None, "; ".join(exc.messages)
        return True, str(raw_value).strip(), None

    return True, None, f"{field_name} must be uploaded as a file."
