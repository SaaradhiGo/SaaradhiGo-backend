from django.core.exceptions import ValidationError
from django.utils.module_loading import import_string
from rest_framework import serializers

from base.media import EMPTY_FILE_VALUES, UPLOAD_KINDS


class NullableFileField(serializers.FileField):
    default_error_messages = {
        "invalid": "Submit a valid file.",
    }

    def to_internal_value(self, data):
        if data in EMPTY_FILE_VALUES or data is None:
            if self.allow_null:
                return None
            self.fail("null")
        return super().to_internal_value(data)


class S3UploadKeyField(serializers.Field):
    """Accepts either a legacy multipart file upload or an S3 object key.

    Keys are produced by POST /api/v1/uploads/presign/ and must already be
    uploaded; structural + existence verification happens in
    base.s3.verify_uploaded_key. File inputs keep the pre-existing behaviour
    (validators run later through model full_clean), so existing clients
    continue to work while they migrate.

    Representation mirrors DRF's FileField: storage-generated URL (signed
    for private kinds), made absolute against the request when available.
    """

    default_error_messages = {
        "invalid": "Submit a valid file or an uploaded S3 key.",
        "null": "This field may not be null.",
    }

    def __init__(self, *, kind, **kwargs):
        if kind not in UPLOAD_KINDS:
            raise ValueError(f"Unknown upload kind '{kind}'.")
        self.kind = kind
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        if data in EMPTY_FILE_VALUES or data is None:
            if self.allow_null:
                return None
            self.fail("null")

        # UploadedFile / FieldFile — legacy direct-upload path.
        if hasattr(data, "read"):
            from base.media import validate_file_size

            validator = UPLOAD_KINDS[self.kind]["file_validator"]
            try:
                validator(data)
                validate_file_size(data)
            except ValidationError as exc:
                raise serializers.ValidationError(exc.messages)
            return data

        if isinstance(data, str):
            from base.s3 import verify_uploaded_key

            try:
                verify_uploaded_key(self.kind, data.strip())
            except ValidationError as exc:
                raise serializers.ValidationError(exc.messages)
            return data.strip()

        self.fail("invalid")

    def to_representation(self, value):
        if value in (None, ""):
            return None

        name = getattr(value, "name", value)
        storage = getattr(value, "storage", None) or self._model_field_storage()
        url = storage.url(name)

        request = self.context.get("request") if self.context else None
        if request is not None:
            return request.build_absolute_uri(url)
        return url

    def _model_field_storage(self):
        """Storage callable/bound storage for the underlying model field."""
        model_field = self.parent.Meta.model._meta.get_field(self.source)
        storage = getattr(model_field, "storage", None)
        if isinstance(storage, str):
            storage = import_string(storage)()
        elif callable(storage):
            storage = storage()
        return storage
