"""Encrypted at rest (10 Nov): PAN, bank account numbers.

`EncryptedCharField` stores Fernet ciphertext and gives the plain value back in Python, so code
that reads `account.account_number` keeps working. The key is `FIELD_ENCRYPTION_KEY` (a Fernet
key); until it is set, one is derived from SECRET_KEY (listed in config-required.md). A value
that is not ciphertext (a row written before this field) reads back as it is, and the data
migration encrypts those rows.

Ciphertext differs on every write, so equality lookups use `fingerprint()` (an HMAC) instead.
"""
import base64
import hashlib
import hmac

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models

_PREFIX = 'enc1:'


def _key():
    raw = getattr(settings, 'FIELD_ENCRYPTION_KEY', '') or ''
    if raw:
        return raw.encode()
    digest = hashlib.sha256(b'saaradhigo-field-encryption:' + settings.SECRET_KEY.encode()).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt(value):
    if value in (None, ''):
        return value
    if str(value).startswith(_PREFIX):
        return value
    return _PREFIX + Fernet(_key()).encrypt(str(value).encode()).decode()


def decrypt(value):
    if not value or not str(value).startswith(_PREFIX):
        return value
    try:
        return Fernet(_key()).decrypt(value[len(_PREFIX):].encode()).decode()
    except InvalidToken:
        return None


def fingerprint(value):
    """A stable HMAC of a normalised value, for "is this the same account" checks."""
    if not value:
        return ''
    norm = ''.join(str(value).split()).upper()
    return hmac.new(_key(), norm.encode(), hashlib.sha256).hexdigest()


def mask(value, keep=4):
    value = str(value or '')
    return ('••' + value[-keep:]) if value else ''


class EncryptedCharField(models.CharField):
    """A CharField whose column holds ciphertext. Never filter on it; use a fingerprint column."""

    def from_db_value(self, value, expression, connection):
        return decrypt(value)

    def get_prep_value(self, value):
        return encrypt(super().get_prep_value(value))
