"""The object-store SDK's exception types, so business code can catch them without importing
the SDK (the vendor boundary: see tests/test_vendor_boundary.py)."""

from botocore.config import Config  # noqa: F401  (re-exported for settings)
from botocore.exceptions import BotoCoreError, ClientError

#: What a storage operation can raise when the store itself refuses or is unreachable.
StorageClientError = ClientError
StorageTransportError = BotoCoreError
