"""S3 storage backends for uploaded media.

WHY `file_overwrite` IS TRUE
----------------------------
It was False, which is django-storages' way of saying "if this name is
taken, pick another". Finding out whether a name is taken costs a
`HeadObject` call on every single save.

Two things make that a bad trade here, and together they made document
upload impossible from the website:

1. **The collision it guards against cannot happen.** Every upload path is
   built by `PrefixedUUIDPath`, which is `uuid4().hex` plus the extension.
   Nothing is ever saved under a caller-supplied name, so there is no name
   to collide with.

2. **The deployment's storage credentials deny HeadObject.** The Railway
   bucket answers 403 to it, so `get_available_name` raised
   `ClientError: An error occurred (403) when calling the HeadObject
   operation` and EVERY `FileField.save` through this storage ended in HTTP
   500. Measured on QA, from the real traceback, not inferred.

The presigned direct-to-S3 path never showed this, because signing a URL
calls nothing -- which is why the mobile apps have always worked and the
website's first upload attempt failed at the last step.

With overwrite on, `get_available_name` returns the name it was given and
makes no call at all. Writing the same UUID twice would overwrite it; that
requires generating the same UUID twice.
"""

from storages.backends.s3boto3 import S3Boto3Storage


class PublicMediaStorage(S3Boto3Storage):
    default_acl = None
    # See the module docstring: the name is always a fresh uuid4, and the
    # existence probe this would otherwise do is denied by the bucket.
    file_overwrite = True
    querystring_auth = False


class PrivateDocumentStorage(S3Boto3Storage):
    default_acl = None
    file_overwrite = True
    querystring_auth = True


def public_media_storage():
    return PublicMediaStorage()


def private_document_storage():
    return PrivateDocumentStorage()
