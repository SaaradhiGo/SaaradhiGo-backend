"""What kind of object store this deployment talks to, and how to talk to it.

ONE PLACE, BECAUSE THERE ARE TWO CALLERS AND THEY MUST NOT DRIFT
----------------------------------------------------------------
`integrations/storage/s3.py` builds its own boto3 client for presigning. `django-storages`
builds another from Django settings for reading and writing. When those two
disagree about addressing style, region or checksums, the symptom is a
signature the server rejects -- and it only shows up on whichever path
happens to be exercised. That is exactly how document upload reached
production-shaped QA broken: presign returned a URL that looked fine and
every actual write answered 403.

So the rules live here and both callers read them.

CLOUDFLARE R2, AND WHY IT NEEDS ITS OWN ANSWERS
------------------------------------------------
R2 speaks the S3 API, but three defaults that are right for AWS are wrong
for it, and each fails as an opaque signature or connection error rather
than as "unsupported":

1. **Addressing style must be `path`.** R2's endpoint is
   `https://<account-id>.r2.cloudflarestorage.com` and the bucket is the
   first path segment. Virtual-hosted style would address
   `<bucket>.<account-id>.r2.cloudflarestorage.com`, which does not resolve.

2. **Region must be `auto`.** R2 has no regions in the AWS sense. A real
   region name goes into the sigv4 credential scope and the signature stops
   matching.

3. **Checksums must be `when_required`.** botocore 1.36+ defaults to
   `when_supported`, which adds `x-amz-checksum-crc32` headers and, for
   streaming uploads, trailers. Those break presigned PUTs against R2:
   the signature is computed over headers the browser or curl will not
   send. This is the single most common R2-on-boto3 failure and it is
   invisible until an upload is attempted.

AWS S3 is unchanged by any of this: when the endpoint is not R2, the
previous behaviour is returned exactly.
"""

import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: Host suffix of R2's S3-compatible API.
R2_ENDPOINT_SUFFIX = '.r2.cloudflarestorage.com'


def is_r2(endpoint_url):
    """Is this endpoint Cloudflare R2?

    Decided from the HOST rather than from a flag someone has to remember
    to set. A deployment that points `AWS_S3_ENDPOINT_URL` at R2 and forgets
    a separate `USING_R2=true` would get AWS defaults and an unexplainable
    403.
    """
    if not endpoint_url:
        return False
    host = (urlparse(endpoint_url).hostname or '').lower()
    return host.endswith(R2_ENDPOINT_SUFFIX)


def default_addressing_style(endpoint_url):
    """`path` for R2, `virtual` otherwise.

    Virtual-hosted is forced on real AWS deliberately: recent botocore
    presigns to the global `s3.amazonaws.com` host with a regional
    signature scope, which S3 rejects with 403 on PUT.
    """
    return 'path' if is_r2(endpoint_url) else 'virtual'


def default_region(endpoint_url):
    """R2's only region is `auto`; elsewhere there is no default to give."""
    return 'auto' if is_r2(endpoint_url) else None


def client_config_kwargs(endpoint_url):
    """Extra `botocore.config.Config` kwargs for this store.

    Returns a dict so a caller can splat it into its own Config without
    knowing which keys apply. Empty for AWS, so nothing about the existing
    production path changes.
    """
    if not is_r2(endpoint_url):
        return {}
    return {
        # See the module docstring: `when_supported` adds checksum headers
        # and trailers that a presigned PUT cannot reproduce.
        'request_checksum_calculation': 'when_required',
        'response_checksum_validation': 'when_required',
    }


def describe(endpoint_url):
    """A one-line description for logs and the storage check command."""
    if is_r2(endpoint_url):
        account = (urlparse(endpoint_url).hostname or '').split('.')[0]
        return f'Cloudflare R2 (account {account})'
    if endpoint_url:
        return f'S3-compatible endpoint {endpoint_url}'
    return 'Amazon S3'
