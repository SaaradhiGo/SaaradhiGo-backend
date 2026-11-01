"""Does object storage actually work, and if not, exactly which part fails?

    python manage.py check_storage              # read-only checks
    python manage.py check_storage --write      # full round trip

WHY THIS COMMAND EXISTS
-----------------------
Document upload was broken on QA and the only symptom anybody could see was
HTTP 500 on a driver's screen. Finding the cause took a traceback from the
container log, a presigned URL driven by hand with curl, and a boto3 script.
None of that is available to the person who most needs the answer, and none
of it is something you can run before a deploy.

Every failure this command reports is one that actually happened:

  * `HeadObject` denied by the credentials       -> every save 500s
  * `PutObject` refused as SignatureDoesNotMatch -> credentials do not
                                                    match the bucket
  * presigned PUT rejected                       -> addressing style,
                                                    region scope or
                                                    checksum defaults wrong
  * public URL pointing at the API endpoint      -> avatars 401 on R2

It writes nothing unless `--write` is passed, and anything it writes it
deletes, under a clearly-labelled key prefix.

WHY IT LIVES UNDER `servers.driver`
-----------------------------------
It is infrastructure, not a driver concern, and `base` would be the honest
home -- but `base` is not in INSTALLED_APPS, so Django would never find the
command there, and adding an app to the registry to gain a diagnostic is a
bigger change than the diagnostic is worth. Storage's only consumers today
are driver documents and vehicle pictures, so this is where the failures
show up. Move it to `base` if that ever becomes an app.
"""

import io
import uuid

import requests
from django.core.management.base import BaseCommand

from base import object_store


class Command(BaseCommand):
    help = 'Verify object storage configuration, optionally with a real round trip.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--write',
            action='store_true',
            help='Upload and delete a small test object. Writes to the bucket.',
        )

    # -- reporting helpers ------------------------------------------------

    def _ok(self, label, detail=''):
        self.stdout.write(self.style.SUCCESS(f'  PASS  {label}')
                          + (f' — {detail}' if detail else ''))

    def _bad(self, label, detail=''):
        self.failures += 1
        self.stdout.write(self.style.ERROR(f'  FAIL  {label}')
                          + (f' — {detail}' if detail else ''))

    def _note(self, label, detail=''):
        self.stdout.write(f'  ----  {label}' + (f' — {detail}' if detail else ''))

    # -- the checks --------------------------------------------------------

    def handle(self, *args, **options):
        from django.conf import settings

        self.failures = 0
        endpoint = getattr(settings, 'AWS_S3_ENDPOINT_URL', None)

        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING('Object storage'))
        self._note('provider', object_store.describe(endpoint))
        self._note('bucket', settings.AWS_S3_BUCKET_NAME or '(unset)')
        self._note('region', settings.AWS_S3_REGION_NAME or '(unset)')
        self._note('addressing', settings.AWS_S3_ADDRESSING_STYLE)

        if not settings.AWS_S3_BUCKET_NAME:
            self._bad('bucket name', 'AWS_S3_BUCKET_NAME is not set')
            return self._finish()
        if not settings.AWS_ACCESS_KEY_ID or not settings.AWS_SECRET_ACCESS_KEY:
            self._bad('credentials', 'AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY not set')
            return self._finish()

        self._check_consistency(settings, endpoint)
        self._check_public_url(settings, endpoint)

        client = self._check_client(settings)
        if client is None:
            return self._finish()

        if options['write']:
            self._check_round_trip(client, settings)
            self._check_presigned_put(settings)
        else:
            self._note('round trip', 'skipped; pass --write to upload a test object')

        return self._finish()

    def _finish(self):
        self.stdout.write('')
        if self.failures:
            self.stdout.write(self.style.ERROR(
                f'{self.failures} check(s) failed — uploads will not work.'))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS('Storage is configured correctly.'))

    def _check_consistency(self, settings, endpoint):
        """The two clients must agree, or one path works and the other does not."""
        expected_style = (
            settings.AWS_S3_ADDRESSING_STYLE
            or object_store.default_addressing_style(endpoint)
        )
        config = getattr(settings, 'AWS_S3_CLIENT_CONFIG', None)
        if config is None:
            self._bad('storages client config', 'AWS_S3_CLIENT_CONFIG is not set')
            return
        style = (config.s3 or {}).get('addressing_style')
        if style != expected_style:
            self._bad(
                'storages client config',
                f'addressing_style is {style!r}, expected {expected_style!r}. '
                'django-storages ignores AWS_S3_ADDRESSING_STYLE when a '
                'client config is supplied, so this silently wins.',
            )
        else:
            self._ok('presign and storages clients agree', f'addressing={style}')

        if object_store.is_r2(endpoint):
            if getattr(config, 'request_checksum_calculation', None) != 'when_required':
                self._bad(
                    'R2 checksum behaviour',
                    'request_checksum_calculation must be "when_required"; '
                    'botocore 1.36+ otherwise adds CRC32 headers a presigned '
                    'PUT cannot reproduce',
                )
            else:
                self._ok('R2 checksum behaviour', 'checksums only when required')

    def _check_public_url(self, settings, endpoint):
        """Public objects must not be served from the private API endpoint."""
        from base.storage_backends import PrivateDocumentStorage, PublicMediaStorage

        public_host = PublicMediaStorage().custom_domain
        if object_store.is_r2(endpoint) and not public_host:
            self._bad(
                'public file URLs',
                'AWS_S3_PUBLIC_BASE_URL is unset. On R2 the API endpoint is '
                'not publicly readable, so avatars and vehicle pictures will '
                'answer 401. Set it to the bucket\'s r2.dev address or a '
                'custom domain.',
            )
        elif public_host:
            self._ok('public file URLs', f'served from {public_host}')
        else:
            self._ok('public file URLs', 'served from the bucket (AWS)')

        # The security boundary: private documents must never carry a custom
        # domain, because django-storages then returns an UNSIGNED url().
        if PrivateDocumentStorage().custom_domain is not None:
            self._bad(
                'private document URLs',
                'PrivateDocumentStorage has a custom_domain, which makes '
                'url() return an UNSIGNED link to every driver document',
            )
        else:
            self._ok('private document URLs', 'presigned, no custom domain')

    def _check_client(self, settings):
        from integrations.storage.errors import StorageClientError as ClientError, StorageTransportError as BotoCoreError

        from integrations.storage.s3 import build_s3_client

        try:
            client = build_s3_client()
        except Exception as exc:
            self._bad('build client', f'{type(exc).__name__}: {exc}')
            return None

        # ListObjects rather than HeadBucket: some providers deny HeadBucket
        # to scoped credentials that can nonetheless read and write objects,
        # and a check that fails on a working bucket is worse than no check.
        try:
            client.list_objects_v2(Bucket=settings.AWS_S3_BUCKET_NAME, MaxKeys=1)
            self._ok('list objects', 'credentials are accepted by the bucket')
        except ClientError as exc:
            code = exc.response.get('Error', {}).get('Code')
            hint = ''
            if code in ('SignatureDoesNotMatch', '403', 'InvalidAccessKeyId'):
                hint = (' — the credentials do not match this bucket, or the '
                        'addressing style / region scope is wrong')
            self._bad('list objects', f'{code}{hint}')
            return None
        except BotoCoreError as exc:
            self._bad('list objects', f'cannot reach the endpoint: {type(exc).__name__}')
            return None

        return client

    def _check_round_trip(self, client, settings):
        """Put, read back and delete a real object, through django-storages."""
        from integrations.storage.errors import StorageClientError as ClientError

        from base.storage_backends import PrivateDocumentStorage

        storage = PrivateDocumentStorage()
        name = f'_storage_check/{uuid.uuid4().hex}.txt'
        payload = b'saaradhigo storage check'

        try:
            saved = storage.save(name, io.BytesIO(payload))
        except ClientError as exc:
            code = exc.response.get('Error', {}).get('Code')
            op = getattr(exc, 'operation_name', '?')
            self._bad('write via django-storages', f'{op} refused with {code}')
            return
        except Exception as exc:
            self._bad('write via django-storages', f'{type(exc).__name__}: {exc}')
            return
        self._ok('write via django-storages', saved)

        try:
            with storage.open(saved) as handle:
                read_back = handle.read()
            if read_back == payload:
                self._ok('read back', f'{len(read_back)} bytes match')
            else:
                self._bad('read back', 'content differs from what was written')
        except Exception as exc:
            self._bad('read back', f'{type(exc).__name__}: {exc}')

        try:
            url = storage.url(saved)
            if '?' in url and 'Signature' in url or 'X-Amz-' in url:
                self._ok('private url is signed')
            else:
                self._bad('private url is signed', f'no signature in {url[:80]}')
        except Exception as exc:
            self._bad('private url', f'{type(exc).__name__}: {exc}')

        try:
            storage.delete(saved)
            self._ok('delete', 'test object removed')
        except Exception as exc:
            self._bad('delete', f'{type(exc).__name__}: {exc} (object left behind)')

    def _check_presigned_put(self, settings):
        """The path the mobile apps use: sign a PUT, then actually make it."""
        from integrations.storage.s3 import generate_presigned_put

        try:
            signed = generate_presigned_put('license_doc', 'image/jpeg')
        except Exception as exc:
            self._bad('presign', f'{type(exc).__name__}: {exc}')
            return

        try:
            response = requests.put(
                signed['upload_url'],
                data=b'saaradhigo presign check',
                headers={'Content-Type': 'image/jpeg'},
                timeout=30,
            )
        except Exception as exc:
            self._bad('presigned PUT', f'could not reach the store: {exc}')
            return

        if response.ok:
            self._ok('presigned PUT', f'HTTP {response.status_code}')
            try:
                from integrations.storage.s3 import build_s3_client
                build_s3_client().delete_object(
                    Bucket=settings.AWS_S3_BUCKET_NAME, Key=signed['key'])
            except Exception:
                self._note('cleanup', f'could not remove {signed["key"]}')
        else:
            body = (response.text or '')[:200].replace('\n', ' ')
            self._bad(
                'presigned PUT',
                f'HTTP {response.status_code} — {body}. This is what the '
                'mobile apps do; if it fails, no client can upload.',
            )
