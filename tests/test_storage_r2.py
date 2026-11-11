"""QA storage on Cloudflare R2 (owner decision 2026-11-02).

Pins four things:
  * the KYC bucket can have its own credentials, and empty means the main ones;
  * with no public hostname, avatars are served by signed URL (R2's API
    endpoint is not publicly readable), and with one they stay unsigned;
  * unattached uploads older than 24 hours are deleted, referenced or recent
    ones never are;
  * records whose file is gone are found and cleared.
"""
from datetime import timedelta
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from base.storage_backends import PrivateDocumentStorage, PublicMediaStorage
from integrations.storage import s3 as s3mod
from servers.driver import upload_sweep
from servers.driver.models import Driver

User = get_user_model()

R2 = dict(
    AWS_ACCESS_KEY_ID='main-key', AWS_SECRET_ACCESS_KEY='main-secret',
    AWS_S3_ENDPOINT_URL='https://acct.r2.cloudflarestorage.com', AWS_S3_REGION_NAME='auto',
    AWS_S3_BUCKET_NAME='saaradhigo-qa-media', AWS_STORAGE_BUCKET_NAME='saaradhigo-qa-media',
    AWS_S3_PRIVATE_BUCKET_NAME='saaradhigo-qa-kyc',
)


class TestCredentials:
    def test_private_storage_uses_the_main_credentials_when_its_own_are_empty(self, settings):
        for k, v in R2.items():
            setattr(settings, k, v)
        settings.AWS_S3_PRIVATE_ACCESS_KEY_ID = ''
        settings.AWS_S3_PRIVATE_SECRET_ACCESS_KEY = ''
        settings.AWS_S3_PRIVATE_ENDPOINT_URL = None
        s = PrivateDocumentStorage()
        assert s.bucket_name == 'saaradhigo-qa-kyc'
        assert s.access_key == 'main-key'

    def test_private_storage_uses_its_own_credentials_when_set(self, settings):
        for k, v in R2.items():
            setattr(settings, k, v)
        settings.AWS_S3_PRIVATE_ACCESS_KEY_ID = 'kyc-key'
        settings.AWS_S3_PRIVATE_SECRET_ACCESS_KEY = 'kyc-secret'
        settings.AWS_S3_PRIVATE_ENDPOINT_URL = 'https://other.example'
        s = PrivateDocumentStorage()
        assert (s.access_key, s.secret_key, s.endpoint_url) == (
            'kyc-key', 'kyc-secret', 'https://other.example')
        assert PublicMediaStorage().access_key == 'main-key', 'never leaks to public media'

    def test_presign_client_for_a_document_uses_the_private_credentials(self, settings):
        for k, v in R2.items():
            setattr(settings, k, v)
        settings.AWS_S3_PRIVATE_ACCESS_KEY_ID = 'kyc-key'
        settings.AWS_S3_PRIVATE_SECRET_ACCESS_KEY = 'kyc-secret'
        with mock.patch.object(s3mod.boto3, 'client') as client:
            s3mod.build_s3_client('license_doc')
            assert client.call_args.kwargs['aws_access_key_id'] == 'kyc-key'
            s3mod.build_s3_client('avatar')
            assert client.call_args.kwargs['aws_access_key_id'] == 'main-key'


class TestPublicMediaUrls:
    def test_no_public_host_means_signed_urls(self, settings):
        for k, v in R2.items():
            setattr(settings, k, v)
        settings.AWS_S3_PUBLIC_BASE_URL = ''
        settings.PUBLIC_MEDIA_URL_TTL_SECONDS = 3600
        s = PublicMediaStorage()
        assert s.querystring_auth is True and s.querystring_expire == 3600
        assert 'X-Amz-Signature' in s.url('avatars/' + 'a' * 32 + '.png')

    def test_a_public_host_means_plain_urls(self, settings):
        for k, v in R2.items():
            setattr(settings, k, v)
        settings.AWS_S3_PUBLIC_BASE_URL = 'https://media.example'
        s = PublicMediaStorage()
        url = s.url('avatars/' + 'a' * 32 + '.png')
        assert url.startswith('https://media.example/') and 'X-Amz' not in url

    def test_documents_are_always_signed(self, settings):
        for k, v in R2.items():
            setattr(settings, k, v)
        settings.AWS_S3_PUBLIC_BASE_URL = 'https://media.example'
        url = PrivateDocumentStorage().url('license_docs/' + 'b' * 32 + '.pdf')
        assert 'X-Amz-Signature' in url and 'media.example' not in url


@pytest.mark.django_db
class TestUnattachedSweep:
    def _client(self, objects):
        client = mock.Mock()
        client.get_paginator.return_value.paginate.side_effect = (
            lambda Bucket, Prefix: [{'Contents': [o for o in objects if o['Key'].startswith(Prefix)]}])
        return client

    def test_only_old_unreferenced_uploads_are_deleted(self, settings):
        settings.UNATTACHED_UPLOAD_MAX_AGE_HOURS = 24
        now = timezone.now()
        old, new = now - timedelta(hours=30), now - timedelta(hours=2)
        kept_key = 'license_docs/' + 'c' * 32 + '.pdf'
        u = User.objects.create_user(phone_number='+919811100009', role='driver')
        Driver.objects.create(user_id=u, license_doc=kept_key)
        objects = [
            {'Key': 'license_docs/' + 'd' * 32 + '.pdf', 'LastModified': old},  # orphan: delete
            {'Key': kept_key, 'LastModified': old},                              # attached: keep
            {'Key': 'avatars/' + 'e' * 32 + '.png', 'LastModified': new},         # recent: keep
        ]
        client = self._client(objects)
        with mock.patch('integrations.storage.s3.build_s3_client', return_value=client), \
                mock.patch('integrations.storage.s3._bucket', return_value='b'):
            result = upload_sweep.sweep_unattached_uploads(now=now)
        deleted = [c.kwargs['Key'] for c in client.delete_object.call_args_list]
        assert deleted == ['license_docs/' + 'd' * 32 + '.pdf']
        assert result['deleted'] == 1

    def test_dry_run_deletes_nothing(self, settings):
        now = timezone.now()
        client = self._client([{'Key': 'rc_docs/' + 'f' * 32 + '.pdf',
                                'LastModified': now - timedelta(days=3)}])
        with mock.patch('integrations.storage.s3.build_s3_client', return_value=client), \
                mock.patch('integrations.storage.s3._bucket', return_value='b'):
            result = upload_sweep.sweep_unattached_uploads(now=now, dry_run=True)
        client.delete_object.assert_not_called()
        assert result['deleted'] == 1


@pytest.mark.django_db
def test_records_whose_file_is_gone_are_found_and_cleared():
    u = User.objects.create_user(phone_number='+919811100010', role='driver')
    d = Driver.objects.create(user_id=u, license_doc='license_docs/' + '1' * 32 + '.pdf')
    with mock.patch.object(PrivateDocumentStorage, 'exists', return_value=False), \
            mock.patch.object(PublicMediaStorage, 'exists', return_value=True):
        missing = upload_sweep.files_missing_from_storage()
    assert (Driver, Driver._meta.get_field('license_doc'), d.pk) in missing
    upload_sweep.clear_missing(missing)
    d.refresh_from_db()
    assert not d.license_doc


def test_threads_share_one_boto3_session(settings):
    """11 Nov: a new thread per request must not build a new session and load the S3 model again."""
    import threading

    from base.storage_backends import PublicMediaStorage

    store = PublicMediaStorage(access_key='k', secret_key='s', endpoint_url='https://r2.example.invalid',
                               bucket_name='b', region_name='auto')
    got = {}

    def grab(name):
        got[name] = (store._create_session(), store.connection)

    threads = [threading.Thread(target=grab, args=(n,)) for n in ('a', 'b')]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert got['a'][0] is got['b'][0]
    assert got['a'][1] is not got['b'][1]
