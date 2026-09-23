"""Document storage must be pointable at an S3-compatible endpoint.

KYC documents and receipt PDFs are stored through `private_document_storage`, an
S3 backend. With no S3 configured, `/uploads/presign/` fails and driver onboarding
is impossible -- which is exactly the state QA was in, because QA has no AWS
account behind it and never will.

`build_s3_client` constructs its own boto3 client for presigning, so it needed to
be told about a custom endpoint; the django-storages backends already read the
same setting themselves. These tests pin both halves of that: unset means real
AWS S3 (production's behaviour, unchanged), and set means the configured endpoint
is actually used.
"""

import pytest
from django.core.exceptions import ValidationError

from base.s3 import build_s3_client


@pytest.fixture(autouse=True)
def _credentials(settings):
    """Credential-shaped values so a client can be built without real ones."""
    settings.AWS_ACCESS_KEY_ID = 'AKIAQAQAQAQAQAQAQAQA'
    settings.AWS_SECRET_ACCESS_KEY = 'qa-secret-not-real'
    settings.AWS_S3_BUCKET_NAME = 'qa-bucket'


def test_unset_endpoint_still_targets_aws(settings):
    """Production sets no endpoint and must keep talking to real AWS S3.

    boto3 resolves the AWS host itself when endpoint_url is None, so the
    assertion is that the resolved host is an amazonaws.com one -- i.e. nothing
    about this change touches the production path.
    """
    settings.AWS_S3_ENDPOINT_URL = None
    settings.AWS_S3_REGION_NAME = 'ap-south-1'

    client = build_s3_client()
    assert 'amazonaws.com' in client.meta.endpoint_url


def test_a_configured_endpoint_is_used(settings):
    """The whole point: a QA bucket or a local MinIO becomes usable."""
    settings.AWS_S3_ENDPOINT_URL = 'https://t3.storageapi.dev'
    settings.AWS_S3_REGION_NAME = 'auto'

    client = build_s3_client()
    assert client.meta.endpoint_url == 'https://t3.storageapi.dev'


def test_an_empty_endpoint_is_treated_as_unset(settings):
    """An empty environment variable is the common way this gets mis-set.

    `AWS_S3_ENDPOINT_URL=""` must mean "use AWS", not "use the empty host" --
    boto3 would otherwise raise on an unparseable endpoint. This is the same
    empty-string trap that silently disabled QA's test phone numbers.
    """
    settings.AWS_S3_ENDPOINT_URL = ''
    settings.AWS_S3_REGION_NAME = 'ap-south-1'

    client = build_s3_client()
    assert 'amazonaws.com' in client.meta.endpoint_url


def test_a_non_aws_region_name_is_accepted(settings):
    """S3-compatible stores use region names AWS has never heard of.

    Railway buckets report `auto`. The region is only a signing scope for a
    custom endpoint, so it must not be validated against AWS's list.
    """
    settings.AWS_S3_ENDPOINT_URL = 'https://t3.storageapi.dev'
    settings.AWS_S3_REGION_NAME = 'auto'

    assert build_s3_client().meta.region_name == 'auto'


def test_a_missing_region_still_fails_loudly(settings):
    """The guard that produced QA's original error message stays.

    Silently defaulting a region would make a misconfigured environment look
    healthy until the first signature mismatch.
    """
    settings.AWS_S3_ENDPOINT_URL = 'https://t3.storageapi.dev'
    settings.AWS_S3_REGION_NAME = None
    settings.AWS_REGION = None

    with pytest.raises(ValidationError):
        build_s3_client()


def test_addressing_style_is_configurable(settings):
    """Some S3-compatible stores require path style. Virtual stays the default,
    because that is what AWS presigned PUTs need."""
    settings.AWS_S3_ENDPOINT_URL = 'https://t3.storageapi.dev'
    settings.AWS_S3_REGION_NAME = 'auto'
    settings.AWS_S3_ADDRESSING_STYLE = 'path'

    client = build_s3_client()
    assert client.meta.config.s3['addressing_style'] == 'path'
