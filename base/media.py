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


def resolve_file_input(request, field_name):
    if field_name in request.FILES:
        return True, request.FILES[field_name], None

    if field_name not in request.data:
        return False, None, None

    raw_value = request.data.get(field_name)
    if raw_value in EMPTY_FILE_VALUES or raw_value is None:
        return True, None, None

    return True, None, f"{field_name} must be uploaded as a file."
