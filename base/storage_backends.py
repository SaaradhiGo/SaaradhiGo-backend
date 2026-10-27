"""S3 / R2 storage backends for uploaded media.

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

2. **A store may deny HeadObject.** The Railway bucket answered 403 to it,
   so `get_available_name` raised `ClientError: An error occurred (403)
   when calling the HeadObject operation` and EVERY `FileField.save` ended
   in HTTP 500. Measured on QA from the real traceback.

With overwrite on, `get_available_name` returns the name it was given and
makes no call at all.

WHY THE PUBLIC AND PRIVATE STORAGES DIFFER ON `custom_domain`
--------------------------------------------------------------
This is a SECURITY boundary, not a cosmetic one.

Public objects (avatars, vehicle pictures) are served unsigned. On R2 the
S3 API endpoint is not publicly readable -- a plain URL against it answers
401 -- so they must be served from the bucket's public hostname instead,
which is what `AWS_S3_PUBLIC_BASE_URL` carries.

Private documents must never be served that way. Read
`S3Boto3Storage.url()`: when `custom_domain` is set and there is no
CloudFront signer, it returns the plain URL **and never signs it**, even
with `querystring_auth = True`. A `custom_domain` leaking onto the private
storage would therefore turn every driver's licence into a public URL,
silently, with no error anywhere. `custom_domain = None` is pinned on the
private class so a future global `AWS_S3_CUSTOM_DOMAIN` setting cannot do
that, and a test asserts it.
"""

from urllib.parse import urlparse

from django.conf import settings
from storages.backends.s3boto3 import S3Boto3Storage


def _public_host():
    """The hostname public objects are served from, or None.

    django-storages wants a HOST, not a URL -- it builds
    `{protocol}//{custom_domain}/{name}` -- so a configured value like
    `https://files.saaradhigo.com` has its scheme stripped here rather than
    producing `https://https://files...` at the point of use.
    """
    raw = (getattr(settings, 'AWS_S3_PUBLIC_BASE_URL', '') or '').strip()
    if not raw:
        return None
    if '//' in raw:
        return (urlparse(raw).netloc or '').rstrip('/') or None
    return raw.rstrip('/') or None


class PublicMediaStorage(S3Boto3Storage):
    default_acl = None
    # See the module docstring: the name is always a fresh uuid4, and the
    # existence probe this would otherwise do may be denied by the bucket.
    file_overwrite = True
    querystring_auth = False

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('custom_domain', _public_host())
        super().__init__(*args, **kwargs)


class PrivateDocumentStorage(S3Boto3Storage):
    default_acl = None
    file_overwrite = True
    querystring_auth = True
    # PINNED. See the module docstring: a custom domain here would make
    # `url()` return an UNSIGNED link to a driver's licence.
    custom_domain = None

    def __init__(self, *args, **kwargs):
        # Defence in depth: refuse the argument too, so no caller can pass
        # one in and quietly unsign this storage.
        kwargs['custom_domain'] = None
        super().__init__(*args, **kwargs)


def public_media_storage():
    return PublicMediaStorage()


def private_document_storage():
    return PrivateDocumentStorage()
