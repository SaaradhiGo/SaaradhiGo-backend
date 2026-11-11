import mimetypes
import os
import uuid

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile
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
        # The extension is never the client's: the validators below rename an
        # upload to `upload.<ext>` from its sniffed content before it is saved,
        # and anything else that reaches here is reduced to a known extension
        # or none (AUDIT 27.78, Low 5). A client-named `.html` or `.svg` used to
        # be stored -- and served from the public bucket -- under that name.
        _, extension = os.path.splitext(filename or "")
        extension = extension.lower().lstrip(".")
        extension = {"jpeg": "jpg"}.get(extension, extension)
        suffix = f".{extension}" if extension in EXTENSION_CONTENT_TYPES else ""
        return f"{self.prefix}/{uuid.uuid4().hex}{suffix}"


#: Leading bytes of each allowed type. The content decides the type, not the
#: name and not the declared Content-Type, both of which the client writes.
_SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
)

#: Declared types that say nothing, so they are not held against the content.
_UNDECLARED = {"", "application/octet-stream", "binary/octet-stream"}
_ALIASES = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg"}


def sniff_content_type(file_obj):
    """The allowed type the file's first bytes show, or None."""
    try:
        position = file_obj.tell()
    except Exception:  # noqa: BLE001 - not seekable: read from the start
        position = None
    try:
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)
        head = file_obj.read(16) or b""
    finally:
        if position is not None and hasattr(file_obj, "seek"):
            file_obj.seek(position)
    if isinstance(head, str):
        head = head.encode("latin-1", "ignore")
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    for signature, content_type in _SIGNATURES:
        if head.startswith(signature):
            return content_type
    return None


def _pending_upload(file_obj):
    """The bytes a client just sent, or None for an object already in storage.

    A FieldFile pointing at a stored key (the presigned path) has no bytes
    here: that key was checked against the bucket by `verify_uploaded_key`.
    """
    if isinstance(file_obj, UploadedFile):
        return file_obj
    if getattr(file_obj, "_committed", True) is False:
        inner = getattr(file_obj, "file", None)
        return inner if inner is not None else file_obj
    return None


def _validate_upload(file_obj, allowed, message):
    upload = _pending_upload(file_obj)
    if upload is None:
        if _guess_content_type(file_obj) not in allowed:
            raise ValidationError(message)
        return
    sniffed = sniff_content_type(upload)
    if sniffed not in allowed:
        raise ValidationError(message)
    declared = (getattr(upload, "content_type", None) or "").split(";", 1)[0].strip().lower()
    declared = _ALIASES.get(declared, declared)
    if declared not in _UNDECLARED and declared != sniffed:
        raise ValidationError(
            "The file's declared type does not match its content. Upload the original file."
        )
    # Stored under the extension its content has, whatever it was called.
    canonical = f"upload.{CONTENT_TYPE_EXTENSIONS[sniffed]}"
    upload.name = canonical
    if file_obj is not upload:
        file_obj.name = canonical


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
    _validate_upload(file_obj, ALLOWED_IMAGE_TYPES,
                     "Only JPG, PNG, WEBP, or GIF images are allowed.")


def validate_document_file(file_obj):
    _validate_upload(file_obj, ALLOWED_DOCUMENT_TYPES,
                     "Only JPG, PNG, WEBP, GIF, or PDF files are allowed.")


# Registry of direct-to-S3 upload kinds (presigned PUT flow). Each kind maps
# to the storage prefix the resulting key must live under, the visibility of
# its target bucket layout (drives permissioning on /uploads/presign/), and
# the content types a presigned PUT / later HEAD verification may use.
#
#   public  -> PublicMediaStorage (plain URLs)
#   private -> PrivateDocumentStorage (signed GET URLs)
UPLOAD_KINDS = {
    # 10 Nov: identity, address proof, profile photo and vehicle photos.
    "aadhaar_doc": {
        "prefix": "aadhaar_docs",
        "visibility": "private",
        "types": ALLOWED_DOCUMENT_TYPES,
        "file_validator": validate_document_file,
    },
    "pan_doc": {
        "prefix": "pan_docs",
        "visibility": "private",
        "types": ALLOWED_DOCUMENT_TYPES,
        "file_validator": validate_document_file,
    },
    "police_verification_doc": {
        "prefix": "police_verification_docs",
        "visibility": "private",
        "types": ALLOWED_DOCUMENT_TYPES,
        "file_validator": validate_document_file,
    },
    "address_proof_doc": {
        "prefix": "address_proof_docs",
        "visibility": "private",
        "types": ALLOWED_DOCUMENT_TYPES,
        "file_validator": validate_document_file,
    },
    "photo_pending": {
        "prefix": "driver_photos",
        "visibility": "public",
        "types": ALLOWED_IMAGE_TYPES,
        "file_validator": validate_image_file,
    },
    "photo_back": {
        "prefix": "vehicle_pics",
        "visibility": "public",
        "types": ALLOWED_IMAGE_TYPES,
        "file_validator": validate_image_file,
    },
    "photo_left": {
        "prefix": "vehicle_pics",
        "visibility": "public",
        "types": ALLOWED_IMAGE_TYPES,
        "file_validator": validate_image_file,
    },
    "photo_right": {
        "prefix": "vehicle_pics",
        "visibility": "public",
        "types": ALLOWED_IMAGE_TYPES,
        "file_validator": validate_image_file,
    },
    "photo_interior": {
        "prefix": "vehicle_pics",
        "visibility": "public",
        "types": ALLOWED_IMAGE_TYPES,
        "file_validator": validate_image_file,
    },
    # MVAG 2025 driver records (9 Nov): uploaded by the driver, reviewed in the console.
    "health_insurance_doc": {
        "prefix": "health_insurance_docs",
        "visibility": "private",
        "types": ALLOWED_DOCUMENT_TYPES,
        "file_validator": validate_document_file,
    },
    "term_insurance_doc": {
        "prefix": "term_insurance_docs",
        "visibility": "private",
        "types": ALLOWED_DOCUMENT_TYPES,
        "file_validator": validate_document_file,
    },
    "induction_training_certificate": {
        "prefix": "induction_certificates",
        "visibility": "private",
        "types": ALLOWED_DOCUMENT_TYPES,
        "file_validator": validate_document_file,
    },
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
        "permit_doc": {
        "prefix": "permit_docs",
        "visibility": "private",
        "types": ALLOWED_DOCUMENT_TYPES,
        "file_validator": validate_document_file,
    },
    "insurance_doc": {
        "prefix": "insurance_docs",
        "visibility": "private",
        "types": ALLOWED_DOCUMENT_TYPES,
        "file_validator": validate_document_file,
    },
    "fitness_doc": {
        "prefix": "fitness_docs",
        "visibility": "private",
        "types": ALLOWED_DOCUMENT_TYPES,
        "file_validator": validate_document_file,
    },
    "puc_doc": {
        "prefix": "puc_docs",
        "visibility": "private",
        "types": ALLOWED_DOCUMENT_TYPES,
        "file_validator": validate_document_file,
    },
    # US safety inspection certificate. Private, like every other statutory
    # document: a vehicle inspection report carries the plate and the owner.
    "inspection_doc": {
        "prefix": "inspection_docs",
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
        from integrations.storage.s3 import verify_uploaded_key

        try:
            # Only the account that presigned a key may submit it.
            verify_uploaded_key(kind, str(raw_value).strip(), getattr(request, 'user', None))
        except ValidationError as exc:
            return True, None, "; ".join(exc.messages)
        return True, str(raw_value).strip(), None

    return True, None, f"{field_name} must be uploaded as a file."
