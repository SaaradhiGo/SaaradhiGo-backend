"""Turning a storage failure into an answer somebody can act on.

WHY THIS EXISTS
---------------
When the object store refuses a write, `FileField.save` raises deep inside
botocore and the view returns a bare HTTP 500 with "An unexpected error
occurred." Three different people then read that one sentence:

  * the DRIVER, who is told nothing and reasonably tries again, forever;
  * SUPPORT, who cannot tell this apart from any other 500;
  * whoever is on call, who has no hint that the cause is configuration
    rather than code.

Measured on QA: both a presigned PUT and a server-side PutObject to the
document bucket answer `403 SignatureDoesNotMatch`, so the credentials the
deployment holds do not match the bucket. No client can upload a document,
and the symptom was an unexplained 500 on a driver's screen.

A storage outage is not a bug in the request. It deserves 503 and a
sentence that says the problem is ours and that retrying now will not help,
and it deserves to be loud in the log with the operation that failed.

DELIBERATELY NOT A HEALTH PROBE
-------------------------------
This does not ping S3 before each save. A probe doubles the calls, cannot
be trusted between the probe and the write, and would itself fail on a
deployment whose credentials deny HeadObject -- which is how the previous
defect in this area presented. It reports what actually happened.
"""

import logging

from integrations.storage.errors import StorageClientError as ClientError, StorageTransportError as BotoCoreError
from rest_framework import status

from base.utils import error_response

logger = logging.getLogger(__name__)

#: What the person is told. It names no bucket, key or credential: a driver
#: reading an error should not learn our storage topology.
STORAGE_UNAVAILABLE_MESSAGE = (
    'We could not store that file. This is a problem on our side, not with '
    'your document — our team has been alerted. Please try again later.'
)


class StorageUnavailable(Exception):
    """The object store refused the write."""


def save_with_storage_errors(save, *, what):
    """Run `save`, converting an object-store failure into a response.

    Returns `(result, None)` on success, or `(None, response)` where the
    response is a 503 the caller should return as-is.

    `what` names the operation for the log -- "driver documents", "vehicle
    documents" -- so an alert says which upload path is down.
    """
    try:
        return save(), None
    except ClientError as exc:
        code = exc.response.get('Error', {}).get('Code', 'Unknown')
        operation = getattr(exc, 'operation_name', 'unknown')
        # ERROR, not warning: nobody can upload anything until this is
        # fixed, and it is invisible from the outside.
        logger.error(
            'storage_write_failed what=%s operation=%s code=%s',
            what, operation, code,
        )
        return None, error_response(
            code='STORAGE_UNAVAILABLE',
            message=STORAGE_UNAVAILABLE_MESSAGE,
            field='file',
            issue=f'Object store rejected {operation} with {code}',
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    except BotoCoreError as exc:
        # Connection, endpoint and credential-resolution failures, which
        # never reach the service at all.
        logger.error(
            'storage_unreachable what=%s error=%s', what, type(exc).__name__,
        )
        return None, error_response(
            code='STORAGE_UNAVAILABLE',
            message=STORAGE_UNAVAILABLE_MESSAGE,
            field='file',
            issue=f'Object store unreachable: {type(exc).__name__}',
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
