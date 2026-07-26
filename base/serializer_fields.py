from rest_framework import serializers

from base.media import EMPTY_FILE_VALUES


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
