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
    integrations.storage.s3.verify_uploaded_key. File inputs keep the pre-existing behaviour
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
            from integrations.storage.s3 import verify_uploaded_key

            # The account the key must belong to (AUDIT 27.78, Low 5): the
            # view passes `upload_owner`, or the request it was given. With
            # neither there is nobody to check against, and it is refused.
            context = self.context or {}
            owner = context.get("upload_owner") or getattr(context.get("request"), "user", None)
            try:
                verify_uploaded_key(self.kind, data.strip(), owner)
            except ValidationError as exc:
                raise serializers.ValidationError(exc.messages)
            return data.strip()

        self.fail("invalid")

    def to_representation(self, value):
        if value in (None, ""):
            return None
        if UPLOAD_KINDS[self.kind]["visibility"] == "private":
            # A KYC document is never rendered as a bucket URL (AUDIT 27.78).
            return private_document_representation(self, value, self.source)

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


def _in_a_list(field):
    from rest_framework.serializers import ListSerializer

    parent = getattr(field, "parent", None)
    return isinstance(getattr(parent, "parent", None), ListSerializer)


def private_document_representation(field, value, column):
    """What a response says about a private KYC document (AUDIT 27.78).

    Never a bucket URL. The driver's OWN detail responses (the view sets
    `document_links` in the context) get a five-minute view link that audits
    the open; lists and every other caller get None, which is how "nothing to
    show here" has always read.
    """
    if value in (None, "") or not getattr(value, "name", value):
        return None
    context = field.context or {}
    if not context.get("document_links") or _in_a_list(field):
        return None
    instance = getattr(value, "instance", None)
    from servers.driver.document_access import view_link
    from servers.driver.models import Driver

    if isinstance(instance, Driver):
        return view_link(context.get("request"), instance, column)
    driver = getattr(instance, "driver_id", None)
    if driver is None:
        return None
    return view_link(context.get("request"), driver, column, vehicle=instance)


class DocumentLinkField(serializers.Field):
    """Read-only: a private document as `private_document_representation` gives it."""

    def __init__(self, **kwargs):
        kwargs["read_only"] = True
        super().__init__(**kwargs)

    def get_attribute(self, instance):
        return getattr(instance, self.source, None)

    def to_representation(self, value):
        return private_document_representation(self, value, self.source)


class DocumentOnFileField(serializers.Field):
    """Read-only: True when a document is on file, else None. For operator
    responses, which open documents through `admin_document_view` (AUDIT
    27.78). None rather than False, so "nothing here" reads the same as a
    document field the masking policy withheld."""

    def __init__(self, **kwargs):
        kwargs["read_only"] = True
        super().__init__(**kwargs)

    def get_attribute(self, instance):
        return getattr(instance, self.source, None)

    def to_representation(self, value):
        return True if getattr(value, "name", value) else None


class MoneyField(serializers.DecimalField):
    """A money attribute in the API's existing shape: the string '135.68'.

    Models store money as integer minor units and expose the old attribute
    name as a Decimal (`base.money.MinorUnitsAmount`). DRF renders a bare
    property through `ReadOnlyField`, whose Decimal becomes a JSON FLOAT --
    a silent contract change for every app. Declaring this field keeps the
    string the clients have always parsed, and keeps writes validated as
    before; the model property then rounds once, in the money helper.

    The places come from the currency table (the default market's exponent),
    not a literal 2.
    """

    def __init__(self, max_digits=10, **kwargs):
        from base.money import DEFAULT_CURRENCY, exponent

        kwargs.setdefault('decimal_places', exponent(DEFAULT_CURRENCY))
        super().__init__(max_digits=max_digits, **kwargs)
