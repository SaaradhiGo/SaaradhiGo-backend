"""Talking to Cloudflare R2 instead of AWS S3, without breaking either.

R2 speaks the S3 API, but three defaults that are right for AWS are wrong
for it, and every one of them fails as an opaque `403
SignatureDoesNotMatch` rather than as "unsupported". That is the worst
possible failure mode: it looks identical to bad credentials, which is what
we spent an afternoon chasing on the previous provider.

These tests pin the rules so a future change cannot quietly reintroduce any
of them, and pin the ONE SECURITY BOUNDARY in this area: private documents
must never be served from a custom domain, because django-storages then
returns an unsigned URL.
"""

import pytest

from base import object_store

R2 = 'https://0a1b2c3d4e5f.r2.cloudflarestorage.com'
MINIO = 'https://minio.internal:9000'


class TestItRecognisesR2FromTheEndpoint:
    """Decided from the host, not from a flag somebody has to remember."""

    @pytest.mark.parametrize('endpoint', [
        R2,
        'https://ACCOUNT.R2.CLOUDFLARESTORAGE.COM',
        'https://x.r2.cloudflarestorage.com/some/path',
    ])
    def test_r2_endpoints_are_recognised(self, endpoint):
        assert object_store.is_r2(endpoint) is True

    @pytest.mark.parametrize('endpoint', [
        None, '', MINIO, 'https://s3.amazonaws.com',
        # Deliberately adversarial: a host that merely CONTAINS the suffix
        # as a prefix of its own name must not be treated as R2.
        'https://r2.cloudflarestorage.com.evil.example',
    ])
    def test_everything_else_is_not(self, endpoint):
        assert object_store.is_r2(endpoint) is False


class TestTheThreeDefaultsThatDifferOnR2:
    def test_r2_uses_path_addressing(self):
        """The bucket is a PATH segment; the virtual-hosted host does not
        resolve at all."""
        assert object_store.default_addressing_style(R2) == 'path'

    def test_aws_keeps_virtual_addressing(self):
        """Forced on AWS deliberately: botocore otherwise presigns to the
        global s3.amazonaws.com host with a regional signature scope, which
        S3 rejects with 403 on PUT."""
        assert object_store.default_addressing_style(None) == 'virtual'
        assert object_store.default_addressing_style(MINIO) == 'virtual'

    def test_r2_region_is_auto(self):
        """R2 has no regions; a real region name goes into the sigv4 scope
        and the signature stops matching."""
        assert object_store.default_region(R2) == 'auto'

    def test_no_region_is_invented_for_anything_else(self):
        assert object_store.default_region(None) is None
        assert object_store.default_region(MINIO) is None

    def test_r2_disables_automatic_checksums(self):
        """botocore 1.36+ defaults to `when_supported`, which adds
        x-amz-checksum-crc32 headers and streaming trailers. A presigned PUT
        cannot reproduce them, so the signature fails."""
        kwargs = object_store.client_config_kwargs(R2)

        assert kwargs['request_checksum_calculation'] == 'when_required'
        assert kwargs['response_checksum_validation'] == 'when_required'

    def test_aws_behaviour_is_untouched(self):
        """Nothing about the existing production path changes."""
        assert object_store.client_config_kwargs(None) == {}
        assert object_store.client_config_kwargs(MINIO) == {}


class TestTheTwoClientsCannotDisagree:
    """`integrations/storage/s3.py` presigns with its own client; django-storages reads and
    writes with another. When they disagree on addressing style the symptom
    is that one path works and the other 403s."""

    def test_the_storages_client_config_carries_the_addressing_style(self):
        from django.conf import settings

        config = settings.AWS_S3_CLIENT_CONFIG

        # django-storages applies `addressing_style` ONLY when client_config
        # is None (read S3Boto3Storage.__init__), so a partial config here
        # would silently drop it.
        assert config is not None
        assert (config.s3 or {}).get('addressing_style') == \
            settings.AWS_S3_ADDRESSING_STYLE
        assert config.signature_version == settings.AWS_S3_SIGNATURE_VERSION


class TestPrivateDocumentsAreNeverServedUnsigned:
    """THE SECURITY BOUNDARY IN THIS FILE.

    `S3Boto3Storage.url()` returns the plain custom-domain URL and never
    signs it when `custom_domain` is set and there is no CloudFront signer
    -- even with `querystring_auth = True`. A custom domain leaking onto the
    private storage would turn every driver's licence into a public URL,
    silently, with no error anywhere.
    """

    def test_the_private_storage_has_no_custom_domain(self):
        from base.storage_backends import PrivateDocumentStorage

        assert PrivateDocumentStorage().custom_domain is None

    def test_it_refuses_one_passed_in(self):
        from base.storage_backends import PrivateDocumentStorage

        storage = PrivateDocumentStorage(custom_domain='files.example.com')

        assert storage.custom_domain is None

    def test_it_ignores_a_global_custom_domain_setting(self, settings):
        from base.storage_backends import PrivateDocumentStorage

        settings.AWS_S3_CUSTOM_DOMAIN = 'files.example.com'

        assert PrivateDocumentStorage().custom_domain is None

    def test_it_still_signs(self):
        from base.storage_backends import PrivateDocumentStorage

        assert PrivateDocumentStorage().querystring_auth is True


class TestPublicFilesAreServedFromThePublicHost:
    def test_a_configured_url_has_its_scheme_stripped(self, settings):
        """django-storages wants a HOST; it builds `{protocol}//{domain}/...`,
        so a full URL would produce `https://https://...`."""
        from base.storage_backends import PublicMediaStorage

        settings.AWS_S3_PUBLIC_BASE_URL = 'https://files.saaradhigo.com/'

        assert PublicMediaStorage().custom_domain == 'files.saaradhigo.com'

    def test_a_bare_host_is_accepted_too(self, settings):
        from base.storage_backends import PublicMediaStorage

        settings.AWS_S3_PUBLIC_BASE_URL = 'pub-abc123.r2.dev'

        assert PublicMediaStorage().custom_domain == 'pub-abc123.r2.dev'

    def test_unset_means_the_bucket_serves_them(self, settings):
        from base.storage_backends import PublicMediaStorage

        settings.AWS_S3_PUBLIC_BASE_URL = ''

        assert PublicMediaStorage().custom_domain is None


class TestOverwriteStaysOn:
    """Re-asserted here because the R2 work touches the same classes: the
    existence probe this avoids is denied by some providers and guards
    against a collision that uuid4 names cannot have."""

    def test_both_storages_overwrite(self):
        from base.storage_backends import (
            PrivateDocumentStorage,
            PublicMediaStorage,
        )

        assert PrivateDocumentStorage().file_overwrite is True
        assert PublicMediaStorage().file_overwrite is True
